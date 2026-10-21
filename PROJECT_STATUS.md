---
project_id: x-algo-watch
updated: 2026-10-22
status: active
overview: 只读监测 X 推荐算法公开源码变化，生成分级证据，辅助创作者手册人工复核。
progress: 仓库已公开；Linux CI 120 项通过；所有者已存环境密钥，模型已启用，10 项配置审计通过；正在验收历史样例真实起草。
next: 运行 10 月 14 日历史样例的起草与复核，供所有者审阅确认；继续收集 24 小时定时证据。
evidence:
  - docs/acceptance-p1.md
  - docs/acceptance-p2-p4.md
  - docs/requirements-audit.md
  - docs/deployment.md
  - README.md
  - tests/test_history.py
  - data/README.md
---

# 项目进度

2026-10-21：完成 P1，74 项测试和 50 个提交的历史回放通过。该条是早期验收，后续结果以下段为准。

2026-10-22：P2–P4 本地代码已补齐，包括任务隔离、产物/敏感内容校验、幂等 Issue、420 项登记、29 行迁移、模型校验与独立复核、所有者编辑确认及调用恢复。用户选定 Xing4.0 与 NVIDIA Nemotron 3 Super，均固定免费模型。118 项测试全部通过，包含合成端到端联调及真实历史回放；原上游工作区保持不变。

后续发布：用户授权后已公开推送，仓库权限与 secret scanning / push protection、openrouter 仅 main 策略都已配置。Linux CI、历史测试 Issue #1 和正常检查均成功；首次 publish 的目录校验问题已修复，相关证据见验收记录。模型仍关闭，用户要求在密钥输入框处交接，由用户自行填写。

后续复验：f04f916 的 Linux CI 为 120 passed in 8.96s；大型摘要修复后的历史工作流成功，同一测试 Issue 已保留旧/新值与规则差额。仅查询密钥名称列表，环境中仍未发现 OPENROUTER_API_KEY，未启用模型。

密钥交接后：所有者已直接保存 OPENROUTER_API_KEY，已设置 DRAFT_ENABLED=true，真实 API 的 10 项上线配置审计全部通过。正在增加仅选取已有完整记录的历史样例补写，不改变生产进度；实际模型输出尚待验收。

尚未完成：真实模型起草与评论确认、通知到达、连续 24 小时定时证据。3 个人工来源明确为未核对。完整目标保持未完成，不以本机测试替代这些线上条件。
