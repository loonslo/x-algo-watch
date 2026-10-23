# GitHub 流程调整验收（2026-10-23）

依据：[本轮 GitHub 测试卡](acceptance-github-flow-cards.md)。用户授权调整四处流程，并要求测试及真实模型运行由 GitHub 执行。本机只编辑和检查差异，不运行 pytest 或模型。

## 改动与失败边界

- 项目证据优先完整收录命中开关/结构名称的过滤器及标签水合器生产实现，另收录候选管线框架和水合错误更新实现；跳过测试模块以保留预算。核心实现放不下时暂停上下文准备，不静默裁剪；其他 diff/窗口仍标明范围，不冒充完整文件。新上下文产生新摘要，原证据快照和研究审计保留。
- 格式纠错传入通过安全扫描的 previous_invalid_output 和 previous_validation_errors；换免费路由时也有原稿可修。更新提示词及传输策略版本，避免旧失败缓存冒充新流程。全部格式、证据和三角色审核要求不变。
- check.yml 获得主串行锁后读取最新 main，输出实际 source_commit，draft 固定读取这个 SHA。publish 仍读取当前 main 并校验数据摘要；没有关闭覆盖保护。
- 严格支持 07d159b 的 ReactedTo 展开：读取同提交 expansion.rs 及 tweet_rules.rs，不执行 Rust；核对已审查展开实现摘要、长度表达式、引用和动作，按非阻断规则→父表→阻断规则重建顺序。依赖缺失、表达式/动作未知、展开实现改变仍报红，不推进成功进度。

## GitHub 验证计划

新增真实 07d159b 级别表与展开实现夹具、三条规则原文片段，并在 NOTICE 登记来源。回归覆盖实际顺序、未知/缺失依赖拒绝、完整 Fair Share/错误处理证据、核心预算不足停止、换路由纠错原稿，以及队列源码固定契约。现有 50 提交历史回放保持执行。

当前仅完成编辑，尚未执行 GitHub CI、隔离检测或新流程真实模型复验。结果将在本记录追加；不得将编辑完成写成质量通过。人工 registry、正式 digest 及旧模型裁决不由这次修改自动确认。

## 首轮远程复验

[PR #6](https://github.com/loonslo/x-algo-watch/pull/6) 已合入 main 的 7a41b72。PR CI 39935434358：291 passed in 18.41s；合入后 CI 39935598458：291 passed in 17.84s。新增真实历史、核心完整性、拒绝路径及队列契约均在 GitHub 执行，本机未运行 pytest 或模型。

[隔离检测 39935600865](https://github.com/loonslo/x-algo-watch/actions/runs/39935600865) 成功：test=true、result_code=0，07d159b 已能完整解析；隔离测试不推进生产进度。

[真实模型 39935607359](https://github.com/loonslo/x-algo-watch/actions/runs/39935607359) 的实际输入上下文为 1f430a3b…，总正文 85000 字符；包含完整生产区段：Fair Share 111 行、新用户过滤器 40 行、标签水合器 92 行、水合框架 222 行、候选管线框架 457 行，均未截断。旧 2d95a04c… 快照保留。起草被 who/when/action 技术词校验拒绝，纠错遇 HTTP 429，保存同一 V1 等待服务；detect/draft/publish 均成功。

[续跑 39936041954](https://github.com/loonslo/x-algo-watch/actions/runs/39936041954) 从已有稿件纠错。实际输入保留前次两条调用审计，当天预算为 23，没有清零；模型返回正常响应，技术词错误未再触发，但将配置率 0.5 写为 50%，被未提供百分比派生值的数字白名单拒绝。已达两次格式失败，按规则 V1/waiting_manual/invalid_output 暂停，未冒充三角色通过。

## 精确配置率派生补充

上述 50% 是配置值单位转换，不应当作模型凭空捏造线上流量。新增程序派生函数，仅对白名单参数 ShadowTrafficPhoenixClusterRates 中合法的集群:率条目，用 Decimal 换算百分比并注明不证明线上实际比例；不转换任意权重、不放宽数字校验。新增精确换算、非法输入、权重隔离及 50 允许/51 仍拒绝的回归。该补充尚待 GitHub CI 与真实模型复验。
