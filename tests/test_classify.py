import pytest

from x_algo_watch.classify import parameter_level
from x_algo_watch.models import Parameter, Severity


def param(name, value):
    return Parameter(name, "bool" if type(value) is bool else "u32", name.lower(), value, repr(value), 1)


@pytest.mark.parametrize("name,before,after,expected", [
    ("EnablePopularPostsSource", False, True, Severity.RED),
    ("FavoriteWeight", 0.5, 0.6, Severity.ORANGE),
    ("PhoenixClusterId", "old", "new", Severity.GRAY),
    ("EnableCandidatesLogging", None, False, Severity.YELLOW),
    ("ShadowTrafficRate", None, 0.3, Severity.YELLOW),
    ("BrandSafetyLimit", None, 50, Severity.ORANGE),
    ("Unused", 1, None, Severity.YELLOW),
])
def test_classification(name, before, after, expected):
    old = param(name, before) if before is not None else None
    new = param(name, after) if after is not None else None
    level, _, _ = parameter_level(name, old, new, {name: new} if new else {}, [])
    assert level == expected


def test_closed_gate_and_nonempty_experiment_wording():
    name = "PopularPostsMaxResults"
    params = {"EnablePopularPostsSource": param("EnablePopularPostsSource", False), name: param(name, 500)}
    level, message, _ = parameter_level(name, None, params[name], params, [])
    assert level == Severity.YELLOW and "EnablePopularPostsSource" in message
    name = "ShadowClusters"
    _, message, _ = parameter_level(name, None, param(name, "nonempty"), {}, [])
    assert "默认关" not in message


def test_registry_precedes_name_rules():
    name = "PhoenixClusterId"
    fact = {"id": "important", "source": {"name": name, "path": "home-mixer/params/param.rs"}}
    args = (name, param(name, "old"), param(name, "new"), {})
    assert parameter_level(*args, [fact])[0] == Severity.RED
    fact.update(kind="ignore", note="经人工核对的噪音")
    assert parameter_level(*args, [fact])[0] == Severity.GRAY
