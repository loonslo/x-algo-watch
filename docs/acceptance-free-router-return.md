# 切回免费自动路由并直接验证

维护日期：2026-10-29。

用户在固定 Gemma 端点多次上游429后，要求切回此前的 free 继续真实调用。起草与全部审核角色已改回 openrouter/free，allow_same_model=false；不固定实际型号，仍保存接口实际响应型号，保留全部三审、原审计和共享日预算。

直接 API 诊断改为读取当前配置，不再写死 Gemma。先核验密钥认证，再发最小文本请求；若其成功，再发当前路由使用的严格 JSON Schema 请求，allow_fallbacks=true。Gemma 诊断保留原 JSON 对象分支。模型调用均在请求前记账，不修改生产研究记录，不生成算法研究结论。

Windows / Python 3.14.0：直接诊断、工作流边界、共享预算及免费路由相关 59 passed，15.35 秒，含两种配置的传输分支和预算不足时拒绝请求。云端实际结果见下节，不能由本地测试推断所有免费型号可用或不可用。

## 云端真实结果

[Actions 40872174653](https://github.com/loonslo/x-algo-watch/actions/runs/40872174653) 使用提交 4a803c6f545a63533f28cf98c16c2dc1f000b979，运行真实 direct_probe，作业成功，耗时 1 分 7 秒。认证检查 HTTP200/authenticated=true；两次模型请求均先占用共享预算。

| 真实请求 | HTTP | 实际返回型号 | 模型正文 | 请求耗时 |
| --- | --- | --- | --- | --- |
| minimal_text | 200 | nvidia/nemotron-3-ultra-550b-a55b:free | OK | 1.52 秒 |
| project_json | 200 | dots-studio/dots-3-note-preview:free | {"ok":true} | 3.06 秒 |

两次 finish_reason 都为 stop，第二次发送 strict JSON Schema、require_parameters=true、allow_fallbacks=true。响应记录证明本次免费路由可返回完整文本及 JSON，并且实际选择了两个不同的免费型号；不保证后续请求选中相同型号或始终可用。此前 Gemma 的上游限流不能推断为所有免费端点不可用。

云端 model-probe.json 已按字节原样补存为 [真实 API 诊断 JSON](samples/2026-10-29-free-router-direct-40872174653.json)，核对与下载源文件逐字节一致。这两份正文是云端模型真实返回，没有用本地模拟响应替换。它们仅验证 API 连通性和小 JSON 请求，尚未完成整版机制分析、三审或读者质量验收。
