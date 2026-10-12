# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

一名使用 Windows 10/11 的中国大学生（本人及同类同学），同时在 3–6 个学习平台上有课程作业与考试。
真实场景是「我不知道这周还有哪些没交」，以及「临交前一天才发现」。

标注：本节由 README「面向 Windows 10/11 的桌面作业汇总工具」与支持平台列表推出，**未做用户访谈**。

## Product Purpose

把分散在清华网络学堂、清华 OJ、雨课堂、Tyche、数你最灵、U 校园的作业与考试汇总到同一处，并在截止前用常驻桌面浮窗提醒。
成功 = 不漏交，且不需要每天手动去六个网站里翻一遍。

标注：由 README 功能列表推出。

## Positioning

用你本机已有的浏览器会话在后台完成登录（账号密码用 Windows DPAPI 加密存储；三层登录：无头浏览器 → 屏幕外真实窗口 → 只在你需要补验证码时才提示），
因此「汇总」这件事不依赖任何第三方服务器，也不需要你反复重新登录。手工 to-do 与平台自带提醒在这一点上无法照搬。

## Operating Context

- 常驻后台，支持开机自启（写 Windows 注册表 Run 项）。
- 启动默认静默：不出现窗口，后台抓完；要看任务时点浮窗标题栏的「主面板」。
- 两个桌面浮窗固定 360px 宽：红色 = 24 小时内截止或已过期（最多 8 行）；白色 = 其余未完成（最多 12 行）。
- 浮窗高度由 `core/controller.py` 的 `_calc_height` 计算：红窗 `header 42 + n × row 20`，白窗 `header 46 + n × row 22`，上限 260 / 360，再 **+16** 的收尾余量。
- 主窗口 1000×720，创建后移动到屏幕右下角。
- 浮窗只在任务刷新或状态变化时更新，不按固定间隔抓取。
- 抓取进度以最多 3 行文字滚动显示，抓完自动收起。
- 多平台并行抓取（默认 3 并发，`HOMEWORK_BOT_WORKERS` 可调）；启动时最多等网络 120 秒。
- 界面运行在 pywebview + Edge WebView2 桌面外壳内，页面由本地 `file://` 加载，不是网站。

## Capabilities and Constraints

功能：6 个平台的抓取与任务汇总；站点可启用/停用/清除会话/删除/填账号密码（Tyche 另需 Basic Auth，数你最灵另需学校全名，雨课堂只能扫码）；
手动任务（标题与截止时间必填，课程与链接可选）；勾选完成与撤销；「未完成 / 已完成」两个标签；任务链接交给系统浏览器打开；
设置内两个开关（开机自启动、启动时静默），改完立即写 `sites.yaml`。

技术约束（直接影响界面实现，属硬约束）：

- 纯 vanilla HTML/CSS/JS：**没有构建步骤、没有 npm / package.json、没有 CDN 与外部字体**，必须离线可用。
- 三份样式文件 `main.css` / `panel.css` / `alert.css` 各自硬编码调色板；两个浮窗共用 `floating-common.js`。
- **浮窗高度由 Python 侧 `_calc_height` 决定 → CSS 行高与 `controller.py` 的 header/row 常量必须成对修改**，否则内容被裁切。
- 前后端通过 pywebview `js_api` 通信；Python 异常在 JS 侧表现为 rejected promise（多处错误分支依赖这个语义）。
- 打包由 PyInstaller 完成（`HomeworkTasks.spec` 收集 `web/`）；`dist/HomeworkTasks/_internal/web/` 是构建产物副本，当前落后于源码。

已确认的边界：未开通的平台不显示；U 校园目前只抓考试。

## Brand Commitments

现有名称：作业清单 / HomeworkTasks（v26.4）。界面语言：简体中文。
**用户未提出任何具有约束力的视觉资产或参考**：无 logo、无指定字体、无指定配色 → 视觉世界可被替换。

## Evidence on Hand

- `Work/README.md`：产品事实来源；`Work/docs/changelog/CHANGE v26.1–v26.4.md`。
- 真实任务数据形状（`uid` / `title` / `course` / `site_name` / `manual` / `url` / `end` / `done`），来自 `main.js` 的 `render()` 与 `core/task_logic.py`。
- 真实平台名单与各平台登录差异（README 表格）。
- 上一轮的界面验证基建：`recon-archive/fixes-verify/ui_preview_build.js`（把真实 CSS 内联 + 预置真实 DOM 的无头预览）。

**缺失且未来工作不得编造**：真实截图资产、用户研究、遥测数据、任何用户数/评价/增长/平台合作信息。

## Product Principles

1. 一次配置，长期无感：登录、抓取、启动都不该让用户当运维；界面不该把「维护登录」变成主线。
2. 截止时间是唯一主角：层级围绕「还剩多久」组织，站点、课程、链接都是次要字段。
3. 不打扰：只在需要用户动手时出现；能后台完成的事不占据前台。
4. 离线自足：任何依赖外部 CDN / 字体 / 服务的实现方式都不成立。
5. 数据留在本机：没有账号体系与云同步，界面不得暗示存在服务器侧数据。

## Accessibility & Inclusion

- 文本与占位符对比度 ≥ 4.5:1（当前实测 6 处不达标，见 `design-verify` 审计输出）。
- 键盘可达：不得存在只能靠 hover 才能发现或完成的操作（当前手动任务的「删除」按钮 `opacity: 0`，仅 hover 显示）。
- 可点击目标 ≥ 24×24px（当前浮窗窗口按钮实测约 19px 高）。
- 中文界面在 1000×720 主窗口与 360px 宽浮窗内不得裁切或横向溢出。
