"""修订循环主路径、暂停、恢复和发布边界；全部为模拟模型响应。"""

from copy import deepcopy
import json
from pathlib import Path
import shutil

import pytest
import yaml

from tests.test_drafting import PROMPTS, RecordedClient
from tests.test_project_research import NOW, analysis, prepare, review
from x_algo_watch.drafting import Deferred
from x_algo_watch.iteration import iterate_items, iteration_policy
from x_algo_watch.project_evidence import pending_packs, project_pack, save_context
from x_algo_watch.research import research_event, valid_research_event
from x_algo_watch.sample import write_owner_preview
from x_algo_watch.storage import RecordStore
from x_algo_watch.models import WatchError

MODELS = {"analysis": "openrouter/free", "review": "openrouter/free"}


@pytest.fixture
def study(sample_repo, tmp_path, monkeypatch):
    repo, result = prepare(sample_repo)
    pack = project_pack(repo, result.to_dict())
    root = tmp_path / 'watch'
    root.mkdir()
    monkeypatch.chdir(root)
    shutil.copytree(PROMPTS, root/'prompts')
    (root/'config').mkdir()
    settings = {"models": MODELS, "project_research": True, "extra_review_rounds": 2, "daily_calls": 1000, "max_candidate_versions": 5, "stagnation_limit": 2}
    (root/'config/watch.yaml').write_text(yaml.safe_dump(settings), encoding='utf-8')
    save_context(root/'data', pack)
    return pack, settings, root


def candidate(pack, index):
    value = analysis(pack)
    value['questions'] = ['调用链是否接入？' + '待核对。' * index]
    return value


def responses(pack, index, agrees=False):
    verdict = review(pack, agrees)
    if not agrees:
        verdict['issues'][0]['reason'] = '对应证据缺少调用位置。' + '待核对。' * index
    return [candidate(pack, index), review(pack), verdict, review(pack)]


def invoke(study, values, prior=(), **kwargs):
    pack, settings, _ = study
    client = RecordedClient(values)
    outputs, calls = iterate_items([pack], list(prior), client, settings, Path('prompts'), **kwargs)
    event = research_event(pack, outputs[pack['item_id']], MODELS)
    assert valid_research_event(event, pack, required_extra_reviews=2)
    return event, calls, client


def test_more_than_one_revision_then_all_three_roles_pass(study):
    pack, _, root = study
    event, calls, client = invoke(study, responses(pack, 0) + responses(pack, 1) + responses(pack, 2, True))
    assert len(client.calls) == 12
    assert event['iteration']['phase'] == 'ready' and event['iteration']['candidate'] == 3
    assert len(event['iteration']['history']) == 3 and event['state'] == 'pending_confirmation'
    assert all(row['status'] == 'rechecked' for row in event['iteration']['ledger'])
    assert client.calls[4][2]['issue_ledger']
    assert 'review_feedback' not in client.calls[5][2]  # 审核不读取其他角色的意见。
    assert not client.calls[5][2]['previous_own_issues']
    assert client.calls[6][2]['previous_own_issues'] == event['iteration']['history'][0]['result']['reviews']['facts_review']['issues']
    assert not client.calls[7][2]['previous_own_issues']
    write_owner_preview(event, pack, root/'preview')
    assert 'V3' in (root/'preview/index.html').read_text(encoding='utf-8')
    assert not (root/'digest.md').exists()


def test_mid_revision_service_failure_resumes_only_missing_checks(study):
    pack, _, root = study
    first, calls, client = invoke(study, responses(pack, 0) + [candidate(pack, 1), review(pack), Deferred('HTTP 504，等待续跑')])
    assert len(client.calls) == 7 and first['state'] == 'pending'
    assert first['iteration']['phase'] == 'waiting_service' and first['iteration']['candidate'] == 2
    assert first['iteration']['next_stage'] == 'facts_review'
    write_owner_preview(first, pack, root/'waiting')
    assert '上一份完成审核' in (root/'waiting/technical.html').read_text(encoding='utf-8')
    done, later, client = invoke(study, [review(pack), review(pack)], calls + [first])
    assert len(client.calls) == 2 and done['iteration']['phase'] == 'ready'
    assert done['iteration']['candidate'] == 2
    assert len([e for e in calls+later if e['type'] == 'model_call']) == 9


def test_revision_limit_preserves_history_and_explicit_resume(study):
    pack, settings, root = study
    event, calls, client = invoke(study, [value for i in range(5) for value in responses(pack, i)])
    assert len(client.calls) == 20 and event['iteration']['phase'] == 'paused_limit'
    RecordStore(root/'data').append_event(event, NOW)
    assert pending_packs(root/'data') == []
    unchanged, _, client = invoke(study, [], calls+[event])
    assert unchanged == event and not client.calls
    fixed, _, client = invoke(study, responses(pack, 5, True), calls+[event], resume=True)
    assert len(client.calls) == 4 and fixed['iteration']['candidate'] == 6
    assert fixed['iteration']['batch_start'] == 6 and fixed['iteration']['resume_count'] == 1
    assert len(fixed['iteration']['history']) == 6
    assert client.calls[0][2]['previous_analysis'] == event['analysis']


def test_repeated_unchanged_problem_stops_after_two_no_progress_revisions(study):
    pack, _, _ = study
    event, _, client = invoke(study, responses(pack, 0)*3)
    assert len(client.calls) == 12
    assert event['iteration']['phase'] == 'paused_stagnation'
    assert event['iteration']['candidate'] == 3
    assert len(event['iteration']['ledger']) == 1


def test_daily_budget_and_time_boundary_are_recoverable_without_new_candidate(study):
    pack, settings, _ = study
    settings['daily_calls'] = 1
    first, calls, _ = invoke(study, [analysis(pack)])
    assert first['iteration']['phase'] == 'waiting_budget'
    settings['daily_calls'] = 1000
    done, _, client = invoke(study, [review(pack)]*3, calls+[first])
    assert len(client.calls) == 3 and done['iteration']['candidate'] == 1
    pending, _, client = invoke(study, [], call_deadline=0)
    assert pending['iteration']['phase'] == 'waiting_time' and not client.calls


def test_service_failures_are_bounded_and_explicitly_recoverable(study):
    pack, _, _ = study
    prior = []
    for i in range(3):
        event, calls, _ = invoke(study, [Deferred('提供方拥堵')], prior)
        prior += calls+[event]
        assert event['iteration']['candidate'] == 1
        assert event['iteration']['phase'] == ('waiting_service' if i < 2 else 'waiting_manual')
    _, _, client = invoke(study, [], prior)
    assert not client.calls
    event, _, client = invoke(study, responses(pack, 0, True), prior, resume=True)
    assert len(client.calls) == 4 and event['iteration']['phase'] == 'ready'


def test_manual_resume_of_failed_review_reuses_other_completed_stages(study):
    pack, _, _ = study
    event, calls, _ = invoke(study, [analysis(pack), review(pack), Deferred('暂时失败')])
    prior = calls+[event]
    for _ in range(2):
        event, calls, client = invoke(study, [Deferred('暂时失败')], prior)
        assert len(client.calls) == 1
        prior += calls+[event]
    assert event['iteration']['phase'] == 'waiting_manual'
    done, _, client = invoke(study, [review(pack), review(pack)], prior, resume=True)
    assert len(client.calls) == 2 and done['iteration']['phase'] == 'ready'
    assert all('analysis' in payload for _, _, payload in client.calls)


def test_invalid_output_requires_explicit_resume_and_can_show_progress_only(study):
    pack, _, root = study
    event, calls, _ = invoke(study, [{}, {}])
    assert event['iteration']['phase'] == 'waiting_manual' and event['state'] == 'manual'
    write_owner_preview(event, pack, root/'invalid')
    text = (root/'invalid/index.html').read_text(encoding='utf-8')
    assert '尚无完成三个角色审核' in text and 'resume_iterations' in text
    done, _, _ = invoke(study, responses(pack, 0, True), calls+[event], resume=True)
    assert done['iteration']['phase'] == 'ready'


@pytest.mark.parametrize('mutation', ['phase', 'ledger', 'verdict', 'context', 'candidate', 'root'])
def test_publication_rejects_forged_loop_or_history(study, mutation):
    pack, _, _ = study
    event, _, _ = invoke(study, responses(pack, 0, True))
    changed = json.loads(json.dumps(event))
    loop = changed['iteration']
    if mutation == 'phase': loop['phase'] = 'paused_limit'
    if mutation == 'ledger': loop['ledger'] = [{'id': 'fake'}]
    if mutation == 'verdict': loop['history'][0]['result']['reviews']['facts_review'] = review(pack, False)
    if mutation == 'context': loop['context_hash'] = '0'*64
    if mutation == 'candidate': loop['candidate'] = 99
    if mutation == 'root': changed['analysis']['reader']['who'] = '其他作者。'
    changed['id'] = research_event(pack, changed, MODELS)['id']  # 即便重算摘要也不能绕过。
    assert not valid_research_event(changed, pack, required_extra_reviews=2)


@pytest.mark.parametrize('settings', [{'max_candidate_versions': True}, {'max_candidate_versions': 11}, {'stagnation_limit': 0}])
def test_iteration_policy_rejects_invalid_limits(settings):
    with pytest.raises(WatchError):
        iteration_policy(settings)


def test_no_hypothesis_is_allowed_when_explicit_evidence_gap_is_reviewed(study):
    pack, _, _ = study
    value = analysis(pack)
    value['hypotheses'] = []
    value['no_hypothesis_reason'] = '缺少调用链，不能判断实际曝光影响。'
    value['reader']['action'] = '当前证据不足，无需据此调整发布策略。'
    event, _, _ = invoke(study, [value]+[review(pack)]*3)
    assert event['iteration']['phase'] == 'ready'
