from pathlib import Path

import pytest

from x_algo_watch.models import ParseError
from x_algo_watch.parsers import guard_count, literal, parse_levels, parse_modules, parse_params, parse_updates

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("raw,expected", [
    ("false", False), ("0.66", 0.66), ("-52.272", -52.272), ("10_000", 10000),
    ('vec!["a,b".to_string(), "other".to_string()]', ["a,b", "other"]),
    ("vec![12, 34]", [12, 34]), ('"a); // remains string"', "a); // remains string"),
])
def test_literals(raw, expected):
    assert literal(raw) == expected


@pytest.mark.parametrize("raw", ["some_function()", 'include_str!("anything")', "1 + 2", "vec![1; 9]"])
def test_unknown_literals_fail(raw):
    with pytest.raises(ParseError):
        literal(raw)


def test_param_multiline_comments_and_line_numbers():
    source = '// last sync 2026-10-20T16:00:00Z\n/* param!(Wrong, bool, "bad", true); */\nparam!(\n  Example, Vec<String>, "key",\n vec!["a,b".to_string(), "c)".to_string()],\n);\n'
    params, sync = parse_params(source)
    assert set(params) == {"Example"}
    assert params["Example"].value == ["a,b", "c)"]
    assert params["Example"].line == 3
    assert sync == "2026-10-20T16:00:00Z"


@pytest.mark.parametrize("source", [
    "// last sync 2026-10-20T16:00:00Z\n", "param!(Bad, bool, \"key\", true);",
    '// last sync 2026-10-20T16:00:00Z\nparam!(Bad, bool, "key", "false");',
    '// last sync 2026-10-20T16:00:00Z\nparam!(Bad, bool, "key", false); param!(Bad, bool, "other", false);',
    '// last sync 2026-10-20T16:00:00Z\nparam!{Bad, bool, "key", false}',
])
def test_invalid_params_fail(source):
    with pytest.raises(ParseError):
        parse_params(source)


@pytest.mark.parametrize("file,style,extra", [("levels-e1a0375.rs.txt", "levels!", 26), ("levels-92f0918.rs.txt", "array", 26), ("levels-74d49a4.rs.txt", "PostRules", 27)])
def test_actual_historical_levels(file, style, extra):
    levels, actual_style = parse_levels((FIXTURES / file).read_text(encoding="utf-8"))
    assert actual_style == style
    assert len(set(levels["TIMELINE_HOME_RECOMMENDATIONS"]) - set(levels["TIMELINE_HOME"])) == extra


@pytest.mark.parametrize("file", ["levels-92f0918.rs.txt", "levels-74d49a4.rs.txt"])
def test_unknown_syntax_at_both_format_transitions_fails(file):
    source = (FIXTURES / file).read_text(encoding="utf-8")
    if "92f" in file:
        source = source.replace("&[&Rule]", "UnknownRuleList")
    else:
        source = source.replace("post: &[", "post: unknown![")
    with pytest.raises(ParseError):
        parse_levels(source)


def test_empty_levels_and_modules_fail():
    with pytest.raises(ParseError):
        parse_levels("")
    with pytest.raises(ParseError):
        parse_modules("// pub mod ignored;")
    with pytest.raises(ParseError):
        parse_modules("#[cfg(feature = \"changed\")] pub mod conditional;")


@pytest.mark.parametrize("title", ["Notable Updates", "Latest Updates", "Latest update — August 13th, 2026"])
def test_historical_readme_titles(title):
    assert parse_updates(f"## {title}\nupdate\n## Other\nignored") == f"## {title}\nupdate"
    with pytest.raises(ParseError):
        parse_updates("# A different README")


def test_count_guard():
    guard_count(100, 50, "example")
    with pytest.raises(ParseError):
        guard_count(100, 49, "example")
    with pytest.raises(ParseError):
        guard_count(0, 0, "example")


def expansion_sources():
    return {
        "expansion_text": (FIXTURES / "expansion-07d159b.rs.txt").read_text(encoding="utf-8"),
        "tweet_rules_text": (FIXTURES / "reacted-to-rules-07d159b.rs.txt").read_text(encoding="utf-8"),
    }


def test_actual_reacted_to_expansion_preserves_rule_order():
    source = (FIXTURES / "levels-07d159b.rs.txt").read_text(encoding="utf-8")
    levels, style = parse_levels(source, **expansion_sources())
    hydration = levels["TIMELINE_HOME_HYDRATION"]
    assert style == "PostRules+Expansion"
    assert hydration[:2] == ("ERASED_AUTHOR_DROP_INACTIVE", "DEACTIVATED_AUTHOR_DROP")
    assert hydration[6:9] == (
        "ORIGINAL_TWEET_ADULT_MEDIA_PROPAGATION_BLUR_SENSITIVE",
        "ORIGINAL_TWEET_GORE_MEDIA_PROPAGATION_BLUR_VIOLENCE",
        "FOSNR_HATEFUL_CONDUCT_DROP_UNDESIRABLE",
    )
    assert hydration[-1] == "PROTECTED_ORIGINAL_AUTHOR_DROP"
    assert len(hydration) == len(set(hydration))


@pytest.mark.parametrize("problem", ["missing_source", "changed_placement", "unknown_expansion", "wrong_length", "missing_parent", "unknown_action"])
def test_expansion_rejects_unknown_or_incomplete_dependencies(problem):
    source = (FIXTURES / "levels-07d159b.rs.txt").read_text(encoding="utf-8")
    dependencies = expansion_sources()
    if problem == "missing_source":
        dependencies["expansion_text"] = None
    elif problem == "changed_placement":
        dependencies["expansion_text"] = dependencies["expansion_text"].replace("!withholds", "withholds")
    elif problem == "unknown_expansion":
        source = source.replace("Expansion::ReactedTo", "Expansion::Unknown")
    elif problem == "wrong_length":
        source = source.replace("TIMELINE_HOME_HYDRATION_OWN.len()", "999")
    elif problem == "missing_parent":
        source = source.replace("const TIMELINE_HOME_HYDRATION_OWN:", "const OTHER:")
    else:
        dependencies["tweet_rules_text"] = dependencies["tweet_rules_text"].replace("drop_post(", "unknown_action(")
    with pytest.raises(ParseError):
        parse_levels(source, **dependencies)


def family_sources():
    return {
        "expansion_text": (FIXTURES / "expansion-606b254.rs.txt").read_text(encoding="utf-8"),
        "tweet_rules_text": (FIXTURES / "reacted-to-rules-07d159b.rs.txt").read_text(encoding="utf-8"),
    }


def test_actual_family_levels_flatten_in_declaration_order():
    source = (FIXTURES / "levels-606b254.rs.txt").read_text(encoding="utf-8")
    levels, style = parse_levels(source, **family_sources())
    assert style == "PostRules+Families+Expansion"
    assert len(levels["QUOTED_TWEET_RULES"]) == 8 and levels["QUOTED_TWEET_RULES"][0] == "DEACTIVATED_AUTHOR_DROP"
    # 命名规则族与内联规则族按声明顺序拼接。
    assert levels["TIMELINE_HOME"][:5] == ("SUSPENDED_AUTHOR_DROP", "DEACTIVATED_AUTHOR_DROP", "ERASED_AUTHOR_DROP_INACTIVE", "OFFBOARDED_AUTHOR_DROP_INACTIVE", "PROTECTED_AUTHOR_DROP")
    assert levels["TIMELINE_HOME"][5:7] == ("VIEWER_BLOCKS_AUTHOR_DROP", "VIEWER_MUTES_AUTHOR_DROP")
    assert set(levels["TIMELINE_HOME"]) < set(levels["TIMELINE_HOME_RECOMMENDATIONS"])
    hydration = levels["TIMELINE_HOME_HYDRATION"]
    assert hydration[6:8] == ("ORIGINAL_TWEET_ADULT_MEDIA_PROPAGATION_BLUR_SENSITIVE", "ORIGINAL_TWEET_GORE_MEDIA_PROPAGATION_BLUR_VIOLENCE")
    assert hydration[-1] == "PROTECTED_ORIGINAL_AUTHOR_DROP"


@pytest.mark.parametrize("problem", ["unverified_expansion", "missing_source", "unknown_family", "wrong_length", "duplicate_rule", "wrong_family_type", "unknown_const"])
def test_family_levels_reject_unverified_syntax(problem):
    source = (FIXTURES / "levels-606b254.rs.txt").read_text(encoding="utf-8")
    dependencies = family_sources()
    if problem == "unverified_expansion":
        dependencies["expansion_text"] = dependencies["expansion_text"].replace("!withholds", "withholds")
    elif problem == "missing_source":
        dependencies["expansion_text"] = None
    elif problem == "unknown_family":
        source = source.replace("    HOME_BLURS,\n", "    HOME_BLURS_MISSING,\n", 1)
    elif problem == "wrong_length":
        source = source.replace("rule_count(TIMELINE_HOME_HYDRATION_OWN)", "rule_count(FOSNR_DROPS)")
    elif problem == "duplicate_rule":
        source = source.replace("&[&NULLCASTED_TWEET_DROP, &STALE_TWEET_DROP_UNSPECIFIED],", "&[&NULLCASTED_TWEET_DROP, &NULLCASTED_TWEET_DROP],", 1)
    elif problem == "wrong_family_type":
        source = source.replace("const HOME_BLURS: &[&Rule]", "const HOME_BLURS: &[Rule]", 1)
    else:
        source = source.replace("pub(super) const FILTER_ALL: PostRules", "pub(super) const FILTER_ALL: Unknown", 1)
    with pytest.raises(ParseError):
        parse_levels(source, **dependencies)
