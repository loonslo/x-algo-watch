# 发布、验收与回滚

本地代码已包含 P2–P4 的运行边界；公开发布仍须确认精确仓库目标。所有步骤仅适用于本项目，不对上游或手册写入。

## 发布前

1. 运行完整测试，核对 `registry/facts.yaml` 和 `registry/migrations.yaml`，检查公开输出；使用 GitHub noreply 提交身份。
2. 确认是否以本机生成的历史记录作初始进度。当前生产基线是 `7f2ca2b`，原有 26 个红橙历史提交不批量发通知；基线后的新红橙提交会通知。历史测试使用专门的 `测试` Issue，不推进生产状态。
3. 在准确目标下创建公开仓库，设置默认 main、Actions 默认只读，启用 secret scanning 与 push protection，再推送已审阅代码。

## MVP 0 与 MVP 1

`check.yml` 默认只运行 detect / publish；没有 `DRAFT_ENABLED=true` 时，draft 跳过。detect 和 publish 不引用任何 secret。定时 cron 为 UTC `17 1-22/3 * * *`，对应北京时间 00:17 至 21:17，每 3 小时一轮。

先在 Actions 手动触发历史测试：`test_from=862a123`、`test_to=74d49a4`。预期出现 `测试` 标签、源码证据和规则数 26→27；真实 `data/` 与 digest 不改变。随后手动运行正常检查，核对 main 的状态提交与本机结果。

通知需单独配置：在仓库的 Watch 菜单选择 Custom，勾选 Issues 并 Apply；在个人 Notifications 设置确认邮件通知已开启、默认接收地址正确。仅选择 Participating and @mentions 时，机器人创建的新 Issue 未必会提醒所有者。不要把接收地址写入公开文件；Issue 创建成功与邮件实际送达分别验收。

MVP 1 当前按所有者选择，使用同一固定免费型号的两个调用角色：

- 起草：`minimax/minimax-m3.1-flash:free`
- 复核：`minimax/minimax-m3.1-flash:free`

默认要求起草、复核使用不同型号；所有者已明确改为同一 MiniMax，因此配置显式设置 `allow_same_model: true`。同模型仍执行两次独立请求和两种提示词，但不能称为不同模型的独立复核；审计记录 review_mode，草稿醒目标注并保留人工确认。所有型号仍须以 `:free` 结尾，不做付费或随机模型回退。本地最大每天 50 次、调用间隔至少 3.1 秒；失败请求也记入审计。OpenRouter 的服务端额度仍是最终约束，实际输出质量须线上验收。

MiniMax 当前使用其支持的 reasoning effort=low、总输出上限 6000，给结构化 JSON 留出空间。空响应、超长或预算截断均延期，不视为合格分析；下轮复用已通过的阶段继续。推理也占用总输出预算，参见 [OpenRouter 推理设置](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)。

启用前，在 GitHub 创建 `openrouter` 环境，将 deployment branch policy 设为 **仅 main 分支**；不要使用会允许标签匹配的宽泛规则。在 OpenRouter 为项目创建限额专用 key，由所有者直接存成环境 secret `OPENROUTER_API_KEY`。密钥不发到聊天、不放在本机或仓库文件中。最后将 repository variable `DRAFT_ENABLED` 设为 `true`。

只有 draft 任务使用该环境，只有调用模型的步骤传入 key。定时和手动任务仅在 main 运行；Issue 标签事件不能触发带 key 的任务。确认工作流只接受所有者标签，并重新读取 Issue 和机器人评论。所有 action 已固定到官方仓库提交号。

补写历史样例时，手动运行 check，填写 `backfill_commit` 为已有完整红橙检查记录的完整 SHA；不要同时填写 `test_from` / `test_to`。例如 `17c96bf346df3d62b90f1bb01830d432dba74485` 是两项权重同比增加的样例。补写不重跑上游、不改变最后成功检查位置，只追加补写与通知资格事件；之后沿用正式起草、复核和所有者确认流程。与隔离的历史测试不同，确认后可进入真实速报。

历史记录的登记匹配提示反映原检查时的状态；确认历史草稿时须对照现有 registry/facts.yaml，不能将旧“未登记”直接当作当前事实。模型复核通过不代替这一步人工核对。

当前按用户更正启用 `project_research: true`：模型以整个版本为单位，读取有范围限制的项目架构、跨模块差异、实现与引用窗口，输出机制变化和具体假设，不再只解释孤立参数。人工确认前可在版本文件阅读带条件、替代解释和置信度的推测；正式 digest 仍须所有者确认。

每次实际研究另存 `data/project-context/提交号-内容摘要.json` 的不可变输入快照，研究正文链接到这一份；同一版本以后补证据也不能改掉旧研究所引用的输入。上下文网络读取失败不阻断检测证据发布，请求保留在记录里后续重试。

首次历史研究可填写 `research_all=true`，或 `research_commit=完整SHA`；只读取已有完整迭代记录，不推进检测进度，不批量创建历史 Issue。当前 49 个迭代的上下文已准备在 `data/project-context/`，每轮最多处理 3 个待研究版本；有队列而无新提交时也会运行 draft。每天与旧模型调用共享最多 50 次预算，不增加付费或随机回退；额度用尽时跨日续写，不将待研究冒充已解读。只研究新版本时无需额外输入，正常 detect 会准备上下文。

只读检查配置：

```powershell
python -m x_algo_watch.deployment --repository OWNER/x-algo-watch
```

审计使用 gh 已有登录，只输出布尔结果，不读取令牌值或 secret 内容。环境或权限不可读时结果为未通过，不能当作“已设置”。

## 记录与故障

检查记录是检测进度唯一来源；同一提交不重复追加。模型调用、复核方式、登记表重核和确认决策以带版本的事件追加到同一月记录。旧逐项模型草稿只在 Issue 评论；新的整版本研究可在独立版本文件预览，明确标为模型推测/尚未人工确认，分歧同时展示。模型输出的审计 JSON 留在记录中，确认后才追加 `digest.md`。

读者入口为 `versions/README.md`。每个上游提交一份 `versions/日期-完整提交号.md`，以整版本模型机制研究为主；没有结果时显示待研究，原固定模板只作折叠的证据提示。日期按 UTC 计算，目录按提交时间倒序。确认后将速报原文与具体研究决策同步到对应文件，旧速报的确认不授予新版研究已确认状态。灰色版本、初始基线和失败记录也有档案；基线不属于一次迭代，失败不写成无变化。登记重核附在原档案内，历史测试不写入目录。

重建已有版本档案：`python -m x_algo_watch.versions`。此命令只读取本项目的既存证据与速报，写 `versions/`；不访问上游、不改检查记录或生产进度、不调用模型。生成文件由程序维护，人工编辑仍在 Issue 评论中进行并经确认归档，避免重建丢失批注。

检测产物包含数据哈希。publish 拉取 main 后先比较原始数据哈希，再校验产物和敏感信息，防止起草期间 main 的新数据被覆盖。只提交白名单内的 `data/` 文件（含完整 SHA 命名的项目上下文 JSON）及由受信代码生成的版本目录/档案；confirm 另可追加 digest。项目研究事件须匹配当前上下文摘要、模型结构与提示词配置；版本页由受信代码呈现，写入前扫描全部页面，拒绝符号链接与白名单外文件。推送不使用 force，不将令牌写 Git 配置或 URL。

先持久化检测证据和通知资格，再调用 Issue API；因此 API 暂时失败后下一轮仍会补发，不会因检测进度已推进而漏提醒。Issue 按完整 SHA、机器人身份与生产/测试标记去重。关闭的 Issue 不自动重开。重复发布同一草稿不覆盖所有者编辑。

额度耗尽、模型缺失或服务不可用：只保留证据，维持待起草，下轮从已通过的分析或复核阶段继续。校验两次仍失败则需人工；两个模型有分歧时明确标注。任何 `[待补]` 内容在所有者编辑补全前不能确认。

## 上线验收与回滚

P2 必须保留实际 Actions run 链接、正常记录提交、历史测试 Issue 链接，并观察满 24 小时的定时结果；本机测试不代替这些证据。P4 还需核验环境分支策略、真实模型质量和通知到达。没有这些证据时状态保持“待线上验收”。

要暂停模型，先将 `DRAFT_ENABLED` 设为 `false`；检测与证据通知继续运行。要停止全部监测，在 GitHub 禁用 check 工作流。程序版本出问题时通过普通 revert 恢复，保留数据记录；不要重写 Git 历史。若需回放，使用独立输出目录；生产记录恢复须先备份并明确最后成功 SHA。

若发现密钥泄露，由所有者先在 OpenRouter 作废 key，再替换 GitHub 环境 secret；随后审查用量与公开记录。程序不会替用户把密钥保存到本机。

参考：[GitHub 工作流权限](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)、[环境分支限制](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)、[OpenRouter 免费额度](https://openrouter.ai/pricing)、[官方模型列表](https://openrouter.ai/api/v1/models)。
