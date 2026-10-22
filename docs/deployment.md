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

MVP 1 使用用户选定的两个免费模型：

- 起草：`xingchen-agi/xing4.0-29b-a4b:free`
- 复核：`nvidia/nemotron-3-super-120b-a12b:free`

只接受固定的不同模型，均须以 `:free` 结尾；不做付费或随机模型回退。本地最大每天 50 次、调用间隔至少 3.1 秒；失败请求也记入审计。OpenRouter 的服务端额度仍是最终约束。模型名单截至 2026-10-21 的官方 API；实际输出质量尚须线上验收。

启用前，在 GitHub 创建 `openrouter` 环境，将 deployment branch policy 设为 **仅 main 分支**；不要使用会允许标签匹配的宽泛规则。在 OpenRouter 为项目创建限额专用 key，由所有者直接存成环境 secret `OPENROUTER_API_KEY`。密钥不发到聊天、不放在本机或仓库文件中。最后将 repository variable `DRAFT_ENABLED` 设为 `true`。

只有 draft 任务使用该环境，只有调用模型的步骤传入 key。定时和手动任务仅在 main 运行；Issue 标签事件不能触发带 key 的任务。确认工作流只接受所有者标签，并重新读取 Issue 和机器人评论。所有 action 已固定到官方仓库提交号。

补写历史样例时，手动运行 check，填写 `backfill_commit` 为已有完整红橙检查记录的完整 SHA；不要同时填写 `test_from` / `test_to`。例如 `17c96bf346df3d62b90f1bb01830d432dba74485` 是两项权重同比增加的样例。补写不重跑上游、不改变最后成功检查位置，只追加补写与通知资格事件；之后沿用正式起草、独立复核和所有者确认流程。与隔离的历史测试不同，确认后可进入真实速报。

只读检查配置：

```powershell
python -m x_algo_watch.deployment --repository OWNER/x-algo-watch
```

审计使用 gh 已有登录，只输出布尔结果，不读取令牌值或 secret 内容。环境或权限不可读时结果为未通过，不能当作“已设置”。

## 记录与故障

检查记录是检测进度唯一来源；同一提交不重复追加。模型调用、独立复核、登记表重核和确认决策以带版本的事件追加到同一月记录。草稿正文只发 Issue 评论；模型输出的审计 JSON 留在记录中。确认后才追加 `digest.md`。

检测产物包含数据哈希。publish 拉取 main 后先比较原始数据哈希，再校验产物和敏感信息，防止起草期间 main 的新数据被覆盖。只提交白名单内的 `data/` 文件；confirm 另可追加 digest。推送不使用 force，不将令牌写 Git 配置或 URL。

先持久化检测证据和通知资格，再调用 Issue API；因此 API 暂时失败后下一轮仍会补发，不会因检测进度已推进而漏提醒。Issue 按完整 SHA、机器人身份与生产/测试标记去重。关闭的 Issue 不自动重开。重复发布同一草稿不覆盖所有者编辑。

额度耗尽、模型缺失或服务不可用：只保留证据，维持待起草，下轮从已通过的分析或复核阶段继续。校验两次仍失败则需人工；两个模型有分歧时明确标注。任何 `[待补]` 内容在所有者编辑补全前不能确认。

## 上线验收与回滚

P2 必须保留实际 Actions run 链接、正常记录提交、历史测试 Issue 链接，并观察满 24 小时的定时结果；本机测试不代替这些证据。P4 还需核验环境分支策略、真实模型质量和通知到达。没有这些证据时状态保持“待线上验收”。

要暂停模型，先将 `DRAFT_ENABLED` 设为 `false`；检测与证据通知继续运行。要停止全部监测，在 GitHub 禁用 check 工作流。程序版本出问题时通过普通 revert 恢复，保留数据记录；不要重写 Git 历史。若需回放，使用独立输出目录；生产记录恢复须先备份并明确最后成功 SHA。

若发现密钥泄露，由所有者先在 OpenRouter 作废 key，再替换 GitHub 环境 secret；随后审查用量与公开记录。程序不会替用户把密钥保存到本机。

参考：[GitHub 工作流权限](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)、[环境分支限制](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)、[OpenRouter 免费额度](https://openrouter.ai/pricing)、[官方模型列表](https://openrouter.ai/api/v1/models)。
