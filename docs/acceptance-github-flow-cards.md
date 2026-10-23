# GitHub 实际流程测试卡（2026-10-23）

用户要求：测试与模型调用均由 GitHub Actions 执行，用实际生成、三角色审核和中断恢复结果判断流程是否需要调整。不能以本地预览、手写改稿或旧模型结果替代本轮测试。

## 验收边界

- 目标仓库 loonslo/x-algo-watch，分支 main。沿用 openrouter/free、实际型号审计、三角色全部通过、最多五候选、停滞暂停及每日 1000 次共享预算。
- 保留历史、旧候选与当日计数；已暂停的单版本通过 resume_iterations=true 显式恢复。正式 digest 仍需所有者确认。
- 依次运行模型测试，避免并发绕过预算或覆盖状态。运行成功不等于稿件通过；检查实际 model_call、version_research、iteration 与最终页面。

## G1：7f2ca2b 新版真实起草、复核与恢复

目标：7f2ca2b914cadac591ea7fceec0c477e473d3dee。

入口：check.yml，research_commit 指定目标，resume_iterations=true。

[实际运行 39923486204](https://github.com/loonslo/x-algo-watch/actions/runs/39923486204)。已发起，结果待运行结束后记录；不得提前写通过。

核对：本轮是否产生新调用、实际响应型号、阶段失败原因、是否进入全部三角色、争议后是否修订、是否保存检查点与预算、最终通俗/技术页是否绑定本轮结果。

## G2：989fb91 新版输出协议与三角色审核

目标：989fb91254d461bf4b9df53fb12d5c9a61c2b354。

入口同 G1，显式恢复此前输出校验暂停。

[实际运行 39923548088](https://github.com/loonslo/x-algo-watch/actions/runs/39923548088)。已发起，在主串行组等待；结果待补。

核对：四问及摘要字数、证据引用、默认值与线上状态区分、Fair Share 完整实现是否足以支持结论、本人及关注作者是否被误写成绝对豁免。旧阅读层改写不算本轮自动生成稿。

## G3：07d159b 检测失败边界

基线：989fb91254d461bf4b9df53fb12d5c9a61c2b354；目标：07d159b69109f069c85f84d39dd38e38adc040f0。

计划使用 check.yml 的 test_from/test_to 隔离历史检测；等待前两项主串行任务结束后发起，当前尚未运行本卡。

已知待验证阻塞：visibility-filtering/rules/levels.rs 的 TIMELINE_HOME_HYDRATION 引用由 Expansion::ReactedTo.placed 构造的规则数组，当前严格解析器不支持。预期应红色、complete=false、不推进生产进度、不用不完整证据起草模型结论。不能把这种安全停止写成算法无变化或完整测试通过。

## G4：现有程序与真实历史回放回归

[GitHub CI 39923666690](https://github.com/loonslo/x-algo-watch/actions/runs/39923666690)：已执行成功；具体测试数量以该任务日志为准，待回读补充。该 CI 克隆只读上游对象并执行 pytest，不能替代 G1/G2 的实际模型稿质量验收。

## 本轮结果

测试正在进行。本卡登记本身不代表模型质量通过、线上算法部署或人工确认；结果将在本文件继续追加实际证据。
