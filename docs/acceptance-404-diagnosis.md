# 事实审核 HTTP404 诊断

维护日期：2026-10-29。

## 已知记录与边界

原运行 [40877016338](https://github.com/loonslo/x-algo-watch/actions/runs/40877016338) 的 mechanism_facts_review 返回404，未保存服务端原始错误，因此不能仅凭该记录断言模型已删除、额度耗尽或参数不兼容。此前综合审核已获得有效异议，不能因下一阶段失败删除异议或将整版标为通过。

## 重建与诊断方法

从既存可信上下文、任务计划和审计只读重建下一缺失请求，捕获调用参数后立即退出，不发送模型请求、不写研究记录。核对原调用 stage:93eef6e93ea8b27a77f9de629e99e79c3851aca700d298fb828b79031abbc217 的输入摘要 f24a9879d1cdcfdbad0087620438fa9b81e4d839fdf200953ec29d9cb4ba5856 及提示词摘要均一致。统一抽出请求体构造函数，研究及诊断共用，保持生产传输行为。

重建请求为 openrouter/free、strict JSON Schema、require_parameters=true、allow_fallbacks=true、max_tokens=24000、reasoning.effort=low，完整两条消息共23158字符；请求体摘要 e5c2b64bd2c2fdb106e7b5a105ce19a9f83ab4f0bbe06360494b80ff02965a69。

云端直接诊断最多三次模型调用，均先计入共享预算：先重放完整原请求；仅在404时分别比较移除 reasoning、原请求只将输出上限改为12000。对照结果只用于诊断，不导入生产审核，也不改变原三关、证据要求或配置。执行期间保持同一串行锁、所有者及 main 限制、专用密钥环境。服务错误仅保存经过敏感信息检查的 message/provider 字段。

[OpenRouter 官方路由文档](https://github.com/OpenRouterTeam/docs/blob/main/guides/routing/provider-selection.mdx)说明 require_parameters 会筛选支持所请求参数的提供方，没有匹配端点可能404；这是待核对的机制解释，不是本次已证明的直接原因。

## 本地检查

Windows / Python 3.14.0：请求体复用、免费路由、Gemma 兼容、预算、诊断和工作流相关75 passed，16.13秒；后续增加原输入/提示词不匹配即拒绝重放的边界，相关27 passed，0.26秒。本地只读重建也再次确认两个摘要一致；未据此宣称404已修复。

## 云端精确重放结果

[诊断运行40880082004](https://github.com/loonslo/x-algo-watch/actions/runs/40880082004) 已成功完成。认证200；云端再次核对原输入与提示词摘要一致，使用完整原消息及原参数执行一次真实模型调用，HTTP200，耗时112.22秒，实际返回 nvidia/nemotron-3.5-super-vl:free。返回完整 JSON，item_id 为原任务，agree=true、issues=[]、finish_reason=stop；随后本地按原任务及原稿调用审核校验，结构校验通过。

原样产物：[精确重放 model-probe.json](samples/2026-10-29-review-404-replay-40880082004.json)，SHA256=f5ea264b8219b3507ef263246a97bc9230e9a3bcb1fa23e6dff3b32c3ab2eb32，已与下载源文件逐字节核对。由于原请求未再次返回404，后续参数对照按边界跳过，没有为获取成功响应降低输出上限、移除推理参数或改变schema。

结论：当前原请求及参数组合可成功执行，本次未复现404。原错误正文未保存，无法确定当时是无匹配端点、模型资源问题还是其他404；“免费路由当时的可用端点匹配变化”仅为可能解释，不能当作已证实根因。此结果也不能证明以后不会再次404。

诊断调用单独计入共享预算，未导入正式审核记录。正式研究中此前综合审核的两条异议保留；本次某一角色同意不等于其他异议消失，不代表整版内容或读者验收通过。

## 后续可观察性修复

为后续404增加固定安全分类：no_parameter_endpoint、no_policy_endpoint、no_available_endpoint、model_not_found；仅在服务错误明确出现对应文字时记录，未匹配时仍为unspecified，不猜填原因或存服务端原文。生产请求参数、缓存及三审规则保持原行为。相关诊断、阶段元数据、传输及新分类测试74 passed，0.27秒。原404不能用新增分类追溯改写为其中某类。
