# 方案逐项核对

维护日期：2026-10-22。证据以当前文件、测试和外部状态为准；“本地通过”不等于“上线完成”。

| 方案要求 | 当前证据 | 结论 |
| --- | --- | --- |
| P0 公开 x-algo-watch 仓库 | 用户已授权；loonslo/x-algo-watch 已公开推送 | 已完成 |
| Actions 默认只读、secret scanning、push protection | 真实 GitHub API 均返回已开启/read | 已完成 |
| Python 3.14、仅 PyYAML、固定哈希 | pyproject.toml、两个 requirements 文件；Windows/Linux wheel 哈希下载验过 | 本地通过 |
| P1 逐提交发现/抽取/分级/记录 | 118 项测试中的真实历史回放与失败用例；50 个提交、49 次比较 | 本地通过 |
| P1 已知冷启动、开关、权重、视频去重、新参数与过滤器 | tests/test_history.py 的具体旧→新断言 | 本地通过 |
| P1 未知语法报红、两次格式迁移、规则差额 26→27 | 真实夹具、模拟旧解析器、实际对比测试 | 本地通过 |
| P1 去重、上游 HEAD/工作区不变、不执行上游代码 | 两轮逐字比较；Git 对象读取；tests/test_history.py | 本地通过 |
| P1/P2 失败不能当无变化 | 网络/解析失败测试、工作流最后报告不完整 | 本地通过，待真实运行 |
| P2 每 3 小时、main、并发、官方 action 提交固定 | .github/workflows/check.yml 与安全结构测试 | 文件与本地断言通过 |
| P2 detect→publish 隔离、写入白名单、扫描、幂等 Issue | automation.py / github.py；边界测试与 Linux CI 120 项通过；真实状态提交与 Issue 更新 | 测试通过，真实正常发布与同一 Issue 更新通过 |
| P2 手动正常运行与历史测试 Issue | acceptance-p2-p4.md 中的成功 run、状态提交与测试 Issue #1；摘要旧/新数值已核实 | 已运行通过 |
| P2 Issue 通知到达 | 网页保存 Custom → Issues；所有者明确确认收到指定测试 Issue #3 的邮件 | 邮件送达通过；手机未验 |
| P2 连续 24 小时定时检查 | 目前只有北京时间 06:23 的一个成功 schedule 运行；03:17、09:17 无对应运行 | 未通过，继续观察 |
| P3 三份资料一次性转换、代码事实 claim、开关与忽略初稿 | registry/facts.yaml 420 项；migrations.yaml 的源摘要与覆盖率 | 本地生成并核验，待所有者审阅初稿 |
| P3 29 行迁移一致 | tests/test_registry_migration.py 独立预期区间 | 全部通过 |
| P3 26→27、10-16→10-19 | 真实历史定值断言 | 通过 |
| P3 人工类只登记、不伪装核对 | last_checked=null、unreviewed；monthly 刷新逻辑 | 登记完成，内容未核对（方案允许） |
| P4 固定免费模型的两个调用角色 | 所有者已明确将不同型号要求改为同一 MiniMax；实际两项起草/复核调用通过，草稿与审计标注 same_model | 真实同模型链路通过；不宣称异模型独立复核 |
| P4 专用环境、main-only、只有 draft 拿 key | check.yml、安全测试、实际环境 main 分支规则；所有者已存密钥，审计 10 项全通过 | 配置完成，模型已启用 |
| P4 8 步处理与数字/证据校验、一次纠错、复核调用、程序组稿 | 实际 README 分析被数字校验拦截，纠错一次后复核通过并发评论；旧记录计算上下文缺失已修复 | 真实流程通过；含完整计算行的草稿继续复验 |
| P4 不重复调用、留痕、额度用尽只发证据、下轮续写 | 事件审计、调用计数、续跑测试；真实 HTTP 429 / 404 仅发证据，MiniMax 分析已缓存 | 本地通过，真实降级通过；真实复核续写待验 |
| P4 所有者编辑机器人评论后确认，其他人不动 | confirmation.py；确认、驳回、假作者、冲突、重复事件测试 | 本地通过，真实工作流未验 |
| 草稿不直接改手册、不自动改事实、确认后才入 digest | 数据流与端到端测试、文件白名单 | 本地通过 |
| README、LICENSE、AGENTS、PROJECT_STATUS、部署与回滚记录 | 根目录文件与 docs/deployment.md | 文件已提供 |
| 不含私钥、密钥、本机路径、私人邮箱 | 产物 public_text 扫描、假 key 拦截；Git 采用 noreply 身份 | 生成数据已扫描；发布前仍检查实际暂存内容 |

完整目标仍为方案最终运行状态，未因本地代码完成而缩小为“只通过测试”。上线缺口必须在授权发布后补足真实证据。
