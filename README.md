# x-algo-watch

只读检查 [xai-org/x-algorithm](https://github.com/xai-org/x-algorithm) 的公开源码，抽取变化、给出红橙黄灰级别，并保留可追溯的证据。

目前已实现 **P1–P4 的代码与测试**：检测、Actions 任务隔离、Issue 发布、事实登记、免费模型起草与复核、所有者标签确认。公开仓库已部署，Linux CI 144 项测试、手动正常检查、历史测试 Issue 与邮件通知均通过；按所有者选择由同一 MiniMax 型号分别调用起草与复核，真实草稿已生成并等待人工修订确认。连续 24 小时定时运行仍待验收。

`registry/facts.yaml` 有 420 项代码事实、开关、忽略初稿和人工来源登记，另有 29 行独立行号迁移样本。外部资料的人工核对日期留空，避免把资料创建日期写成已经核对。

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
| `confirmation.py` | 仅所有者标签、机器人草稿、追加速报与决策审计 |
| `migrate.py` | 一次性只读资料迁移工具，不属于定时程序 |
| `deployment.py` | 只读核查 GitHub 权限、环境与启用条件 |

检测程序不会修改上游工作区或执行上游代码，不写手册文件。只有获授权部署后的 publish / confirm 工作流可向本项目 main 写入白名单数据。输出目录禁止位于上游副本内。check 与 confirm 使用同一并发组；程序只对公开文本进行检查，无法证明线上部署或规则语义。

## 模型与发布

用户选择由 MiniMax M3.1 Flash 免费版承担起草、复核两个调用角色，固定 ID 及显式 `allow_same_model: true` 见 `config/watch.yaml`。两次调用分别使用起草、复核提示词；这是同模型复核，不属于不同模型的独立复核，草稿会标注。每日最多 50 次，校验失败只重试一次；额度不足时只留证据，下轮从已有有效结果继续。草稿只在机器人评论中出现，只有所有者编辑并确认后进入速报。

部署默认关闭模型；仅 draft 任务使用 `openrouter` 环境，仅模型调用步骤拿密钥。模型 key 由所有者直接存入 GitHub，不能放到本机、聊天或项目文件。发布与回滚步骤见 [部署说明](docs/deployment.md)。

## 测试与验收

```powershell
python -m pytest -c pyproject.toml tests -q
python -m pytest -c pyproject.toml tests -q --upstream-repo=../x-algorithm
```

第一条运行可独立复现的单元和合成 Git 集成测试，跳过需要真实上游的历史验收。第二条额外执行完整历史回放，并检查上游 HEAD、工作区状态保持不变。也可用 `X_ALGO_UPSTREAM` 提供上游路径。Windows 若默认临时目录不可访问，用 `--basetemp=runtime/pytest-本次唯一名称` 指定新的项目内测试目录。

详细结果见 [P1 历史验收](docs/acceptance-p1.md)、[后续阶段验收](docs/acceptance-p2-p4.md)、[需求核对](docs/requirements-audit.md) 和 [项目状态](PROJECT_STATUS.md)。CI 在 Linux / Python 3.14 运行相同测试，只有实际工作流结果才能证明线上通过。

## 公开与许可

本项目与 X、xAI 无关，属于非官方独立项目。代码默认值不等于线上实际取值，解释未经平台确认。登记和检测结果用于辅助复核，不直接作为运营结论；手册和速报须人工判断。

本项目采用 [Apache-2.0](LICENSE)。测试夹具和生成证据引用自采用相同许可证的 `xai-org/x-algorithm`，来源文件、提交号列在 [NOTICE](NOTICE) 中；生成记录按提交与路径保留来源。未复制手册内容。
