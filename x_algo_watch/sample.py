"""使用已有可信证据准备单版本样例；不改进度、不访问模型密钥。"""

import argparse
import html
import json
from pathlib import Path
import re
import shutil

from .automation import data_files, write_bundle
from .git import SHA
from .models import WatchError
from .project_evidence import context_packs
from .storage import RecordStore
from .storage import atomic_write


def write_owner_preview(event: dict, pack: dict, output: Path) -> None:
    """仅供所有者验收的本地双页预览；不改模型文字，不发布争议结论。"""
    from .research import valid_research_event
    from .readers import evidence_links
    from .iteration import PHASES, REASONS, NEXT_LABELS
    original_event = event
    if not valid_research_event(event,pack,required_extra_reviews=2):
        raise WatchError('审阅预览需要同源的完整三轮裁决')
    if output.exists():
        raise WatchError('审阅预览目录已存在，请使用新目录')
    esc = lambda value: html.escape(str(value))
    progress = ''
    loop = event.get('iteration')
    if loop:
        label, recovery = PHASES[loop['phase']]
        progress = '<section class="card"><h2>迭代进度：V'+str(loop['candidate'])+'</h2><p>'+esc(label)+'</p><p>原因：'+esc(REASONS[loop['reason']])+'</p><p>恢复条件：'+esc(recovery)+'</p><p>下一步：'+esc(NEXT_LABELS[loop['next_stage']])+'</p><table><tr><th>候选</th><th>审核</th><th>问题数</th></tr>'
        for row in loop['history']:
            progress += '<tr><td>V'+str(row['number'])+'</td><td>'+('全部通过，已通过复核' if row['result']['review']['agree'] else '存在异议')+'</td><td>'+str(len(row['result']['review']['issues']))+'</td></tr>'
        progress += '</table><details><summary>问题清单与处理状态</summary><p>未再提出仅表示本版未重复该条质疑，不等于已完成复核或已确认。</p>'
        for row in loop['ledger']:
            progress += '<p><code>'+esc(row['id'])+'</code> · '+esc(row['status'])+'：'+esc(row['reason'])+'</p>'
        progress += '</details></section>'
        if loop['candidate'] > len(loop['history']) and loop['history']:
            event = {**event, **loop['history'][-1]['result']}
            progress += '<p class="notice">当前候选仍未完成。以下展示上一份完成审核的候选原稿；其异议仍待处理。</p>'
    if set(event.get('reviews', {})) != {'review','facts_review','reader_review'}:
        if not loop:
            raise WatchError('审阅预览需要同源的完整三轮裁决')
        body = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>迭代等待续跑</title><body><nav><a href="index.html">通俗页</a> · <a href="technical.html">技术页</a> · <a href="research.json">研究记录</a></nav>'+progress+'<p>尚无完成三个角色审核的候选，暂不展示用户或技术结论。</p></body></html>'
        for name in ('index.html', 'technical.html'):
            atomic_write(output/name, body)
        atomic_write(output/'research.json',json.dumps(original_event,ensure_ascii=False,indent=2))
        atomic_write(output/'evidence.json',json.dumps(pack,ensure_ascii=False,indent=2))
        return
    def refs(values: list[str]) -> str:
        markdown = evidence_links(values,event['sources'],'evidence.json')
        return ' · '.join('<a href="'+esc(url)+'">'+esc(label)+'</a>' for label,url in re.findall(r'\[([^\]]+)\]\(([^)]+)\)',markdown))
    names={'review':'综合复核','facts_review':'事实与遗漏核对','reader_review':'推断与读者理解核对'}
    agreed=sum(r['agree'] for r in event['reviews'].values())
    status='三轮完成：'+str(agreed)+' 轮同意，'+str(3-agreed)+' 轮不同意'
    notice='以下是实际模型原稿，已通过模型复核；线上部署尚未核实。' if agreed==3 else '本轮存在未解决分歧，暂不可作为可发布结论。以下保留实际模型原稿；没有人工改写。'
    css='body{margin:0;background:#f5f6f8;color:#20252b;font:17px/1.85 system-ui,"Microsoft YaHei",sans-serif}main{max-width:920px;margin:32px auto;padding:32px;background:white;border-radius:14px}h1{font-size:28px;line-height:1.5}h2{margin-top:32px;font-size:23px}h3{font-size:19px}a{color:#185dc3}nav{display:flex;gap:20px;flex-wrap:wrap}.notice{background:#fff0ec;border-left:5px solid #c64020;padding:18px;margin:24px 0}.meta{color:#555;font-size:14px}.card{border:1px solid #ddd;border-radius:8px;padding:18px;margin:16px 0}code{overflow-wrap:anywhere}details{margin:20px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere}table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid #ddd;padding:12px;text-align:left}@media(max-width:650px){main{margin:0;padding:20px}body{font-size:16px}}'
    audit='<table><tr><th>核对角色</th><th>实际裁决</th><th>问题数</th></tr>'
    for stage,review in event['reviews'].items():
        audit+='<tr><td>'+names[stage]+'</td><td>'+('同意' if review['agree'] else '不同意')+'</td><td>'+str(len(review['issues']))+'</td></tr>'
    audit+='</table>'
    top='<p class="meta">版本 '+esc(event['commit'][:7])+' · 所有者审阅样例 · 线上部署尚未核实</p>'+progress+'<div class="notice"><strong>'+status+'</strong><p>'+notice+'</p></div>'+audit
    analysis=event['analysis'];reader=analysis['reader']
    def page(title: str, body: str) -> str:
        return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+esc(title)+'</title><style>'+css+'</style></head><body><main><nav><a href="index.html">通俗审阅页</a><a href="technical.html">技术审阅页与审核意见</a><a href="research.json">完整研究 JSON</a></nav>'+top+body+'</main></body></html>'
    user='<h1>'+esc(reader['title'])+'</h1><h2>30 秒摘要：本次更新重点（模型原稿）</h2>'
    for field,label in [('what_changed','改了什么？'),('when','是否上线、何时可能生效？'),('who','可能影响谁？')]:
        user+='<h3>'+label+'</h3><p>'+esc(reader[field])+'</p>'
    user+='<p>摘要依据：'+refs(reader['evidence_refs'])+'</p><h2>具体调整与影响解读：模型原稿</h2>'
    for row in reader['impacts']:
        if 'before' in row and 'after' in row:
            user+='<p><strong>以前：</strong>'+esc(row['before'])+'</p><p><strong>现在的代码如何处理：</strong>'+esc(row['after'])+'</p>'
        user+='<section class="card"><h3>'+esc(row['heading'])+'</h3><p>'+esc(row['effect'])+'</p><p><strong>成立条件：</strong>'+esc(row['conditions'])+'</p><p><strong>不能下的结论：</strong>'+esc(row['limits'])+'</p>'+refs(row['evidence_refs'])+'</section>'
    user+='<h2>现在该怎么做</h2><p>'+esc(reader['action'])+'</p>'
    if reader['terms']:
        user+='<details><summary>原稿的术语解释（可检查是否易懂）</summary>'
        for row in reader['terms']:user+='<p><strong>'+esc(row['term'])+'</strong>：'+esc(row['meaning'])+' '+refs(row['evidence_refs'])+'</p>'
        user+='</details>'
    user+='<p class="meta">本地审阅稿已通过模型复核，线上部署尚未核实。三轮具体意见与源码证据见技术审阅页。</p>'
    tech='<h1>技术审阅：'+esc(analysis['title'])+'</h1><h2>三轮原始裁决</h2>'
    for stage,review in event['reviews'].items():
        tech+='<section class="card"><h3>'+names[stage]+'：'+('同意' if review['agree'] else '不同意')+'</h3><p class="meta">实际型号：'+esc(event.get('resolved_models',{}).get(stage) or '接口未返回')+'</p>'
        for issue in review['issues']:tech+='<p><code>'+esc(issue['field'])+'</code>：'+esc(issue['reason'])+' '+refs(issue['evidence_refs'])+'</p>'
        tech+='</section>'
    tech+='<h2>源码前后行为：模型原稿</h2>'
    for row in analysis['changes']:
        tech+='<section class="card"><h3>'+esc(row['mechanism'])+'</h3><p><strong>以前：</strong>'+esc(row['before'])+'</p><p><strong>现在：</strong>'+esc(row['after'])+'</p>'+refs(row['evidence_refs'])+'</section>'
    tech+='<h2>机制推断：模型原稿</h2>'
    for row in analysis['hypotheses']:
        tech+='<section class="card"><h3>'+esc(row['hypothesis'])+'</h3><p>机制证据强度：'+esc(row['confidence'])+'；不代表上线或流量效果已验证。</p>'
        for field,label in [('basis','依据'),('conditions','成立条件'),('creator_impact','创作者影响'),('alternative','替代解释'),('verify','验证办法')]:tech+='<p><strong>'+label+'：</strong>'+esc(row[field])+'</p>'
        tech+=refs(row['evidence_refs'])+'</section>'
    tech+='<h2>待核对问题：模型原稿</h2><ul>'+''.join('<li>'+esc(q)+'</li>' for q in analysis['questions'])+'</ul><h2>覆盖与原始输入</h2><p>这是有范围限制的项目证据，不代表通读全部源码。</p><p><a href="evidence.json">实际证据包</a> · <a href="research.json">研究与三轮裁决原始 JSON</a></p><pre>'+esc(json.dumps(pack['coverage'],ensure_ascii=False,indent=2))+'</pre>'
    atomic_write(output/'index.html',page('通俗审阅样例',user))
    atomic_write(output/'technical.html',page('技术审阅样例',tech))
    atomic_write(output/'research.json',json.dumps(original_event,ensure_ascii=False,indent=2))
    atomic_write(output/'evidence.json',json.dumps(pack,ensure_ascii=False,indent=2))


def prepare_sample(root: Path, commit: str, bundle: Path) -> None:
    if not SHA.fullmatch(commit):
        raise WatchError("样例必须指定完整提交号")
    runtime = (root / "runtime").resolve()
    if bundle.resolve() == runtime or not bundle.resolve().is_relative_to(runtime):
        raise WatchError("样例输出必须位于项目 runtime 的新子目录")
    data = root / "data"
    if not any(r["commit"] == commit and r["complete"] and r.get("base") for r in RecordStore(data).records()):
        raise WatchError("样例需要已有完整迭代记录")
    if not any(p["commit"] == commit for p in context_packs(data)):
        raise WatchError("样例没有可信项目上下文")
    if bundle.exists():
        raise WatchError("样例输出目录已存在，请使用新目录")
    working = bundle / "working"
    shutil.copytree(data, working)
    write_bundle(bundle, data_files(data), working, test=False, code="single-version-sample", result_code=0, research_only=commit)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument("--bundle", required=True, type=Path)
    args = parser.parse_args()
    try:
        prepare_sample(Path.cwd(), args.commit, args.bundle)
        return 0
    except WatchError as exc:
        print(str(exc)); return 2


if __name__ == "__main__":
    raise SystemExit(main())
