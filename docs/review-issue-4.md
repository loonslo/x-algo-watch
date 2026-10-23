# Issue #4 人工复核准备记录

核对日期：2026-10-23。环境：Windows / Python 3.14；只读 Git 对象及 GitHub 连接器。本记录是本次源码核对结论，不是所有者确认、模型三关通过或线上部署验收。未修改 Issue、人工事实表、速报及检查进度。

对象：[Issue #4](https://github.com/loonslo/x-algo-watch/issues/4) 及机器人评论 6337426097。固定目标 `989fb91254d461bf4b9df53fb12d5c9a61c2b354`，第一父提交 `7f2ca2b914cadac591ea7fceec0c477e473d3dee`。不将后来 main 的状态混入这次复核。

## 已确认

- Fair Share 源码默认值由 false 变为 true；新增新用户 soft NSFW 过滤开关默认 false，年龄阈值默认 86400 秒（24 小时）。Fair Share 候选预算 PhoenixScoresResultSize 仍为 2800。证据：[参数差异](https://github.com/xai-org/x-algorithm/compare/7f2ca2b914cadac591ea7fceec0c477e473d3dee...989fb91254d461bf4b9df53fb12d5c9a61c2b354)、[目标参数](https://github.com/xai-org/x-algorithm/blob/989fb91254d461bf4b9df53fb12d5c9a61c2b354/home-mixer/params/param.rs#L804)。这只证明公开默认配置，不能证明线上使用这些值。
- Fair Share 完整实现现已补核。启用要求开关 true、无缓存帖子、有 scoring_sequence，且 Phoenix ranker kill switch 没有被判定启用。候选按 served_type 分组；缺失时取第一个 retrieval_source 的 served_type，再缺失归入 None 组。总数不超预算则全保留；超预算按组数量占比分配，先向下取整，再按余数从大到小补齐，同余数按组首次出现顺序；组内保留输入顺序前若干项，最终保持原输入相对顺序。没有按粉丝数、账号大小或作者身份分配名额。源码内测试示例 440/220/440、预算 1000，保留 400/200/400；这是阅读到的上游测试，未执行。该文件相对父提交无变化，本次改变的是默认开关。证据：[完整实现及测试](https://github.com/xai-org/x-algorithm/blob/989fb91254d461bf4b9df53fb12d5c9a61c2b354/home-mixer/filters/fair_share_ranking_filter.rs#L11)。
- 两项过滤均在 Phoenix 候选管线的评分前过滤链；Fair Share 另接入 PhoenixScores 管线。它裁剪进入评分的候选，不能直接解释成最终展示配额。证据：[Phoenix 候选管线](https://github.com/xai-org/x-algorithm/blob/989fb91254d461bf4b9df53fb12d5c9a61c2b354/home-mixer/candidate_pipeline/phoenix_candidate_pipeline.rs#L400)、[PhoenixScores 管线](https://github.com/xai-org/x-algorithm/blob/989fb91254d461bf4b9df53fb12d5c9a61c2b354/home-mixer/candidate_pipeline/phoenix_scores_pipeline.rs#L150)、[框架执行顺序](https://github.com/xai-org/x-algorithm/blob/989fb91254d461bf4b9df53fb12d5c9a61c2b354/candidate-pipeline/candidate_pipeline.rs#L123)。
- 新用户过滤要求：无主题 For You 请求、开关 true，且能从用户 ID 算出账号年龄并严格小于阈值；删除条件是 in_network == Some(false) 且 soft_nsfw == Some(true)。这里“站外”指关注关系之外，不是外部网站链接。未知标签或未知 in_network 不满足删除条件；缓存帖子没有像 Fair Share 那样的跳过门槛。证据：[完整过滤器](https://github.com/xai-org/x-algorithm/blob/989fb91254d461bf4b9df53fb12d5c9a61c2b354/home-mixer/filters/new_user_soft_nsfw_filter.rs#L10)。
- 标签水合器只查询缺少现成 soft_nsfw 标签、且作者不在本人/已关注作者集合中的帖子，并复用已有标签。整批查询错误返回 Err，单条明确失败也返回 Err；框架 update_all 仅对 Ok 更新，错误时保留原字段，不因该错误直接删除或在这里重试。原标签 None 的候选仍不满足本过滤器删除条件；已有 true 不因失败被清除。成功响应没有 SOFT_NSFW 标签且没有明确失败标记时写入 Some(false)，与查询失败不同。不能承诺最终展示，因为后续还有其他过滤和选择。证据：[标签水合器](https://github.com/xai-org/x-algorithm/blob/989fb91254d461bf4b9df53fb12d5c9a61c2b354/home-mixer/candidate_hydrators/new_user_soft_nsfw_label_hydrator.rs#L38)、[框架更新行为](https://github.com/xai-org/x-algorithm/blob/989fb91254d461bf4b9df53fb12d5c9a61c2b354/candidate-pipeline/hydrator.rs#L63)。
- 两个提交的 snapshot 实测参数 198→200，过滤器实现文件 34→35（排除 mod.rs），模块声明 33→34；新增 new_user_soft_nsfw_filter。这是源码清单计数，不是线上运行规则数。Issue 正文 base == commit、files/added/removed 为 0 的记录是当前登记重核，不能拿其零差异否认真实父子变化，也不能将全部登记失配都认定为本次提交造成。

## 仍不确定

- 线上是否部署这版、特定请求的真实 feature switch、年龄阈值、预算、缓存/scoring_sequence 状态及 kill switch。公开源码和本 Issue 不提供这些运行证据。
- 标签覆盖率与准确率、来源数量及输入顺序的实际分布、过滤数量、最终曝光/互动变化。源码支持条件性候选裁剪推断，不证明线上效果。
- migration16、migration25 及两个 reference.source 登记失配的业务含义；它们是引用依赖失配提示，不直接证明相关观点错误或机制已启用。本次未读取原手册或迁移原资料，不能据此批量更新、驳回人工登记。

## 应驳回或改写

- 驳回“Fair Share 同步打开/线上已启用”：改成“公开源码默认值改为 true，线上状态未知”。
- 驳回“新用户 weak NSFW 过滤已成为线上主线”：改成“新增条件性候选过滤路径，公开默认开关为 false”。
- 驳回“Fair Share 平均分流、扶持小账号或保证作者曝光”：实现是按来源类型数量比例裁剪评分候选，最终展示仍有后续流程。
- 驳回“本人和已关注作者一律保留”：水合查询跳过不等于删除豁免；最终以两个候选字段为准，也不保证通过其他过滤。
- 更新“Fair Share 完整实现未知”和“标签错误处置未知”：旧证据包截断导致的缺口现已补齐，应保留原审计，另附本次补证，不追改为原模型已核完。

## 可直接用于人工复核的结论

建议维持需人工，不直接确认现有机器人草稿。可确认：989fb91 将 Fair Share 的公开默认值由 false 改为 true；其完整实现按来源类型数量比例裁剪评分候选，不支持小账号受益或平均分流结论。新增的新用户 weak NSFW 过滤默认关闭，仅在开关开启、无主题 For You、账号年龄小于阈值等条件满足时，删除同时明确标记为关注关系外及 soft NSFW 的候选；标签查询失败本身不直接删帖。两项机制的线上配置、部署范围及真实曝光影响均未证实。过滤文件/模块数量失配成立，引用登记需逐项复核；“线上已开启”“新用户过滤已生效”“关注作者绝对豁免”应从草稿删除。

## 验证范围与限制

已通过 GitHub 连接器回读 Issue、全部评论、完整 Fair Share 文件和新用户过滤器；两文件 blob SHA 与本机 Git 对象一致（208fd3b67415a76128135f972ff0f104e4d497e7、d2a71a4a61b5788c1321dedc75aba00a7cb2d312）。只读对象核对完整标签水合器、框架更新与执行顺序、两个管线及父子参数差异；现有 snapshot 对两个指定提交均成功解析并复算上述计数。

尝试使用完整登记表运行隔离 compare，因该副本所需 Git 对象或历史不可读，未完成，未将其记为检测通过；随后只读 snapshot 和指定文件/父子 diff 核对成功。未执行全历史回放、完整测试套件、上游 Rust 测试、模型三关复核或生产实验；本次仅新增审阅文档，不改程序。生产检查记录、登记、确认标签和正式 digest 均未修改。
