import json
import shutil
import yaml

import pytest

from tests.test_project_research import NOW, prepare
from tests.test_project_research import analysis, review
from tests.test_drafting import RecordedClient, PROMPTS, MODELS
from x_algo_watch.automation import publish
from x_algo_watch.drafting import run
from x_algo_watch.models import WatchError
from x_algo_watch.project_evidence import project_pack, save_context
from x_algo_watch.sample import prepare_sample, write_owner_preview
from x_algo_watch.research import research_event
from x_algo_watch.model_policy import aggregate_reviews
from x_algo_watch.storage import RecordStore


def test_sample_uses_existing_context_without_changing_progress(sample_repo, tmp_path):
    repo, result = prepare(sample_repo)
    root = tmp_path / "watch"; data = root / "data"
    store = RecordStore(data); store.append(result,NOW,"uncommitted")
    save_context(data,project_pack(repo,result.to_dict()))
    before = {p:p.read_bytes() for p in data.rglob('*') if p.is_file()}
    bundle = root / "runtime/sample"
    prepare_sample(root,result.commit,bundle)
    manifest = json.loads((bundle/'manifest.json').read_text(encoding='utf-8'))
    assert manifest['research'] == [result.commit]
    assert all(p.read_bytes()==body for p,body in before.items())
    assert store.last_success() == result.commit
    with pytest.raises(WatchError, match='已存在'):
        prepare_sample(root,result.commit,bundle)


@pytest.mark.parametrize('commit',['main','a'*40])
def test_sample_rejects_unrecorded_or_non_sha_target(tmp_path,commit):
    with pytest.raises(WatchError):
        prepare_sample(tmp_path,commit,tmp_path/'sample')


def test_sample_cannot_write_into_source_data(tmp_path):
    with pytest.raises(WatchError,match='runtime'):
        prepare_sample(tmp_path,'a'*40,tmp_path/'data/sample')


def test_reviewed_sample_can_publish_locally_without_issue_or_key_access(sample_repo,tmp_path,monkeypatch):
    repo,result=prepare(sample_repo); root=tmp_path/'watch'; data=root/'data'
    RecordStore(data).append(result,NOW,'uncommitted')
    pack=project_pack(repo,result.to_dict());save_context(data,pack)
    shutil.copytree(PROMPTS,root/'prompts');monkeypatch.chdir(root)
    config=root/'config/watch.yaml';config.parent.mkdir()
    config.write_text(yaml.safe_dump({'models':MODELS,'project_research':True,'extra_review_rounds':2}),encoding='utf-8')
    bundle=root/'runtime/sample';prepare_sample(root,result.commit,bundle)
    draft=root/'runtime/draft.json'
    run(bundle,draft,config,client=RecordedClient([analysis(pack),review(pack),review(pack),review(pack)]))
    monkeypatch.setattr('x_algo_watch.automation.publish_issues',lambda *args: pytest.fail('不应访问 Issue'))
    # 模型运行期间 main 新增记录时，只能追加同源研究事件，不能回写旧快照。
    RecordStore(data).append_event({'type':'manual_note','id':'keep-new-record'},NOW)
    with pytest.raises(WatchError,match='已变化'):
        publish(bundle,None,draft_bundle=draft,push=False,notify_issues=False)
    assert publish(bundle,None,draft_bundle=draft,push=False,notify_issues=False,merge_research=True)==[]
    event=[e for e in RecordStore(data).events() if e['type']=='version_research'][-1]
    assert event['state']=='pending_confirmation' and len(event['reviews'])==3
    assert any(e['id']=='keep-new-record' for e in RecordStore(data).events())
    assert (root/'versions/technical').is_dir() and not (root/'digest.md').exists()


def test_owner_preview_preserves_disputed_original_and_escapes_html(sample_repo,tmp_path):
    repo,result=prepare(sample_repo);pack=project_pack(repo,result.to_dict())
    value=analysis(pack);value['reader']['what_changed']='<script>原稿文本</script>'
    reviews={'review':review(pack,False),'facts_review':review(pack,False),'reader_review':review(pack)}
    event=research_event(pack,{'state':'disputed','analysis':value,'review':aggregate_reviews(pack['item_id'],reviews),'reviews':reviews,'extra_review_rounds':2},MODELS)
    output=tmp_path/'owner-preview';write_owner_preview(event,pack,output)
    user=(output/'index.html').read_text(encoding='utf-8')
    assert '1 轮同意，2 轮不同意' in user and '暂不可作为可发布结论' in user
    assert '<script>' not in user and '&lt;script&gt;原稿文本&lt;/script&gt;' in user
    assert 'technical.html' in user and '技术审阅页' in (output/'technical.html').read_text(encoding='utf-8')
    assert json.loads((output/'research.json').read_text(encoding='utf-8'))==event
    with pytest.raises(WatchError,match='完整三轮'):
        write_owner_preview({**event,'reviews':{}},pack,tmp_path/'invalid')
