from pathlib import Path
from datetime import timedelta
import json
import shutil

import pytest
import yaml

from tests.test_drafting import MODELS, PROMPTS, RecordedClient
from tests.test_project_research import analysis, prepare, review, NOW
from x_algo_watch.drafting import analyze_items, validate_analysis
from x_algo_watch.drafting import run
from x_algo_watch.automation import detect, publish
from tests.fakes import FakeGitHub
from x_algo_watch.evidence import digest
from x_algo_watch.models import WatchError
from x_algo_watch.project_evidence import pending_packs, project_pack, save_context, save_context_snapshot
from x_algo_watch.readers import load_edition
from x_algo_watch.research import RESEARCH_FIELDS, analysis_projection, render_research, research_event, research_schema, validate_research, valid_research_event
from x_algo_watch.readers import READER_FIELDS
from x_algo_watch.storage import RecordStore
from x_algo_watch.versions import build_versions


def test_new_calls_require_four_question_reader_and_ground_its_claims(sample_repo):
    repo, result = prepare(sample_repo); pack = project_pack(repo, result.to_dict())
    old = analysis(pack); del old['reader']
    assert validate_research(old, pack) == []  # 历史审计仍可读取。
    assert validate_research(old, pack, require_reader=True)
    forged=research_event(pack,{'state':'pending_confirmation','analysis':old,'review':review(pack)},MODELS)
    assert not valid_research_event(forged,pack)  # 发布边界也不能绕过格式。
    wrong = analysis(pack); wrong['reader']['who'] += '曝光上涨 9876.54321'
    assert validate_analysis(analysis_projection(wrong), pack, project_research=True)
    wrong = analysis(pack); wrong['reader']['impacts'][0]['evidence_refs'] = ['E999']
    assert validate_research(wrong, pack)
    wrong = analysis(pack); wrong['reader']['online_status']='deployed'
    assert validate_research(wrong, pack)
    wrong = analysis(pack); wrong['reader']['what_changed']='Phoenix 候选水合后进行处理。'
    assert validate_research(wrong, pack)
    wrong = analysis(pack); wrong['reader']['when'] = ''
    assert validate_research(wrong, pack)


def test_reader_first_technical_details_last_no_duplicate_summary(sample_repo):
    repo, result = prepare(sample_repo); pack = project_pack(repo, result.to_dict())
    value = analysis(pack)
    value['summary'] = 'TECHNICAL_SUMMARY_SENTINEL'
    event = research_event(pack, {'state':'pending_confirmation','analysis':value,'review':review(pack), 'extra_review_rounds':2, 'reviews':{stage:review(pack) for stage in ('review','facts_review','reader_review')}}, MODELS)
    text = render_research(event)
    front, tail = text.split('<details><summary>技术分析', 1)
    assert all(q in front for q in ('改了什么？','已经上线了吗？','可能影响谁？','现在该做什么？'))
    assert '线上部署尚未核实' in front and '生成与审核记录' not in front
    assert 'TECHNICAL_SUMMARY_SENTINEL' not in text
    assert '另一种解释' in tail and '如何验证' in tail
    assert '[E1](' in front and '证据强度' not in front


def test_unresolved_new_reader_is_withheld_until_repaired(sample_repo):
    repo, result = prepare(sample_repo); pack = project_pack(repo, result.to_dict())
    value = analysis(pack); value['reader']['what_changed'] = '错误正文测试标记'
    bad = review(pack, False); bad['issues'][0]['field'] = 'reader'
    result = {'state':'disputed','analysis':value,'review':bad}
    event = research_event(pack, result, MODELS)
    text = render_research(event)
    assert '错误正文测试标记' not in text.split('研究附录：')[0]
    assert '用户正文待修订' in text
    assert '错误正文测试标记' in text  # 只在明确标记的旧稿审计内。
    outputs, _ = analyze_items([pack], [], RecordedClient([value, bad]), MODELS, NOW, PROMPTS, project_research=True)
    assert outputs[pack['item_id']]['state'] == 'disputed'


def test_edition_binds_exact_research_and_rejects_tampered_supplement(sample_repo, tmp_path):
    repo, result = prepare(sample_repo); pack = project_pack(repo, result.to_dict())
    save_context_snapshot(tmp_path/'data', pack)
    value = analysis(pack)
    event = research_event(pack, {'state':'pending_confirmation','analysis':value,'review':review(pack)}, MODELS)
    edition = {'schema_version':1,'source_research_id':event['id'],'item_id':event['item_id'],'reader':value['reader']}
    directory=tmp_path/'editorial/versions'; directory.mkdir(parents=True)
    path=directory/(event['commit']+'.json'); path.write_text(json.dumps(edition),encoding='utf-8')
    assert load_edition(tmp_path,event)['reader']==value['reader']
    different={**event,'id':'research:another'}
    assert load_edition(tmp_path,different) is None
    edition['supplementary_evidence']={'R1':{'text':'changed','text_digest':digest('original'),'path':'source.rs','commit':event['commit'],'start_line':1}}
    path.write_text(json.dumps(edition),encoding='utf-8')
    with pytest.raises(WatchError): load_edition(tmp_path,event)


def test_readability_compatibility_keeps_agreed_research_but_not_disputes(sample_repo, tmp_path, monkeypatch):
    repo, result = prepare(sample_repo); pack = project_pack(repo, result.to_dict())
    save_context(tmp_path/'data',pack)
    event=research_event(pack,{'state':'pending_confirmation','analysis':analysis(pack),'review':review(pack)},MODELS)
    event['profile_hash']='a'*64
    RecordStore(tmp_path/'data').append_event(event,NOW)
    monkeypatch.chdir(tmp_path)
    (tmp_path/'config').mkdir(); config=tmp_path/'config/watch.yaml'
    config.write_text(yaml.safe_dump({'readability_compatible_profiles':['a'*64]}),encoding='utf-8')
    assert pending_packs(tmp_path/'data')==[]
    config.write_text('{}',encoding='utf-8')
    assert len(pending_packs(tmp_path/'data'))==1
    config.write_text(yaml.safe_dump({'readability_compatible_profiles':['a'*64]}),encoding='utf-8')
    event['state']='disputed'; event['id']='research:new'
    # 未通过的旧研究不得借兼容清单标成完成。
    save_context(tmp_path/'failed-data',pack)
    RecordStore(tmp_path/'failed-data').append_event(event,NOW)
    assert len(pending_packs(tmp_path/'failed-data'))==1


def test_real_latest_edition_corrects_user_issues_without_rewriting_model_audit():
    root=Path(__file__).resolve().parents[1]
    before=(root/'data/records/2026-10.md').read_bytes()
    sha='989fb91254d461bf4b9df53fb12d5c9a61c2b354'
    edition_file=json.loads((root/'editorial/versions'/f'{sha}.json').read_text(encoding='utf-8'))
    event=next(e for e in RecordStore(root/'data').events() if e['id']==edition_file['source_research_id'])
    edition=load_edition(root,event)
    assert edition and len(edition['supplementary_evidence'])==2
    text=render_research(event,edition=edition)
    front=text.split('<details><summary>技术分析')[0]
    assert '复核分歧' in front and '算法版本更新公告' not in front
    assert '默认开关为关闭（false）' not in front
    assert '[R1](' in text  # 迁移证据仍留在技术审计中。
    assert (root/'data/records/2026-10.md').read_bytes()==before


def test_all_current_nine_editions_validate_and_versions_build_without_data_changes():
    root=Path(__file__).resolve().parents[1]
    events={e['id']:e for e in RecordStore(root/'data').events()}
    editions=list((root/'editorial/versions').glob('*.json'))
    assert len(editions)>=9
    for path in editions:
        original=json.loads(path.read_text(encoding='utf-8'))
        event=events[original['source_research_id']]
        assert load_edition(root,event) is not None


def test_budget_deferred_new_profile_keeps_previous_reviewed_reading_page(sample_repo, tmp_path):
    repo, result = prepare(sample_repo); pack = project_pack(repo,result.to_dict())
    store=RecordStore(tmp_path/'data'); store.append(result,NOW,'uncommitted')
    completed=research_event(pack,{'state':'pending_confirmation','analysis':analysis(pack),'review':review(pack)},MODELS)
    store.append_event(completed,NOW)
    pending=research_event(pack,{'state':'pending'},MODELS)
    store.append_event(pending,NOW+timedelta(seconds=1))
    build_versions(tmp_path)
    page=next((tmp_path/'versions').glob('20*.md')).read_text(encoding='utf-8')
    assert '处理中' in page and '算法版本更新公告' not in page
    assert '推荐帖子来源设置有变化' not in page
    assert '处理中' in (tmp_path/'versions/README.md').read_text(encoding='utf-8')


def test_technical_page_uses_same_research_with_working_relative_evidence_links(sample_repo, tmp_path):
    repo, result = prepare(sample_repo); pack = project_pack(repo,result.to_dict())
    store=RecordStore(tmp_path/'data'); store.append(result,NOW,'uncommitted')
    event=research_event(pack,{'state':'pending_confirmation','analysis':analysis(pack),'review':review(pack)},MODELS)
    store.append_event(event,NOW)
    build_versions(tmp_path)
    user_page=next((tmp_path/'versions').glob('20*.md'))
    technical=tmp_path/'versions/technical'/user_page.name
    assert 'technical/'+user_page.name in user_page.read_text(encoding='utf-8')
    text=technical.read_text(encoding='utf-8')
    assert '../'+user_page.name in text and '../../data/project-context/' in text
    assert '### 算法机制与技术依据' in text and '### 技术推断与验证办法' in text
    assert '30 秒摘要' not in text and '普通读者可跳过' not in text
    assert analysis(pack)['hypotheses'][0]['hypothesis'] in text


def test_online_prompt_and_published_schema_use_same_exact_fields(sample_repo):
    root=Path(__file__).resolve().parents[1]
    schema=research_schema()
    assert json.loads((root/'schemas/version-research-v2.json').read_text(encoding='utf-8'))==schema
    assert set(schema['required'])==RESEARCH_FIELDS
    assert set(schema['properties']['reader']['required'])==READER_FIELDS
    repo,result=prepare(sample_repo); pack=project_pack(repo,result.to_dict())
    client=RecordedClient([analysis(pack),review(pack)])
    analyze_items([pack],[],client,MODELS,NOW,PROMPTS,project_research=True)
    for _,system,_ in client.calls:
        assert json.dumps(schema,ensure_ascii=False) in system
        assert 'additionalProperties' in system and 'online_status' in system


def test_missing_reader_auto_corrects_once_then_blocks_if_still_missing(sample_repo):
    repo,result=prepare(sample_repo); pack=project_pack(repo,result.to_dict())
    invalid=analysis(pack); invalid.pop('reader')
    corrected=RecordedClient([invalid,analysis(pack),review(pack)])
    output,events=analyze_items([pack],[],corrected,MODELS,NOW,PROMPTS,project_research=True)
    assert output[pack['item_id']]['state']=='pending_confirmation'
    assert len(corrected.calls)==3 and corrected.calls[1][2]['previous_validation_errors']
    assert [e['valid'] for e in events if e['type']=='model_call']==[False,True,True]
    blocked=RecordedClient([invalid,invalid])
    output,_=analyze_items([pack],[],blocked,MODELS,NOW,PROMPTS,project_research=True)
    assert output[pack['item_id']]['state']=='manual' and len(blocked.calls)==2


def test_future_version_generates_reader_then_review_repairs_it_without_editorial_file(sample_repo, tmp_path, monkeypatch):
    repo,result=prepare(sample_repo)
    workspace=tmp_path/'watch';workspace.mkdir();monkeypatch.chdir(workspace)
    shutil.copytree(PROMPTS,workspace/'prompts')
    config=workspace/'config/watch.yaml';config.parent.mkdir()
    config.write_text(yaml.safe_dump({'models':MODELS,'project_research':True,'daily_calls':8,'extra_review_rounds':2}),encoding='utf-8')
    RecordStore(workspace/'data').append(result,NOW,'uncommitted')
    bundle=workspace/'bundle';detect(bundle,source=repo.path,research_commit=result.commit)
    pack=project_pack(repo,result.to_dict())
    bad=analysis(pack);bad['reader']['who']='这是需要纠正的影响对象。'
    objection=review(pack,False);objection['issues'][0]['field']='reader'
    fixed=analysis(pack);fixed['reader']['who']='只有新来源覆盖的帖子作者可能受影响。'
    model=RecordedClient([bad,objection,review(pack),review(pack),fixed,review(pack),review(pack),review(pack)])
    output=workspace/'draft.json';run(bundle,output,config,client=model)
    publish(bundle,FakeGitHub(),draft_bundle=output,push=False)
    assert len(model.calls)==8 and not (workspace/'editorial').exists()
    page=next((workspace/'versions').glob('20*.md')).read_text(encoding='utf-8')
    tech_page=next((workspace/'versions/technical').glob('20*.md')).read_text(encoding='utf-8')
    front=page.split('> 💡 **专业读者导航**')[0]
    assert fixed['reader']['who'] in front and bad['reader']['who'] not in front
    assert all(label in front for label in ('30 秒摘要','改了什么？','已经上线了吗？','可能影响谁？','现在该做什么？'))
    assert '影响解读' in front and '研究附录：' in tech_page


def test_announcement_keeps_same_evidence_and_action_after_adjustments(sample_repo):
    repo, result = prepare(sample_repo)
    pack = project_pack(repo, result.to_dict())
    value = analysis(pack)
    event = research_event(pack, {'state': 'pending_confirmation', 'analysis': value, 'review': review(pack), 'extra_review_rounds':2, 'reviews':{stage:review(pack) for stage in ('review','facts_review','reader_review')}}, MODELS)
    page = render_research(event)
    front = page.split('<details><summary>技术分析')[0]
    assert '算法版本更新公告' in front and '非官方源码解读' in front
    assert front.index('### 30 秒摘要：本次更新重点') < front.index('## 你可能感受到的变化（影响解读）') < front.index('## 现在该怎么做')
    assert front.count(value['reader']['action']) == 1
    for row in value['reader']['impacts']:
        assert all(row[field] in front for field in ('heading', 'effect', 'conditions', 'limits'))
        assert all('[' + ref + ']' in front for ref in row['evidence_refs'])
    disputed = research_event(pack, {'state': 'disputed', 'analysis': value, 'review': review(pack, False)}, MODELS)
    blocked = render_research(disputed).split('<details><summary>技术分析')[0]
    assert value['reader']['title'] not in blocked
    assert '## 你可能感受到的变化（影响解读）' not in blocked


def test_announcement_contract_reaches_all_model_roles():
    root = Path(__file__).resolve().parents[1]
    for role in ('analysis', 'review', 'facts_review', 'reader_review'):
        prompt = (root / 'prompts' / ('version-' + role + '-v1.md')).read_text(encoding='utf-8')
        assert 'before/after 对照同一场景' in prompt
        assert '用户影响优先流程' in prompt
    effect = research_schema()['properties']['reader']['properties']['impacts']['items']['properties']['effect']
    assert '普通用户' in effect['description']


def test_new_reader_requires_user_before_after_and_checks_entire_body(sample_repo):
    from copy import deepcopy
    repo, result = prepare(sample_repo)
    pack = project_pack(repo, result.to_dict())
    value = analysis(pack)
    assert not validate_research(value, pack, require_reader=True, user_impact=True)
    old = deepcopy(value)
    old['reader']['impacts'][0].pop('before')
    old['reader']['impacts'][0].pop('after')
    assert not validate_research(old, pack)  # 历史研究仍可读取。
    assert validate_research(old, pack, user_impact=True)
    for key in ('heading', 'before', 'after', 'effect', 'conditions', 'limits'):
        wrong = deepcopy(value)
        wrong['reader']['impacts'][0][key] = 'TIMELINE_HOME_HYDRATION 名单发生变化'
        assert any('用户正文' in e for e in validate_research(wrong, pack, user_impact=True))
    wrong = deepcopy(value)
    wrong['reader']['terms'] = [{'term': '规则', 'meaning': 'after 被截断，无法核对。', 'evidence_refs': ['E1']}]
    assert any('用户正文' in e for e in validate_research(wrong, pack, user_impact=True))
    value['reader']['impacts'] = []  # 没有直接用户影响时，不强迫编造。
    assert not validate_research(value, pack, user_impact=True)


def test_before_after_numbers_are_grounded_and_process_follows_content(sample_repo):
    repo, result = prepare(sample_repo)
    pack = project_pack(repo, result.to_dict())
    value = analysis(pack)
    value['reader']['impacts'][0]['before'] += ' 987654321'
    assert '数字不在证据包：987654321' in validate_analysis(analysis_projection(value), pack, project_research=True)
    event = research_event(pack, {'state':'pending_confirmation', 'analysis':analysis(pack), 'review':review(pack), 'extra_review_rounds':2, 'reviews':{stage:review(pack) for stage in ('review','facts_review','reader_review')}}, MODELS)
    page = render_research(event)
    assert '| 以前 | 现在的代码如何处理 |' in page
    assert page.index('对你的影响') < page.index('研究附录：')
    assert '核对项目 | 当前情况' not in page
