import pytest

from x_algo_watch.migrate import manual_topics
from x_algo_watch.models import WatchError
from x_algo_watch.storage import public_text


SOURCE = """### 2.3 源码不可核（15 条，保留原标注）
收益资格共 7 条。删帖重发、作者回复、搜索入口、Premium、社区笔记与收益、定时发布、抽奖与品牌合作、AI 生成内容。
## 3. 后续
"""

CHAPTER = """## 3. 合成收益章节
### 3.1 准入
| 条件 | 要求 | 说明 |
| --- | --- | --- |
| 订阅 | PRIVATE_POLICY_SENTINEL | 待核对 |
| 粉丝 | 234567 | 待核对 |
| 展示 | 数值不公开 | 待核对 |
| 地区与年龄 | 待核对 | 待核对 |
来源：https://help.x.com/en/using-x/original-content-rewards
### 3.2 统计
1. 合成的帖子口径
2. 合成的入口口径
3. 合成的观众口径
### 3.3 不登记的例子
只作运算示意。
"""


def test_manual_coverage_registers_only_named_topics_and_keeps_seven_gaps():
    facts, coverage = manual_topics(SOURCE, "a" * 40)
    assert len(facts) == 8
    assert len({f["id"] for f in facts}) == 8
    assert all(f["check"] == "manual" and f["value"] is None and f["manual_review"]["last_checked"] is None for f in facts)
    assert all(f["evidence"] == "D" for f in facts)
    assert len(coverage["unresolved_items"]) == 7
    assert len(coverage["registered_noncode_topics"]) + len(coverage["unresolved_items"]) == coverage["expected_noncode_items"]
    assert len(coverage["existing_source_entry_ids"]) == 3
    public_text(str(facts) + str(coverage))


@pytest.mark.parametrize("source", [SOURCE.replace("15 条", "14 条"), SOURCE.replace("共 7 条", "共 8 条"), SOURCE.replace("搜索入口", "未知类别")])
def test_changed_manual_source_cannot_silently_claim_complete_coverage(source):
    with pytest.raises(WatchError):
        manual_topics(source, "a" * 40)


def test_authorized_revenue_topics_complete_registration_without_copying_policy():
    facts, coverage = manual_topics(SOURCE, "a" * 40, CHAPTER)
    assert len(facts) == len(coverage["registered_noncode_topics"]) == 15
    assert len({fact["id"] for fact in facts}) == 15
    assert coverage["unresolved_items"] == []
    assert len(coverage["revenue_provenance"]["items"]) == 7
    assert coverage["revenue_provenance"]["external_content_verified"] is False
    assert all(f["value"] is None and f["manual_review"]["last_checked"] is None for f in facts)
    assert [f["used_in"] for f in facts[-7:]] == [["手册 3.1"]] * 4 + [["手册 3.2"]] * 3
    serialized = str(facts) + str(coverage)
    assert "PRIVATE_POLICY_SENTINEL" not in serialized and "234567" not in serialized
    public_text(serialized)


@pytest.mark.parametrize("chapter", [CHAPTER + "\n## 4. 不在授权内\n", CHAPTER.replace("3. 合成的观众口径", "4. 结构变化"),
                                    CHAPTER.replace("地区与年龄", "未知条件"), CHAPTER.replace("original-content-rewards", "other")])
def test_revenue_source_drift_or_extra_chapter_is_rejected(chapter):
    with pytest.raises(WatchError):
        manual_topics(SOURCE, "a" * 40, chapter)
