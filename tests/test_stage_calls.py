import json

import pytest

from x_algo_watch.models import UnsafePublicText
from x_algo_watch.storage import public_json
from x_algo_watch.models import Deferred
from x_algo_watch.stage_calls import deferred_metadata


def test_json_newlines_before_decorators_are_not_misclassified_as_contact_addresses():
    raw = json.dumps({"quote": "@jax.jit\ndef run(): pass\n"})
    public_json({"raw_output": raw, "output": json.loads(raw)})


def test_escaped_credentials_are_still_rejected_in_decoded_or_malformed_json():
    secret = "sk-or-v1-" + "a" * 30
    raw = json.dumps({"text": secret}).replace("sk-or", "\\u0073k-or")
    with pytest.raises(UnsafePublicText): public_json({"raw_output": raw})
    with pytest.raises(UnsafePublicText): public_json(raw[:-1])


@pytest.mark.parametrize(("message", "reason"), [("finish_reason=length", "output_length"),
    ("模型未返回可用 JSON 内容", "empty_content"), ("免费模型连接或响应异常", "connection"),
    ("HTTP 429", "http"), ("untrusted provider text", "unknown")])
def test_deferred_diagnostics_classify_without_copying_untrusted_message(message, reason):
    client = type("Client", (), {"last_response_received": True, "last_response_status": 429})()
    value = deferred_metadata(Deferred(message + " sk-or-v1-" + "a" * 40), client)
    expected = {"deferred_reason": reason, "response_received": True, "response_status": 429}
    if reason == "http":
        expected["http_reason"] = "unspecified"
    assert value == expected
    public_json(value)


@pytest.mark.parametrize(("message", "category"), [("账户免费每日额度已用尽", "account_daily_limit"),
    ("账户免费每分钟限流", "account_minute_limit"), ("模型提供方暂时拥堵", "provider_overloaded"),
    ("项目密钥额度限制", "key_limit")])
def test_http_reasons_keep_only_known_categories(message, category):
    result = deferred_metadata(Deferred("HTTP 429；" + message), object())
    assert result["http_reason"] == category
    assert message not in str(result)
