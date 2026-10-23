# GitHub 实际流程测试卡（2026-10-23）

用户要求：测试与模型调用均由 GitHub Actions 执行，用实际生成、三角色审核和中断恢复结果判断流程是否需要调整。不能以本地预览、手写改稿或旧模型结果替代本轮测试。

## 验收边界

- 目标仓库 loonslo/x-algo-watch，分支 main。沿用 openrouter/free、实际型号审计、三角色全部通过、最多五候选、停滞暂停及每日 1000 次共享预算。
- 保留历史、旧候选与当日计数；已暂停的单版本通过 resume_iterations=true 显式恢复。正式 digest 仍需所有者确认。
- 依次运行模型测试，避免并发绕过预算或覆盖状态。运行成功不等于稿件通过；检查实际 model_call、version_research、iteration 与最终页面。

## G1：7f2ca2b 新版真实起草、复核与恢复

目标：7f2ca2b914cadac591ea7fceec0c477e473d3dee。

入口：check.yml，research_commit 指定目标，resume_iterations=true。

[实际运行 39923486204](https://github.com/loonslo/x-algo-watch/actions/runs/39923486204)。已完成，Actions 为 success，但本轮两次起草均被输出校验拒绝，未进入三角色审核：第一次为 nvidia/nemotron-3.5-super-vl:free，when 含 online_status；纠错稿为 dots-studio/dots-3-note-preview:free，who 含 Phoenix。两次均 HTTP 200，最终 V1/manual/waiting_manual/invalid_output。运行成功不能记成稿件通过。[未经改写的实际输出](samples/2026-10-23-github-flow.md)。

核对：本轮是否产生新调用、实际响应型号、阶段失败原因、是否进入全部三角色、争议后是否修订、是否保存检查点与预算、最终通俗/技术页是否绑定本轮结果。

## G2：989fb91 新版输出协议与三角色审核

目标：989fb91254d461bf4b9df53fb12d5c9a61c2b354。

入口同 G1，显式恢复此前输出校验暂停。

[实际运行 39923548088](https://github.com/loonslo/x-algo-watch/actions/runs/39923548088)。已完成真实模型阶段，新增 12 次调用。V1：综合同意、事实不同意 5 项、读者同意；V2：综合不同意 12 项、事实同意、读者不同意 8 项；V3：综合不同意 10 项，事实核对 HTTP 200 但 finish_reason=length 后延期，读者核对尚未执行。最终 pending/V3/waiting_service/facts_review，没有全部通过的成稿。

核对：四问及摘要字数、证据引用、默认值与线上状态区分、Fair Share 完整实现是否足以支持结论、本人及关注作者是否被误写成绝对豁免。旧阅读层改写不算本轮自动生成稿。

## G3：07d159b 检测失败边界

基线：989fb91254d461bf4b9df53fb12d5c9a61c2b354；目标：07d159b69109f069c85f84d39dd38e38adc040f0。

新增 detection-test.yml，使用既有 detect 的 test_from/test_to 隔离检测，不调用模型、不发布生产结果，也不占用模型主串行组。[实际运行 39923981236](https://github.com/loonslo/x-algo-watch/actions/runs/39923981236) 已完成。

已知待验证阻塞：visibility-filtering/rules/levels.rs 的 TIMELINE_HOME_HYDRATION 引用由 Expansion::ReactedTo.placed 构造的规则数组，当前严格解析器不支持。实际产物 test=true、result_code=2、complete=false，记录同一 PostRules 解析失败；证据上传成功，Require a complete inspection 步骤退出 2，Actions 为 failure。红色停止、不推进生产进度、不用不完整证据起草模型结论的边界成立；完整检测与模型研究仍未通过。不能把这种安全停止写成算法无变化或完整测试通过。

## G4：现有程序与真实历史回放回归

[GitHub CI 39923666690](https://github.com/loonslo/x-algo-watch/actions/runs/39923666690)：已执行成功，实际日志 280 passed in 18.21s；新增隔离检测工作流后的 CI 39923977756 同样 280 passed in 17.29s。该 CI 克隆只读上游对象并执行 pytest，不能替代 G1/G2 的实际模型稿质量验收。

## 本轮结果

本轮三张实际测试卡及 CI 均已执行并回读，结果如上；模型质量仍未通过，不代表线上算法部署或人工确认。[实际原稿及审核意见](samples/2026-10-23-github-flow.md)直接提取自 GitHub 产物，没有人工润色。

### G2 发布失败与 GitHub 审计恢复

G2 的 detect/draft 成功，publish 因“main 的检测数据已变化，拒绝覆盖”失败。排队时 G1/G2 都固定在 76b30b5；G1 完成后更新了 main 数据，G2 随后仍从触发提交读取旧数据。串行锁没有让默认 checkout 自动切换到最新 main；覆盖保护正确阻止了旧数据回写。

已新增 recover-research.yml，[恢复运行 39926184828](https://github.com/loonslo/x-algo-watch/actions/runs/39926184828) 成功。该入口仅允许所有者在 main 对本仓库已完成的手动 check.yml 运行恢复，经源运行、同源上下文及事件边界校验后，复用现有 merge_research 追加审计，不调用模型、不操作 Issue。只有当前版本最新研究 ID 与源检测基线一致时才恢复 pending 检查点，避免覆盖较新研究。

回读远程记录确认 12/12 条实际调用 ID 已保留，V3/pending/waiting_service/facts_review 检查点成立；最后成功检测提交仍为 989fb91。UTC 2026-10-23 的总预算记账为 21 次（含既有取消预留），没有清零。恢复不是模型审核通过。

### 实际输入覆盖与需调整点

1. G2 的实际 GitHub inspection 产物仍为上下文 2d95a04c…；Fair Share 的 E21 只有第 1–25 行，止于 filter 函数签名，不含 group_by_served_type、fair_share_counts 或 selection_mask。旧阅读层补证并未进入本轮模型输入。应先补核心实现及框架错误处理，再起草；不能靠重复修订补出缺失证据。
2. G1 两次完整响应分别在 when/who 引入机器字段或技术词；代码纠错请求只发送 previous_validation_errors，没有发送被拒绝的完整原稿，且免费路由可能换型号。建议把上一稿与字段级反馈一起交给纠错模型，仍重新检查全部规则和三角色，不能放宽审核来制造通过。
3. 排队运行应在获得串行锁后读取最新可信数据，并明确固定后续阶段的代码/证据来源；本轮已新增恢复入口，主 check.yml 的默认 checkout 行为尚未调整。
4. 07d159b 的规则引用及 Expansion::ReactedTo.placed 写法必须补到严格解析与真实回归样本。280 项历史 CI 通过不能证明最新规则表已经支持。

上述四项是本轮证据支持的后续调整范围，尚未将对应程序修复或修复后质量验收记为完成。本次新增的是 GitHub 测试/恢复入口、真实样例与验收记录；没有在本机运行模型或 pytest 来替代 GitHub 验证。
