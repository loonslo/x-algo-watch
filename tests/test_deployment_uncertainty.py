import pytest

from tests.test_project_research import analysis, prepare
from x_algo_watch.drafting import validate_analysis
from x_algo_watch.project_evidence import project_pack
from x_algo_watch.research import analysis_projection


@pytest.mark.parametrize("text", ["线上是否已生效未核实。", "线上是否已上线？", "线上是否全量启用仍未核实。"])
def test_explicit_deployment_question_is_not_an_assertion(sample_repo, text):
    repo, result = prepare(sample_repo)
    pack = project_pack(repo, result.to_dict())
    value = analysis(pack)
    value["summary"] += text
    assert not validate_analysis(analysis_projection(value), pack, project_research=True)


@pytest.mark.parametrize("text", ["平台确认已生效。", "线上是否已生效未核实。平台确认已上线。"])
def test_question_does_not_exempt_a_separate_affirmative_claim(sample_repo, text):
    repo, result = prepare(sample_repo)
    pack = project_pack(repo, result.to_dict())
    value = analysis(pack)
    value["summary"] += text
    assert "包含无依据的确定性或部署表述" in validate_analysis(analysis_projection(value), pack, project_research=True)
