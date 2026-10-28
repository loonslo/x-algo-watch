import json

import pytest

from x_algo_watch.models import UnsafePublicText
from x_algo_watch.storage import public_json


def test_json_newlines_before_decorators_are_not_misclassified_as_contact_addresses():
    raw = json.dumps({"quote": "@jax.jit\ndef run(): pass\n"})
    public_json({"raw_output": raw, "output": json.loads(raw)})


def test_escaped_credentials_are_still_rejected_in_decoded_or_malformed_json():
    secret = "sk-or-v1-" + "a" * 30
    raw = json.dumps({"text": secret}).replace("sk-or", "\\u0073k-or")
    with pytest.raises(UnsafePublicText): public_json({"raw_output": raw})
    with pytest.raises(UnsafePublicText): public_json(raw[:-1])
