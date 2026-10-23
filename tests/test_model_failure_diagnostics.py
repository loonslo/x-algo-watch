"""只记录固定诊断类别，区分普通失败和真正扫描命中，不泄漏原文。"""

import io
import json
import runpy
import urllib.error
import warnings

import pytest

from tests.test_drafting import MODELS, NOW, PROMPTS, RecordedClient, record_and_pack
from x_algo_watch.drafting import OpenRouter, analyze_items
from x_algo_watch.models import UnsafePublicText, WatchError
from x_algo_watch.storage import public_text


@pytest.mark.parametrize(('text', 'category'), [
    ('sk-or-v1-' + 'x'*32, 'credential'),
    ('ghp_' + 'x'*32, 'credential'),
    ('github_pat_' + 'x'*32, 'credential'),
    ('-----BEGIN ' + 'PRIVATE KEY-----', 'private_key'),
    ('C:' + '/Users/example/private', 'local_path'),
    ('/home/' + 'example/private', 'local_path'),
    ('example' + '@example.invalid', 'email'),
])
def test_sensitive_output_is_still_blocked_without_echoing_match(text, category):
    with pytest.raises(UnsafePublicText) as caught:
        public_text(text)
    assert isinstance(caught.value, WatchError) and caught.value.category == category
    assert text not in str(caught.value)
    _, pack = record_and_pack()
    class LiteralClient:
        def call(self, *_):
            return json.dumps({'text': text})
    outputs, events = analyze_items([pack], [], LiteralClient(), MODELS, NOW, PROMPTS)
    assert outputs[pack['item_id']]['state'] == 'manual'
    calls = [e for e in events if e['type'] == 'model_call']
    assert len(calls) == 2
    assert all(e['error_code'] == 'sensitive_'+category and e['failure_stage'] == 'output_scan' for e in calls)
    assert all('raw_output' not in e and 'output' not in e for e in calls)
    assert text not in json.dumps(events)
    public_text(json.dumps(events))


def test_plain_request_error_is_not_misreported_as_sensitive_content():
    _, pack = record_and_pack()
    hidden = 'sk-or-v1-' + 'x'*32
    outputs, events = analyze_items([pack], [], RecordedClient([WatchError(hidden), WatchError(hidden)]), MODELS, NOW, PROMPTS)
    calls = [e for e in events if e['type'] == 'model_call']
    assert outputs[pack['item_id']]['state'] == 'manual'
    assert all(e['error_code'] == 'local_validation_error' and e['failure_stage'] == 'request' for e in calls)
    assert all('未确认敏感内容' in e['errors'][0] for e in calls)
    assert hidden not in json.dumps(events)
    public_text(json.dumps(events))


def test_successful_http_response_records_safe_status_even_when_output_is_blocked(monkeypatch):
    _, pack = record_and_pack()
    class Response(io.BytesIO):
        status = 200
    content = json.dumps({'text': 'example'+'@example.invalid'})
    def reply(*_args, **_kwargs):
        return Response(json.dumps({'choices': [{'message': {'content': content}}]}).encode())
    monkeypatch.setattr('urllib.request.urlopen', reply)
    monkeypatch.setattr('x_algo_watch.drafting.time.sleep', lambda _: None)
    models = {'analysis':'openrouter/free', 'review':'openrouter/free'}
    _, events = analyze_items([pack], [], OpenRouter('test-key'), models, NOW, PROMPTS)
    calls = [e for e in events if e['type'] == 'model_call']
    assert all(e['response_received'] is True and e['response_status'] == 200 for e in calls)
    assert all(e['resolved_model'] is None and e['error_code'] == 'sensitive_email' for e in calls)


def test_module_entrypoint_and_imported_loop_share_deferred_exception(monkeypatch):
    """复现 -m 入口创建客户端、iteration 导入 drafting 后异常类不一致的问题。"""
    from x_algo_watch import drafting
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        entry = runpy.run_module('x_algo_watch.drafting', run_name='entrypoint_probe')
    assert entry['Deferred'] is drafting.Deferred

    def unavailable(*_args, **_kwargs):
        raise urllib.error.HTTPError('https://openrouter.ai/api/v1/chat/completions',404,'unavailable',{},io.BytesIO(b'{}'))
    monkeypatch.setattr('urllib.request.urlopen',unavailable)
    _, pack = record_and_pack()
    models = {'analysis':'openrouter/free', 'review':'openrouter/free'}
    outputs, events = analyze_items([pack], [], entry['OpenRouter']('test-key'), models, NOW, PROMPTS)
    call = next(e for e in events if e['type']=='model_call')
    assert outputs[pack['item_id']]['state']=='pending'
    assert call['outcome']=='deferred' and call['error_code']=='transport_deferred'
    assert call['failure_stage']=='request' and call['response_status']==404
    assert len([e for e in events if e['type']=='model_call'])==1
