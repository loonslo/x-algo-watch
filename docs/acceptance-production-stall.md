# 生产停滞诊断与修复验收（2026-10-31）

## 现象

- 自 2026-10-29 起所有定时「Check upstream algorithm」运行均为 failure（例：41178820764、41141641869、41108617747、41043979862、40988935662）。
- `data/status.md` 停在 5ed7963，本次状态为「解析失败，进度未推进」，待复核提交数不再减少，没有新的用户解读。

## 根因（两处，均由只读下载的失败运行产物与上游 Git 对象证实）

1. **解析器不认识新写法。** 上游 606b254 把 `visibility-filtering/rules/levels.rs` 重构为「规则族」写法（`&[&[&UserRule]]`、命名规则族拼接、`placed` 接收规则族列表），`QUOTED_TWEET_RULES` 因未知常量类型被判解析失败。程序按设计停止推进，因此 606b254 起三个上游提交（606b254、7fef3ba、9106e9f）全部排队，每轮重复同一失败。
2. **项目上下文对被删除文件误判。** 5ed7963 删除了 `home-mixer/filters/brazil_2026_election_filter.rs`；`project_pack` 把差异中的删除路径当作「必需的核心实现」，在新提交读到空文本，报「核心实现无法完整放入证据预算」。结果每轮追加一条 `research_context_deferred`，该提交永远没有研究上下文。

## 修改

- `x_algo_watch/parsers.py`：新增 `parse_family_levels`。只接受已核实的表达式：命名/内联规则族、`PostRules{user,post}` 由规则族按序拼接、`Expansion::ReactedTo.placed(OWN)` 且类型长度表达式与来源一致。新 `Expansion` 实现以新的去注释/空白摘要核实（仅 `placed` 的父表参数改为规则族，放置顺序不变）；摘要不符、来源缺失、规则族引用缺失/类型不符、规则重复、未知常量类型仍拒绝。旧三种写法的解析路径不变；展开放置逻辑抽为 `place_reacted_to` 供新旧共用。
- `x_algo_watch/project_evidence.py`：核心实现候选只含目标提交树中存在的文件；删除文件的证据仍由差异提供。

## 验证（实际执行，Windows / Python 3.14，只读克隆上游 main）

- 解析 5ed7963 / 606b254 / 7fef3ba / 9106e9f：均成功；5ed7963→606b254 所有既有级别的规则序列逐项一致（上游为行为保持的重构）。
- 以线上失败运行的数据副本（位于 runtime，不入库）执行 `check --baseline … --repo <本机上游裸克隆> --to refs/heads/main`：3 条新记录均 complete，状态「检查完成」，最后处理 9106e9f。
- `project_pack` 对 5ed7963 / 606b254 / 7fef3ba / 9106e9f 均可生成。
- 新增测试：606b254 真实 levels/expansion 夹具的拼接顺序与 7 类拒绝场景；删除过滤器文件的回归（修复前以原错误失败，修复后通过）。
- 完整测试 464 passed、24 skipped（无上游副本）；带 `X_ALGO_UPSTREAM` 的历史相关 5 个测试文件 72 passed。

## 未验证

- 未推送、未触发 GitHub Actions；线上是否恢复须推送后观察下一次定时运行。
- 未调用任何模型；3 个新提交与 31 个历史提交的研究质量未评估。
- 红、橙告警 Issue 在推送后的首次运行中会针对新记录创建，行为未在本机验证。
- 上游 `levels.rs` 再改写法仍会按设计红色停止，这是严格解析的预期行为，不是稳定性保证。
