# Gemma 云端真实调用记录

维护日期：2026-10-29。本次由用户明确要求实际执行 GitHub Actions，查看云端模型真实结果。

## 执行范围

- 仓库：loonslo/x-algo-watch，分支 main，代码提交 5ac42510d65462373b1310bf19d24da9927495c8。
- 工作流：Review one version sample（sample.yml），research_commit=6258a1549ef808be88fba83d3b2d58364b587ee0，resume_iterations=true。
- 执行环境：GitHub Actions Ubuntu / Python 3.14，凭据仅由 openrouter 环境提供，未读取或导出凭据。
- 云端切换提交的 CI [40857941519](https://github.com/loonslo/x-algo-watch/actions/runs/40857941519) 已成功，日志为 441 passed in 338.89s；这不是模型内容质量结果。

## 第一次真实请求

[工作流 40867213103](https://github.com/loonslo/x-algo-watch/actions/runs/40867213103)，2026-10-29 11:40:37 UTC 创建，作业耗时 1 分 19 秒，全阶段成功保存审计。

模型阶段实际新增一次请求，云端保存字段如下：

```json
{
  "stage": "mechanism",
  "model": "google/gemma-4-26b-a4b-it:free",
  "resolved_model": null,
  "outcome": "deferred",
  "valid": false,
  "deferred_reason": "http",
  "http_reason": "provider_overloaded",
  "response_status": 429,
  "usage": {}
}
```

研究处于 candidate=1、phase=waiting_service、next_stage=mechanism，task_states 为空。没有模型正文或新裁决，未进入三角色审核。HTTP 分类来自云端程序对服务响应的固定安全分类；服务端原始错误正文不保存，不能把该分类冒充模型写出的分析。

原始 reviewed-sample 产物中的 draft.json 按字节原样补存为 [云端草稿产物](samples/2026-10-29-gemma-cloud-40867213103.json)，SHA256 为 350c12a04b46157e24b2be4f226af97075cbcc5be8db9fa2992c9bfb9fcd5b47。下载后与原文件逐字节比较一致，未手工改写云端记录。Actions 原产物保留期为 7 天。

独立调用账本记录本次一条 model_call；预算阶段 verified。不能据 HTTP 429 推断项目 1000 次预算已耗尽，也不能据工作流成功宣称模型研究成功。

## 间隔后的真实重试

[第二次工作流 40867670620](https://github.com/loonslo/x-algo-watch/actions/runs/40867670620) 于 2026-10-29 11:44:11 UTC 创建，仍指定相同提交和 Gemma 型号，作业耗时 1 分 19 秒，全阶段成功保存审计。它晚于首次任务创建时间 3 分 34 秒。

实际新增一次请求，记录同样为 response_status=429、http_reason=provider_overloaded、outcome=deferred、valid=false、resolved_model=null、usage={}；无 raw_output，task_states 仍为空，研究仍是 waiting_service / mechanism。第二次也未得到模型正文或任何新审核裁决。

第二份 draft.json 同样按字节原样补存为 [第二次云端草稿产物](samples/2026-10-29-gemma-cloud-40867670620.json)，SHA256 为 e392046f6976559c65f491b70bf346023b1eb7fc280387b48d4ff9ee3921dda2，下载后逐字节一致。

两次真实请求共新增两条调用账本记录，未绕过预算核对，没有将失败请求排除计数。没有取得可评价的模型分析，因此本次结论仅为“指定 Gemma 的真实请求连续两次被服务端以 429 拒绝/延期”，不能声称已验证 Gemma 的研究质量、JSON 协议遵循度或读者内容。后续须待服务可用后继续真实调用。

本次通过只读样例流程查看真实返回，不把样例的本地分析替换进模型结果，不自动把失败输出标成通过。
