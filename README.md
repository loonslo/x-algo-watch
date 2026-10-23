# x-algo-watch

结合 [xai-org/x-algorithm](https://github.com/xai-org/x-algorithm) 的项目架构、版本差异和调用位置，让模型研究推荐算法可能改了什么、为什么这样推测，以及哪些内容或创作者可能受影响。

**[查看全部版本变化与通俗解读 →](versions/README.md)**

主要面向普通创作者与运营：先读“30 秒摘要：本次更新重点”和“具体调整与影响解读”，技术分析与研究证据折叠在末尾；技术读者可从每页顶部进入本版本的技术专页，直接查看完整分析。两页来自同一研究。每个上游提交一篇，新版本排在最上面。详见[怎么读这些文档](docs/how-to-read.md)。

- [最新可读解读：989fb91 的 V2 已通过三个审核角色，待人工确认](versions/2026-10-22-989fb91254d461bf4b9df53fb12d5c9a61c2b354.md)
- [原速报样例：两项权重同时调整，比例不变（已人工确认）](versions/2026-10-14-17c96bf346df3d62b90f1bb01830d432dba74485.md)
- [只看已人工确认的速报汇总](digest.md)

“待模型研究”表示当前只有源码证据，不能算已完成解读。后续模型推测须通过格式、数字、引用校验及三轮模型复核后才可直接阅读，明确标注尚未人工确认；历史页面保留当时的复核方式。正式速报仍须所有者确认。原有固定模板折叠为证据阅读提示，不能冒充模型分析。

后续线上起草与复核共用[固定输出协议](docs/reader-output-contract.md)，必须生成用户阅读层并检查四问、默认条件与行动边界。程序校验后统一排版；缺项会自动纠错一次，仍不合格或复核分歧未解决时，用户正文标为待修订。新版本无需人工另写一份阅读稿。

现在按[可恢复循环](docs/acceptance-iteration-loop.md)执行：每版候选经过三个审核角色，有问题就依据证据修订并重审，默认最多 5 个候选。接口、时间或预算中断从缺少阶段续跑；达到修订边界或连续无进展则展示原因，处理后指定提交并勾选工作流 `resume_iterations` 恢复，保留全部历史。两页及目录显示进度，暂停不会冒充通过，正式速报仍需人工确认。

此前接口错误误报为敏感内容的问题已修复。本次继续调整证据准备、阅读层定点纠错、排队任务来源和新规则表解析；所有测试与真实模型调用均在 GitHub 执行，311 项回归及新规则表隔离检测已通过，模型质量仍以实际三角色结果为准。见[流程调整验收](docs/acceptance-flow-adjustments.md)。

[最新流程调整验收](docs/acceptance-flow-adjustments.md)：989fb91 的 V2 经修订和阅读层定点修补，真实三个角色均同意；[模型原文与修补轨迹](docs/samples/2026-10-23-adjusted-flow.md)已保存。仍待所有者检查事实边界与可读性，不能以模型同意替代人工验收。此前 7f2ca2b 的三轮分歧保留在[原样例记录](docs/acceptance-live-reader-sample.md)。

目前已实现 **P1–P4 的代码与测试**及整版本项目研究，公开仓库已部署免费路由、三角色复核和可恢复修订循环，日上限 1,000 次。最新 GitHub CI **311 项通过**，包含真实历史回放；07d159b 的新规则表隔离检测成功，生产状态等待正常检查重核。989fb91 的 V2 已在 GitHub 经真实修订和定点补丁达到三角色通过，但用户层的事实边界、普通创作者可读性仍待人工验收，其他历史研究及连续 24 小时定时可靠性也未全部验收。正式速报仍须所有者确认，详见[流程调整验收](docs/acceptance-flow-adjustments.md)。

`registry/facts.yaml` 有 460 项代码事实、开关、忽略初稿、来源依赖和人工登记，另有 29 行独立行号迁移样本。[74 条源码说法的依赖清单](registry/code-claim-coverage.yaml)与[15 个非源码核对主题](registry/manual-coverage.yaml)逐项保留定位与摘要。只指定文件/目录的来源采用保守的完整对象摘要监测，不自动证明说法成立。外部资料的人工核对日期留空，避免把资料创建日期写成已经核对。

## 运行

使用 Python 3.14 和 Git。在本项目根目录执行：

```powershell
python -m pip install --require-hashes -r requirements.txt
python -m pip install --require-hashes -r requirements-dev.txt
python -m x_algo_watch --help
```

项目本身通过 `python -m x_algo_watch` 运行，无需安装包或启动服务。运行依赖只有 PyYAML；pytest 及其依赖仅供测试。锁文件包含 Windows/Linux x86_64 所需的分发哈希。

### 比较两个提交

```powershell
python -m x_algo_watch compare --repo ../x-algorithm --from 862a123 --to 74d49a4 --output runtime/comparison
```

### 完整历史回放

```powershell
python -m x_algo_watch replay --repo ../x-algorithm --from 47c1bcd --to 7f2ca2b --output runtime/replay
```

`--from` 包含基线；随后按拓扑顺序处理范围内每一个提交，各自与其第一父提交比较。合并提交也处理，不会将外部分支误作主线相邻版本而制造虚假的回退。上述范围包含 50 个提交、49 次变更比较。

### 离线增量检查

```powershell
python -m x_algo_watch check --repo ../x-algorithm --to refs/remotes/origin/main --baseline 7f2ca2b --output runtime/check
```

读取本机已有 Git 对象，不会更新本机上游引用。首次须明确基线；以后从检查记录恢复进度。登记表改变时，即使上游提交不变也会重新核验；人工核对到期状态可在没有新提交时刷新。

`check` 不传 `--repo` 时会先用 `ls-remote` 查询固定上游 main；没有新提交且登记表未变，就更新当日状态，有变化才临时裸克隆。这个联网分支由 P2 工作流使用。检测程序不读取模型密钥。

### 单独重核登记表

```powershell
python -m x_algo_watch verify --repo ../x-algorithm --to 7f2ca2b --output runtime/verification
```

登记表的原始值不会被程序覆盖；重核事件追加到月记录，核验状态另存。人工维护事实，禁止把新上游值自动写回登记值而掩盖变化。

## 输出与行为

- `versions/README.md`：给读者看的版本目录，按上游提交时间倒序排列。
- `versions/日期-完整提交号.md`：每次迭代的独立模型机制研究、推测及证据。模型结果未完成时明确显示待研究；人工确认后同步获批原文。没有新提交的定时检查不新增版本文件，但会续写研究队列。
- `data/project-context/完整提交号.json`：模型实际读取的上下文，包括架构、跨模块差异、实现、引用窗口及覆盖/截断信息。
- `records/YYYY-MM.md`：按**检查发生月份（UTC）**保存的只追加记录，包含中文摘要和带版本的 JSON 证据。成功记录是进度唯一来源；模型调用、重核和确认以事件追加。
- `status.md`：当前进度、当日检查状态、待复核提交数、过期人工核对项；可重建，无新提交时同一天不反复改写。
- `verification.yaml`：当前目标提交的核验状态及自动开关观察表。失败时 `complete: false`，成功进度保持原值。

默认输出到 `data/`；测试和探索请显式使用 `runtime/` 下的独立目录。项目自带的 `data/` 包含 P1 历史回放及后续登记表重核，部署沿用 7f2ca2b 为生产进度；历史测试不推进该进度。不同回放范围不要混用同一输出目录。“待复核”扣除已记录的确认或驳回决策；历史未处理的红橙提交仍保留供追溯。

解析器支持已核实的 `param!`、过滤器模块，以及 `levels!` / `&[&Rule]` / `PostRules` 三种级别表。未知语法、空结果、数量骤降、已存在文件丢失均报红并停在失败提交，退出码为 2；已完整检查的红橙结果退出码仍为 0。下轮从失败提交补跑。网络失败写“本次未检查”，不会写成“无变化”。同一提交重复运行不重复追加。

`levels.rs` 在 `e1a0375` 才引入。它之前的真实祖先明确记录为旧布局，不提供级别名单计数；这不是现代缺文件的豁免。README 支持历史已核实的 `Latest update`、`Latest Updates`、`Notable Updates` 标题。golden corpus 只提供灰色佐证。忽略名单必须有理由，人工登记的事实和开关优先于名字分类。按名字匹配的配套开关仅为粗分依据，不能证明真实控制关系。

## 模块职责

| 模块 | 职责 |
| --- | --- |
| `git.py` | 发现远程提交；只读 Git 对象、历史、文本 diff |
| `parsers.py` | 严格解析已知 Rust/README 写法，拒绝未知表达式 |
| `classify.py` | 参数类别、默认状态、登记项和忽略理由的分级 |
| `registry.py` | 取值、计数、原文与人工到期核验，拒绝重复 YAML 键 |
| `engine.py` | 快照比较、文件证据、派生规则数量、失败边界 |
| `storage.py` | 防泄漏检查、原子写入、去重、从记录恢复进度 |
| `cli.py` | 比较、回放、增量流程；临时克隆生命周期 |
| `automation.py` / `github.py` | 数据产物校验、白名单发布、幂等 Issue 与评论 |
| `evidence.py` / `drafting.py` | 精确数字、证据编号、逐项校验与复核、额度续跑 |
| `project_evidence.py` / `research.py` | 构建整版本项目上下文；校验并展示算法机制、条件性影响及替代解释 |
| `confirmation.py` | 仅所有者标签、机器人草稿、追加速报与决策审计 |
| `versions.py` | 将已存检查证据整理成版本目录与每次迭代的独立阅读档案，确认后同步人工解读 |
| `migrate.py` | 一次性只读资料迁移工具，不属于定时程序 |
| `deployment.py` | 只读核查 GitHub 权限、环境与启用条件 |

检测程序不会修改上游工作区或执行上游代码，不写手册文件。只有获授权部署后的 publish / confirm 工作流可向本项目 main 写入白名单数据。输出目录禁止位于上游副本内。check 与 confirm 使用同一并发组；程序只对公开文本进行检查，无法证明线上部署或规则语义。

## 模型与发布

按用户最新选择，所有角色均使用 `openrouter/free` 免费自动路由，具体配置见 `config/watch.yaml`。当前 `project_research: true`；起草后依次执行综合复核、源码事实与遗漏核对、机制推断与读者理解核对（`extra_review_rounds: 2`）。每轮只看原始证据和同一稿件，不参考其他裁决；全部同意才发布正文，任何异议都不能被多数票覆盖。实际型号可能相同，附录记录接口返回型号，不宣称一定是不同模型交叉验证。

正常每版 4 次调用；有语义分歧时最多修订一次，修订后重新跑全部三轮，无结构重试时共 8 次。每阶段结构校验失败只纠错一次。每轮最多研究 3 版，每日共用 1,000 次项目调用额度（包含失败与旧型号调用），按 UTC 日期计数，北京时间每日 08:00 重置；切换路由或调整上限不清空已用计数。额度不足、请求延期或主动运行达到 20 分钟时保留审计与队列，下轮从已完成阶段续写；未完成全部复核或仍有分歧则不发布用户结论。正式速报仍须所有者编辑并确认。

部署默认关闭模型；仅 draft 任务使用 `openrouter` 环境，仅模型调用步骤拿密钥。模型 key 由所有者直接存入 GitHub，不能放到本机、聊天或项目文件。发布与回滚步骤见 [部署说明](docs/deployment.md)。

## 测试与验收

```powershell
python -m pytest -c pyproject.toml tests -q
python -m pytest -c pyproject.toml tests -q --upstream-repo=../x-algorithm
```

第一条运行可独立复现的单元和合成 Git 集成测试，跳过需要真实上游的历史验收。第二条额外执行完整历史回放，并检查上游 HEAD、工作区状态保持不变。也可用 `X_ALGO_UPSTREAM` 提供上游路径。Windows 若默认临时目录不可访问，用 `--basetemp=runtime/pytest-本次唯一名称` 指定新的项目内测试目录。

详细结果见 [P1 历史验收](docs/acceptance-p1.md)、[后续阶段验收](docs/acceptance-p2-p4.md)、[逐版本档案验收](docs/acceptance-versions.md)、[项目机制研究验收](docs/acceptance-project-research.md)、[免费路由与多轮校验](docs/acceptance-free-router-reviews.md)、[需求核对](docs/requirements-audit.md) 和 [项目状态](PROJECT_STATUS.md)。CI 在 Linux / Python 3.14 运行相同测试，只有实际工作流结果才能证明线上通过。

## 公开与许可

本项目与 X、xAI 无关，属于非官方独立项目。代码默认值不等于线上实际取值，解释未经平台确认。登记和检测结果用于辅助复核，不直接作为运营结论；手册和速报须人工判断。

本项目采用 [Apache-2.0](LICENSE)。测试夹具和生成证据引用自采用相同许可证的 `xai-org/x-algorithm`，来源文件、提交号列在 [NOTICE](NOTICE) 中；生成记录按提交与路径保留来源。未复制手册内容。
