# 固定 Gemma 模型切换

维护日期：2026-10-29。

## 范围与实现

按用户指定，将 config/watch.yaml 中起草及全部审核角色切换为 `google/gemma-4-26b-a4b-it:free`，显式开启 allow_same_model。机制流程从配置传入各阶段请求、审计及发布校验，包含机制、上下文、筛选、读者起草与全部三角色审核；相同型号不宣称异模型复核。

新模型参与研究指纹及调用缓存键，旧路由稿件、裁决和失败缓存不冒充 Gemma 的结果。原有审计、所有模型的当日调用累计和 1000 次上限保留。历史路由事件仍按其记录的型号验证，发布端检查每项实际引用调用与研究记录的角色型号一致。

2026-10-29 只读核对 [OpenRouter 模型端点](https://openrouter.ai/api/v1/models/google/gemma-4-26b-a4b-it:free/endpoints)与[模型说明](https://openrouter.ai/google/gemma-4-26b-a4b-it:free)：支持 response_format 和 reasoning，模型说明明确没有 JSON Schema enforcement。为 Gemma 使用 JSON 对象传输、require_parameters=true、allow_fallbacks=false；仍传完整阶段协议，并执行原有全部本地结构、引用、证据校验及三个审核角色。旧路由 schema 传输保持兼容。

## 本次验证

环境：Windows / Python 3.14.0，模拟模型响应；真实 Git 回放只读取项目内现有上游对象库。

- Gemma、机制主链、读者流程、旧免费路由、共享预算：65 passed，18.29 秒。
- Gemma、旧起草/研究/迭代、发布、阶段调用、补丁重试、独立执行器及工作流安全：133 passed、2 skipped，40.10 秒；两项跳过仅因该轮未指定上游对象库。
- 上述两项随后指定只读对象库单独补跑：2 passed、25 deselected，3.08 秒。
- 新增回归覆盖实际请求模型与 JSON 模式、机制三审和读者三审、旧模型审核通过结果不复用、同日预算不清零、暂停续跑及发布端拒绝混入旧型号调用。完整模拟链经 8 个阶段后才可发布阅读正文，正式 digest 仍不自动写入。

本次未重跑整个测试集；此前 437 项全量是切换前版本的历史结果，不算本次复测。未发真实 Gemma 请求，未验证其当前可用性、真实输出质量或新配置的远程运行结果；不能据此承诺 HTTP 429 消失。
