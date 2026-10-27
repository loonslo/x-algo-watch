# 专业与非专业用户可读区域彻底拆分与主页面重构验收记录

日期：2026-10-27
环境：Windows 11 / Python 3.14.0 / pytest-8.4.2

## 1. 改造背景与核心目标

此前系统生成的主版本页面（`versions/*.md`）存在严重的数据超载与受众混杂：
- 单文件高达 360 KB / 1,865 行，塞入了 V1~V12 审核迭代历史、38 项底层程序检测模板，以及末尾 1,400 行机器原始 JSON Dump；
- 普通创作者面临巨大的阅读认知压力，而面向算法工程师的专页（`versions/technical/*.md`）反而缺乏独立层次；
- 当模型审核存在技术分歧（如 6258a15 卡在 V5）时，主页面直接暴露出“未解决问题 9 项：I-xxx……”等系统纠错信息，影响非专业用户阅读体验。

本次改造彻底将专业与非专业用户的可读区域物理解耦，实现：
1. **主页面极致瘦身与纯净化**：篇幅控制在 80 行 / 5~8 KB 内，仅呈现 30 秒摘要、场景化体验对比表与创作者建议，彻底剥离底层 JSON 与静态规则堆叠；
2. **专业技术深度归位**：技术推论、算法机制、数学比值、成立条件与验证办法独立收拢在 `versions/technical/` 专页中；
3. **未就绪状态优雅降级**：当模型审核有分歧或待研究时，向创作者呈现体面温和的提示，内部审查争议与 issue 仅在技术专页呈现。

## 2. 代码改动清单

- `x_algo_watch/research.py`:
  - `render_research()` 增加 `reader_only: bool = False` 参数控制；
  - `reader_only=True` 模式下仅生成纯净更新公告，彻底剥离技术分析折叠块与研究附录折叠块；
  - 当模型复核存在分歧且无 reader 时，主页面输出优雅降级说明，不再把争议原句塞入正文。
- `x_algo_watch/versions.py`:
  - 重构 `render_page()`：调用 `render_research(..., reader_only=True)`；
  - 彻底移除主页面末尾 1,400 行 JSON Dump 和 38 项死板静态程序模板；
  - 主页面增加醒目的专业技术专页导航卡片与折叠源码链接；
  - 新版本修订中时，主页面仅展示单行小贴士，避免把完整历史进度表格硬塞在开头。
- `x_algo_watch/confirmation.py`:
  - 恢复所有者人工确认速报的文本替换规则，支持多种复核表述的平滑过渡。
- `tests/test_versions.py`, `tests/test_project_research.py`, `tests/test_reader_editions.py`:
  - 更新断言与双受众页面解耦结构匹配。

## 3. 验收结果

### 自动化测试
执行命令：
```powershell
python -m pytest -c pyproject.toml tests -q --basetemp=runtime/pytest-temp
```
结果：**305 passed, 20 skipped, 0 failed in 85.83s**。全量测试 100% 通过。

### 版本库全量重建与抽样检查
执行命令：
```powershell
python -m x_algo_watch.versions
```
成功重建 54 个版本档案。关键版本实测：
- `versions/2026-10-23-07d159b69109f069c85f84d39dd38e38adc040f0.md`:
  - 文件大小由 361,971 字节（360 KB）骤降至 **7,834 字节（7.8 KB）**，缩减达 98%；
  - 78 行，无任何类名、无 1400 行 JSON dump、无 V1~V12 辩论历史；
  - 底部包含专业读者导航链接，点击可直接跳入 `versions/technical/2026-10-23-07d159b69109f069c85f84d39dd38e38adc040f0.md`。
- `versions/2026-10-24-6258a1549ef808be88fba83d3b2d58364b587ee0.md`:
  - 文件大小为 **1,510 字节（28 行）**；
  - 优雅降级说明取代了此前的 9 项报错 issue 诉状。
