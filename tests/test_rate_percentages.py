from copy import deepcopy

import pytest

from x_algo_watch.evidence import configuration_rate_percentages
from x_algo_watch.drafting import validate_analysis
from x_algo_watch.project_evidence import project_pack
from x_algo_watch.research import analysis_projection
from tests.test_project_research import analysis, prepare


def rate_record(entry):
    return {"findings": [{"category": "parameter", "path": "home-mixer/params/param.rs",
                         "name": "ShadowTrafficPhoenixClusterRates", "after": [entry]}]}


def test_config_rate_conversion_is_exact_and_not_an_online_measurement():
    result = configuration_rate_percentages(rate_record("Experiment18Fou:0.5"))
    assert result[0]["configured_rate"] == "0.5"
    assert result[0]["configured_percent"] == "50"
    assert "不证明线上" in result[0]["meaning"]
    assert configuration_rate_percentages(rate_record("Group:0"))[0]["configured_percent"] == "0"
    assert configuration_rate_percentages(rate_record("Group:1"))[0]["configured_percent"] == "100"


@pytest.mark.parametrize("entry", ["Group:1.1", "Group:-0.5", "Group:NaN", "Group:0.5:extra", 0.5])
def test_unsupported_rate_entries_are_not_derived(entry):
    assert configuration_rate_percentages(rate_record(entry)) == []


def test_weights_are_not_automatically_converted_to_percentages():
    record = rate_record("Group:0.5")
    record["findings"][0]["name"] = "FavoriteWeight"
    assert configuration_rate_percentages(record) == []


def test_derived_rate_allows_exact_conversion_without_relaxing_numeric_guard(sample_repo):
    repo, result = prepare(sample_repo)
    record = result.to_dict()
    record["findings"].extend(rate_record("Experiment18Fou:0.5")["findings"])
    pack = project_pack(repo, record)
    value = analysis(pack)
    value["summary"] += " 配置采样率为 50%，线上状态未知。"
    assert not validate_analysis(analysis_projection(value), pack, project_research=True)
    unsupported = deepcopy(value)
    unsupported["summary"] = unsupported["summary"].replace("50%", "51%")
    assert "数字不在证据包：51" in validate_analysis(analysis_projection(unsupported), pack, project_research=True)
