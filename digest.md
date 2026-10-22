# 运营变更速报

本项目非 X / xAI 官方项目。公开代码默认值不等于线上实际值。

<!-- xaw-digest:17c96bf346df3d62b90f1bb01830d432dba74485 -->
**仓库所有者已确认的速报。公开代码默认值不等于线上值；部署未知。**

**复核方式：同一模型的两次调用，不属于不同模型的独立复核；仓库所有者已人工确认。**

### 2026-10-14 · 17c96bf · 源码变化已人工确认

#### FavoriteWeight 与 NotInterestedWeight 默认值调整且部署状态未知

- 改了什么：代码将 FavoriteWeight 默认值从 0.6 调整为 0.66，将 NotInterestedWeight 默认值从 -47.52 调整为 -52.272；公开默认值变化不能证明线上配置已经更新。
- 状态：默认值已改；部署未知
- 【A】FavoriteWeight 的代码默认值由 0.6 调整为 0.66。（E1）
- 【A】NotInterestedWeight 的代码默认值由 -47.52 调整为 -52.272。（E2）
- 登记说明：历史检查中的“未登记”只代表当时的匹配结果；当前已有 `param.FavoriteWeight`、`param.NotInterestedWeight` 两项登记，均关联手册 9.1（[当前登记表的固定版本](https://github.com/loonslo/x-algo-watch/blob/461bd32a257930a241735a25917619d5a2e79f9c/registry/facts.yaml)）。
- 【A】程序计算的数值关系：点赞权重 ×1.1；不感兴趣权重 ×1.1；两者绝对比值 79.2 → 79.2。比值为不感兴趣权重绝对值除以点赞权重；输入与精确结果见证据包 numeric_context。
- 【C】由于无法确认线上实际配置是否已采用新默认值，相关变更对创作者侧推荐结果的实际影响目前无法确定。（E1、E2）
- 现在要不要改做法：先观察；确认两项参数在线上的实际取值与部署状态，并核对手册是否仍有旧默认值引用。
- 影响手册：手册 9.1（当前登记项关联）；是否还需修改手册：待所有者核对现有版本。
- 出处：`home-mixer/params/param.rs` / `FavoriteWeight` @ `17c96bf346df3d62b90f1bb01830d432dba74485`
- 出处：`home-mixer/params/param.rs` / `NotInterestedWeight` @ `17c96bf346df3d62b90f1bb01830d432dba74485`
- 待核对：FavoriteWeight 和 NotInterestedWeight 在线上的实际配置值分别是什么，本次默认值变更是否已经部署？
- 待核对：手册 9.1 及派生材料是否仍有旧值引用，需要更新哪些内容？

#### README 将巴西选举账户列表的更新时间说明改为“October 13, 2026”。

- 改了什么：README 中 Brazil2026ElectionFilter 相关段落的账户列表更新时间由“September 29, 2026.”改为“October 13, 2026.”；证据未展示账户名单内容或运行配置。
- 状态：默认状态待人工核对；部署未知
- 【A】README 的 Brazil 2026 Elections 段落将账户列表更新时间从“September 29, 2026.”改为“October 13, 2026.”。（E1）
- 【A】该变更前后，README 对 Brazil2026ElectionFilter 移除相关账户帖子、保留显式关注例外以及指向实现代码的描述一致。（E1）
- 【C】README 读者会看到账户名单的维护日期被推后，但现有证据不能证明名单内容、过滤器行为或线上配置发生变化。（E1）
- 现在要不要改做法：先观察；核对 README 所指的巴西选举账户名单是否同步更新，并确认相关过滤器的实际配置是否变化。
- 影响手册：未关联登记项；是否改手册：待人工判断
- 出处：`README.md` / `Notable Updates` @ `17c96bf346df3d62b90f1bb01830d432dba74485`
- 待核对：README 所述账户名单对应的源文件是否也在此次变更中更新，具体名单是否发生变化？
- 待核对：该过滤器的实际运行配置与生效状态是什么？

- 确认：2026-10-22
- Issue：[#2](https://github.com/loonslo/x-algo-watch/issues/2)；确认日期：2026-10-22

