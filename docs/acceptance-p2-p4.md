# P2–P4 本地验收与上线缺口

维护日期：2026-10-22（北京时间）。本次为 P1 后的继续实现；早期 74 项测试的验收保留在 acceptance-p1.md，本文件不将早期结果冒充本次复测。

## 本次实际执行

环境：Windows / PowerShell / Python 3.14.0 / pytest 8.4.2 / PyYAML 6.0.3。

```powershell
python -m pytest -c pyproject.toml tests -q --upstream-repo=../x-algorithm --basetemp=runtime/pytest-next-final --tb=short
python -m compileall -q x_algo_watch tests
```

实际结果：**118 passed in 38.44s**；语法编译检查通过。测试包含 50 个真实历史提交的两轮回放、原工作区不变、登记表迁移，以及不访问真实账号或模型的合成端到端联调。

发布前只读审计曾显示目标仓库不可访问；该结果已被下方真实上线结果取代。模型密钥仍未由程序读取或保存到本机。

## 后续真实上线结果（2026-10-22）

用户明确授权后，已创建并公开发布 [loonslo/x-algo-watch](https://github.com/loonslo/x-algo-watch)。实际 API 已确认：public、默认 main、Actions 默认 read、secret scanning 和 push protection 均开启；openrouter 环境仅允许 main 分支，不允许标签。

- [初次 Linux CI](https://github.com/loonslo/x-algo-watch/actions/runs/39633480028)：118 项通过，9.63 秒。
- [历史测试工作流](https://github.com/loonslo/x-algo-watch/actions/runs/39633571851)：detect/publish 成功，draft 按配置跳过，已生成[测试 Issue #1](https://github.com/loonslo/x-algo-watch/issues/1)。
- [首次正常运行](https://github.com/loonslo/x-algo-watch/actions/runs/39633672871)：发现 publish 传入目录 data 时被过严的路径预校验拒绝。已最小修复，增加实际创建本地提交并模拟 push 的边界测试；令牌只在进程环境中，未进参数或 Git 配置。
- [修复后 Linux CI](https://github.com/loonslo/x-algo-watch/actions/runs/39634159335)：成功，包含上述新增回归。
- [修复后正常运行](https://github.com/loonslo/x-algo-watch/actions/runs/39634180762)：detect/publish 成功，真实状态提交 bce2f6d 已回到 main，进度仍为 7f2ca2b。
- 大型证据摘要原先会省略旧/新数值，已补保留 before/after、行号和计数；相关 10 项发布测试通过。
- [最新 Linux CI](https://github.com/loonslo/x-algo-watch/actions/runs/39634591048)：提交 f04f916 的 **120 项测试全部通过，8.96 秒**，包含目录发布与大型摘要的新增回归。
- [摘要修复后的历史测试](https://github.com/loonslo/x-algo-watch/actions/runs/39634645217)：成功；同一测试 Issue #1 已更新，实际正文保留参数 before/after、行号、权重输入和 26→27 的规则差额。

所有者已保存环境密钥，随后已设置 DRAFT_ENABLED=true。只读上线审计的 10 项配置检查全部通过（含环境密钥存在、仓库级无模型 key、main-only）；未读取密钥值。两个固定模型的官方列表均保留免费价格与 JSON 响应支持。Issue #3 邮件送达已由所有者确认；连续 24 小时定时与真实模型质量仍未完成。

密钥交接后的本地补写验证：`python -m pytest -c pyproject.toml tests -q --upstream-repo=../x-algorithm --basetemp=runtime/pytest-backfill-full --tb=short`，**126 passed in 42.89s**。覆盖保持原成功进度、使用既存证据、拒绝不完整/未登记/灰色记录与混合测试模式。改动文件公开内容扫描及 git diff --check 均通过。

- [补写入口 Linux CI](https://github.com/loonslo/x-algo-watch/actions/runs/39636378288)：**126 passed in 8.81s**。
- [首次真实模型样例](https://github.com/loonslo/x-algo-watch/actions/runs/39636392038)：detect/draft/publish 成功，但第一次 Xing4.0 请求为 HTTP 429，审计记录为 deferred；因此仅创建[证据 Issue #2](https://github.com/loonslo/x-algo-watch/issues/2)，标记待起草，无草稿。生产进度仍为 7f2ca2b，状态提交为 77357b4。工作流绿色仅表示降级流程成功，不能据此宣称模型质量通过。
- 为区分账户每日/每分钟限额与提供方拥堵，增加仅输出固定分类与数字 Retry-After 的错误记录，服务端原文不进入产物。相关模型与流程测试 **18 passed in 0.39s**，含服务端回显假 key 不泄露的用例。
- [错误分类 Linux CI](https://github.com/loonslo/x-algo-watch/actions/runs/39636820234)：**130 passed in 10.00s**。
- [受控重试](https://github.com/loonslo/x-algo-watch/actions/runs/39636841042)：detect/draft/publish 成功，仍为 HTTP 429；固定分类确认是“模型提供方暂时拥堵”。此次记录提交为 61e6f5d，仍使用同一 Issue #2，生产进度保持 7f2ca2b；共两次真实请求，均未生成草稿，尚未调用 NVIDIA 复核。后续由每 3 小时正常检查自动补写，不持续手动重试。

通知订阅的只读 API 因现有登录缺少 notifications scope 返回不可读，未扩展登录权限。2026-10-22（北京时间），所有者先反馈收到邮件，随后明确更正为看错；先前的邮件送达验收结论已撤回，恢复为待验证。程序没有直接发送邮件，仅通过 GitHub 创建 Issue。

随后只读查看 GitHub 通知页面，确认默认通知地址已设置、邮件渠道已开启；地址仅在当前对话中说明，不入仓库。仓库原先仅为 Participating and @mentions，现已改为 Custom → Issues 并保存，重新打开选项核实勾选。没有读取邮件。通过[通知复验工作流](https://github.com/loonslo/x-algo-watch/actions/runs/39712551083)创建[历史测试 Issue #3](https://github.com/loonslo/x-algo-watch/issues/3)，北京时间 10:39；draft 跳过，生产数据及 digest 不变。所有者随后对这条指定标题、指定 Issue 的邮件明确反馈“已收到这条邮件”，本次邮件送达验收通过；不再沿用先前 Issue #2 的错误反馈，也未验证手机渠道。

[首个定时运行](https://github.com/loonslo/x-algo-watch/actions/runs/39686123877)于北京时间 06:23:21 创建，流程成功，状态提交 797a5ac；本轮第三次 Xing4.0 请求仍为提供方 HTTP 429，保持待起草。此次查询 schedule 专项 API 的 total_count=1，03:17 与 09:17 未查到对应运行。连续 24 小时定时验收仍未通过，不以手动补跑代替。

[北京时间 10:42 的手动正常补跑](https://github.com/loonslo/x-algo-watch/actions/runs/39712839301)成功，数据提交为 49a9b37；生产进度仍为 7f2ca2b。第四次 Xing4.0 请求仍为提供方 HTTP 429，无有效草稿、未调用 NVIDIA。此次补跑不计作 schedule 记录。

在合成发布流程中复现草稿就绪仍同时保留“待起草”和“待确认”的标签问题。现只添加目标状态并逐一删除程序管理的过期状态，不整体替换标签；保留所有者标签、评论编辑与关闭状态。相关发布/确认/流程测试 **29 passed in 1.00s**，包含两个方向的状态切换与重复发布不重复写标签。

- [标签修复 Linux CI](https://github.com/loonslo/x-algo-watch/actions/runs/39713205601)：**132 passed in 10.28s**。
- [MiniMax 配置 Linux CI](https://github.com/loonslo/x-algo-watch/actions/runs/39713360460)：**132 passed in 5.78s**。
- [MiniMax 真实样例运行](https://github.com/loonslo/x-algo-watch/actions/runs/39713369683)：流程完成，MiniMax 对权重组的结构化分析 valid、数字与引用校验无错误；NVIDIA 复核请求为 HTTP 404 / deferred，因此没有发布草稿。审计已随 c539404 提交，MiniMax 的分析缓存可供下轮复用。
- 随后查询 OpenRouter 的 NVIDIA Nemotron 3 Super 免费模型公开 endpoints API，唯一端点的近 5 分钟、30 分钟、1 天可用率均为 0；未改变账户隐私设置或绕过路由限制。
- 真实分析正确描述旧/新值但遗漏倍率和不变比值，因此组稿现固定展示程序已计算的倍率及比值，不要求模型自由补算。组稿版本同时绑定实际正文，文字变化会产生新版本，同文重复不会覆盖所有者编辑。相关模型/发布/流程测试 **36 passed in 0.69s**，包括模拟模型完全未提比值的情况。

## P2：检测、发布与工作流

已实现并本地验证：

- check 的 UTC cron、手动历史测试、main 限制、任务只读/写入权限、共用并发组、官方 action 的 40 位提交固定。
- 检测与发布分开；产物限定为数据文件，带哈希清单。发布先核对 main 当前数据，再扫描敏感内容；拒绝产物篡改、过期覆盖或白名单外文件。
- 完整 SHA、机器人身份、测试/生产标记共同去重 Issue。关闭的不重开；相同草稿不覆盖所有者编辑。
- 检测数据和通知资格先持久化，Issue API 失败后下一轮仍补发；解析失败保留红色证据并使最终工作流报告失败。
- 网络失败为“本次未检查”，无新提交不会克隆；登记表改变仍会重核。只读上线审计不会把不可读的配置当作已通过。

真实 Linux、正常状态提交、历史测试 Issue 与 Issue #3 的邮件送达已完成，见上方证据。尚未验收：连续满 24 小时的定时结果。check 已包含可关闭的 P4 draft；MVP 0 模式运行的 detect/publish 不引用 secret，P4 仅 draft 引用专用环境密钥。

## P3：事实登记与引用迁移

已生成并核验 `registry/facts.yaml` 的 **420 项**：360 个普通事实、50 个开关、10 个有理由的实验字符串忽略初稿；其中 3 项为独立的人工外部来源入口。登记包含全部 198 个参数、过滤器和规则计数、实际候选截断常量、源码原文锚点。

指定三份资料的源码引用解析出 **179 个区间、180 个稳定片段**；路径全部解析，无未映射项。只从代码建立 claim，不复制手册原文或判断理由。`registry/migrations.yaml` 的 source_digests 留存资料内容摘要，不含本机路径或原始文稿。

29 行迁移样本逐项与指定报告中的预期区间比较，**全部一致**，含组合区间和原地不动的引用。`phoenix_source.rs` 的 128–168 跨过新增语句，拆成两段原文，独立计算首尾位置为 129–170；不把发生插入的整段宣称为逐字不变。同步日期行只用固定注释前缀定位，日期变化由专门 sync 信号核验。

真实 `862a123 → 74d49a4` 验证：规则差额 **26→27**，同步日期 **2026-10-16→2026-10-19**。重复片段不能随便取第一处，也不会因另一个文件出现相同片段而伪报“已迁移”。

核验结果中，代码事实与锚点全部成立；忽略项保留理由。3 个外部来源的 last_checked 为 null、状态 unreviewed，没有伪造人工核对日期；这些政策内容未验证。忽略初稿与章节关联仍应由所有者审阅，不代表平台认可解释。

## P4：模型、复核与确认

当前两个固定免费模型写入 config/watch.yaml：MiniMax M3.1 Flash 起草、NVIDIA Nemotron 3 Super 复核。Xing4.0 四次请求均为提供方 HTTP 429 后，所有者已明确选择改用 MiniMax 起草；仅改变固定模型配置，未增加随机或付费回退。新的真实调用结果待补录。

本地合成录制响应与端到端联调已验证：

- 数字由 Decimal 精确计算：10-14 的两项权重倍率都是 1.1，绝对比值 79.2 保持不变；模型不能自由计算新数。
- 一项一包，E 编号证据，固定版本提示词，JSON 字段与枚举校验。故意错数、名字、路径、证据编号、默认关误判和无依据的上线承诺均被拦住。
- 第一次失败携校验错误重试一次；两次失败转需人工。换另一个模型复核，分歧明确标注。调用的模型、提示词版本、输入摘要、原文输出及校验结果追加到检查记录。
- 相同输入/提示词/模型不重复分析；额度不足只留证据，下一轮从通过的分析继续复核，不重新消耗分析调用。每天最多 50 次，调用间隔至少 3.1 秒。
- 草稿由程序组稿，正文只在机器人评论中出现；记录里保存结构化调用审计。未独立复核的内容不发成已起草结论。
- 非所有者、无关标签、人类冒充机器人评论、待补项、冲突标签与假 key 均不能写入 digest。所有者编辑后的机器人草稿可追加一次，保留原修改，按北京时间填确认日期。
- 先提交速报与决策再关闭 Issue；重复事件不重复追加。驳回只留决策，不进速报。历史测试 Issue 不写生产 digest。

openrouter 的 main 限制与密钥存在已通过实际 API 核验。已增加手动 backfill_commit 入口，仅允许已有完整红橙记录；保持最后成功检查位置，沿用调用审计和确认流程，用于方案允许的历史样例补写。尚未验收：真实免费模型可用性和质量、真实评论编辑与标签工作流。模型 JSON 测试夹具为合成回答，不能据此宣称真实模型质量通过。

## 交付边界

代码、工作流、登记表、提示词、测试和发布/回滚说明均已准备。公开发布、平台配置和通知仍须用真实外部状态验证；当前不能将整个方案标为完成。逐条缺口见 requirements-audit.md。
