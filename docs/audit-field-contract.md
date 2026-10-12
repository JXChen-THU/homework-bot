# 前后端字段契约审计报告

审计范围：`Work\`（排除 `.venv`、`dist`、`recon-archive`、`__pycache__`）
方法：穷举 `Work\core\controller.py` 的 `Api` 类全部 return 路径 → 追踪 `Work\core\site_runner.py` / `Work\adapters\*.py` 的返回结构 → 比对 `Work\web\main.js` / `floating-common.js` / `*.html` 的实际读取点。
本报告只读审计，未修改任何源码。

---

## ⚠️ 审计过程中的并发改动（必读）

审计期间工作区被**并发修改**：`Work\web\main.js`（2026/10/12 11:49:42，25569 字节）与 `Work\web\main.css`（11:49:46，10459 字节）。本会话第一次读取 `main.js` 时（文件 10,200 字节级、旧版本）第 260–278 行的 `load()` 是：

```js
// 旧版（审计开始时读到的内容）
const res = await window.pywebview.api.get_homeworks();
items = res.items || [];
renderBanners(res.errors || [], res.need_login || []);   // ← 只传 2 个参数，res.startup 被丢弃
```

当前磁盘版本已变为 3 参数调用（`main.js:270`），并且 `main.css:85-89` 新增了 `.banner .startup-detail` 样式。**因此下述「严重」项在本次审计快照中已修复**，下面按「已修复（附修复前后证据）」记录，以便复核与回归测试。

---

# 严重程度：严重（唯一）

## S1. `startup` 键：后端产出、前端从不读取 —— 启动提示（数据目录回退）完全不可见

| 项目 | 内容 |
| --- | --- |
| 键名 | `startup`（元素结构 `{kind, text, detail}`） |
| 生产位置 | `Work\core\paths.py:159-168` 定义 `startup_messages()`，返回 `{"kind":"warn","text":FALLBACK_NOTICE,"detail":"数据目录：<ROOT>"}`；`Work\core\controller.py:478-479` 由 `get_homeworks()` 通过 `"startup": paths.startup_messages()` 送出 |
| 消费位置（旧版） | **真实缺口**：`Work\web\main.js` 旧版第 270 行为 `renderBanners(res.errors || [], res.need_login || [])`，只传 2 个实参；`renderBanners` 的第 3 个形参 `startup`（`main.js:73`）恒为 `undefined`，`main.js:78` 的 `if (startup && startup.length)` 恒假。浮窗（`floating-common.js`）与 `alert.html`/`panel.html` 无任何出口 |
| 消费位置（当前磁盘） | `Work\web\main.js:270` 已改为 `renderBanners(res.errors || [], res.need_login || [], res.startup || [])`；渲染逻辑在 `main.js:78-86`；样式在 `main.css:85-89` |
| 用户可见后果 | 程序装在 `Program Files`、`C:\` 根等不可写目录时，`paths.resolve()`（`paths.py:110-143`）静默把数据根切到 `%LOCALAPPDATA%\HomeworkTasks`，`FALLBACK_NOTICE`（`paths.py:131-138`）已经写好中文说明，但**没有任何界面元素显示它**。用户看到的是：sites.yaml 里填好的站点全不见了、每个站点都显示「未登录」、需要重新填账号密码；旧目录的 `data/` 会话文件也不再被使用，于是所有站点集体要求重新登录。用户合理的推断是「程序把配置弄丢了/升级坏了」，而真实原因（目录不可写）永远无法从界面上得知。修复后用户会在顶部看到黄色提示条「程序所在目录不可写，已改用 C:\Users\…\AppData\Local\HomeworkTasks 保存配置和登录状态。原目录没有可迁移的配置，请重新填写账号密码。」并在下一行看到等宽字体的数据目录路径。 |

补充说明（可达性已确认）：`FALLBACK_NOTICE` 只在「首选根不可写且 `%LOCALAPPDATA%` 可写」时赋值（`paths.py:122-138`），`startup_messages()` 随后在任何一次 `get_homeworks()`（`controller.py:479`）都会被调用，所以该值一旦置位就必定进入返回字典 —— 唯一缺失的环节就是前端读取，与前台现象完全一致。

---

# 严重程度：高

## H1. `apply_sites_changes()` 的 `error` 键：后端产出、前端从不读取 —— 保存失败却提示「✓ 已应用」

| 项目 | 内容 |
| --- | --- |
| 键名 | `error`（成功分支为 `ok`） |
| 生产位置 | `Work\core\controller.py:704-707`：`ok, err = save_sites(new_sites)`；`if not ok: return {"error": f"写入 sites.yaml 失败：{err}"}`；成功 `return {"ok": True}`。错误文本源头为 `Work\core\site_runner.py:94-123` 的 `atomic_write_text()`（`os.replace` 失败时 `return False, f"{type(e).__name__}: {e}"`）与 `site_runner.py:155-167` 的 `save_sites()` |
| 消费位置 | **无消费点**。唯一调用点是 `Work\web\main.js:489-507`：第 491 行 `await window.pywebview.api.apply_sites_changes(siteDraft);` 的返回值**没有赋给任何变量**，也没有 `try` 之外的任何字段检查；第 499 行无条件设置 `applyHint.textContent = '✓ 已应用'`。全文件搜索 `apply_sites_changes` 仅此一处（`main.js:491`） |
| 用户可见后果 | 当 sites.yaml 或其所在目录不可写（被同步工具/杀软锁定、目录被设为只读、磁盘满、`os.replace` 因占用失败）时，用户点「应用」看到的仍是绿色「✓ 已应用」并在 2 秒后消失，随后 `refreshSitesPanel()`（`main.js:498`）从磁盘重新加载，于是**开关/删除操作全部静默回滚**：用户刚关掉的站点又变回启用，刚标记删除的站点又出现在列表里，刚点的「清除登录」也没生效（会话文件确实被删了，但 `enabled`/删除改动丢失），而他刚刚明明看到过「已应用」。界面上没有任何一行字解释这次失败。`refreshSitesPanel()` 的 `catch`（`main.js:376-380`）只在 RPC 抛异常时触发，而本分支是**正常返回**的 `{"error": ...}`，不会走 catch |

---

## H2. `get_autostart()` 的 `error` 键：后端产出、前端从不读取 —— 读注册表失败静默显示「未开启」

| 项目 | 内容 |
| --- | --- |
| 键名 | `error`（以及同一分支的 `command` 永不出现） |
| 生产位置 | `Work\core\controller.py:293-305`：`except Exception as e: return {"enabled": False, "error": str(e)}`（第 305 行）。经 `Api.get_autostart()`（`controller.py:710-711`）原样转发 |
| 消费位置 | **部分消费**。`Work\web\main.js:623-641` 的 `refreshAutostart()` 只读 `r.enabled`（第 632 行），从不读 `r.error`；`else` 分支（第 636-640 行）无条件写「未开启」 |
| 用户可见后果 | 注册表读取失败（`winreg.OpenKey` 因权限/被安全软件拦截/注册表项损坏抛异常，或 `import winreg` 失败）时，`r.enabled === false`，开关渲染成灰色关闭态、右侧显示「未开启」。用户看到的是**确定性的「没开」**，于是去点开关；此时 `set_autostart`（`controller.py:308-323`）同样可能失败，那条路径前端确实处理了（`main.js:656-659` 弹 `alert('设置失败：…')`），于是用户遇到的是「开关点了报错 / 点了没反应」这种自相矛盾的现象：状态区始终只说「未开启」，永远不说是**读不出来**。若注册表正好可写，用户还会看到「已开启」的成功假象。**注意与 `catch` 分支的区别**：`main.js:627-631` 的 `catch` 在 RPC 抛异常时显示「读取失败」，但后端这条路径是**正常返回** `{"enabled": False, "error": ...}`，不会走 catch，因此「读取失败」这四个字永远不会出现 |
| 与嫌疑点比对 | 嫌疑点「前端只读 `enabled`、不读 `error`，确认开关是否会静默显示未开启而吞掉注册表错误」→ **确认成立**（`main.js:632`、`main.js:636-640`）。仅「静默」一词需限定：吞掉的是**失败原因**，开关本身仍是灰态 |

---

# 严重程度：中

## M1. 浮窗「需要登录」横幅：前端读取 `s.site_name`，`need_login` 元素实际产出 `site_name` 但**没有 `site` 键** —— 已排除

| 项目 | 内容 |
| --- | --- |
| 键名 | `need_login[].site_name` / `.site_id` / `.reason` / `.has_credentials` |
| 生产位置 | `Work\core\site_runner.py:584-589` `_need_login_entry()` 返回 `{site_id, site_name, reason, has_credentials}`；`reason` 取值在 `site_runner.py:569-582`（`qr_code` / `no_credentials` / `bad_credentials` / `session_unstable` / `manual_required`） |
| 消费位置 | `Work\web\main.js:88-100` 读 `s.reason`（第 91 行）、`s.site_id`（第 95 行）、`s.site_name`（第 95 行），三个键都实际产出，**契约一致** |
| 结论 | **已排除**。`has_credentials`（`site_runner.py:588`）确实无消费点，但它不影响渲染：`main.js:91` 只用 `reason` 选话术，`reason` 在 `site_runner.py:569-582` 已把 `has_credentials` 的语义编码进去（无凭据 → `no_credentials`，有凭据但失败 → `bad_credentials`/`manual_required`/`session_unstable`，雨课堂 → `qr_code`），前端因此不需要重复判断。属冗余字段，非缺陷 |

---

## M2. `login()` 只回 `{ok}`，丢掉 `silent_login` 的 `reason` —— 失败时前端无法给出可操作话术

| 项目 | 内容 |
| --- | --- |
| 键名 | 缺失的 `reason`（后端已有同语义数据，未上传） |
| 生产位置 | `Work\core\controller.py:506-514`：`ok = login_site(site_id)` → `return {"ok": ok}`（第 514 行）。`login_site()` 只返回 `True/False`（`Work\core\site_runner.py:592-614`），而其内部 `auto_relogin_site()`（`site_runner.py:480-556`）**已经算出** `reason`（`mfa` / `bad_credentials` / `session_save_unusable` / `no_login_form` / `login_page_error` / `timeout` / `exception`，见 `site_runner.py:521-556` 与 `Work\adapters\auth.py:220-222`、`auth.py:620-643`），却被丢弃；`_remember_failure()`（`site_runner.py:554`）只把它存在内存里，供下一次 `get_homeworks` 的 `_need_login_entry()` 使用 |
| 消费位置 | `Work\web\main.js:129-143`：第 130 行只判断 `r.ok`；失败时第 136-137 行硬编码 `'登录未完成，点击重试'`。前端**已有** `LOGIN_HINTS` 话术表（`main.js:46-71`，含 `bad_credentials` → 「自动登录失败，账号或密码可能不对，请点按钮重新填写」、`manual_required` → 「自动登录已尝试，需要你确认一次验证码」），但这条路径拿不到 `reason`，用不上 |
| 用户可见后果 | 用户从顶部提示条点「去验证 / 重试登录」（`main.js:125-143`）后，若登录仍未成功，按钮文字只变成「登录未完成，点击重试」。真实原因（写错密码 / 仍需过人机验证 / 找不到登录表单）**在这一次交互里完全不可见**：用户会反复点同一个按钮，看着一模一样的「未完成」，无法判断该去改密码还是该去过验证码。只有等到下一次 `load()` 走 `need_login` 分支才可能看到更准确的话术，而按 `main.js:131-133`，失败时**不会**重新 `load()`，提示条保持旧文案 |

---

## M3. `items[].url` / `items[].start`：后端全量产出，前端无任何渲染点

| 项目 | 内容 |
| --- | --- |
| 键名 | `url`、`start` |
| 生产位置 | `Work\adapters\base.py:16-18` `Homework.to_dict()` 产出全部 8 个字段（`site, site_name, uid, title, course, start, end, url`），6 个适配器**全部**填充 `url`（作业原网页链接）：`thuoj.py:67`、`tyche.py:45-54`、`learn.py:118`、`yuketang.py:92`、`smartestu.py:89`、`unipus.py:50`；手动任务分支同样产出（`controller.py:455-467`，`"start": ""`、`"url": url or ""`）；`add_task()` 让用户手填 url 并入库（`controller.py:517-556`，第 528 行 `url = (url or "").strip()`，第 533 行 INSERT，第 549 行放进 `_items_cache`） |
| 消费位置 | **主窗口无消费点**：`Work\web\main.js:185-214` 的列表模板只读 `it.done`、`it.manual`、`it.site_name`、`it.course`、`it.end`、`it.uid`、`it.title`（第 186、187、192、194、196、197、198、202、206 行），**没有 `it.url`，没有 `it.start`**。全目录搜索 `Work\web\` 内 `it.url` / `.url` / `start` 均无命中（`grep` 结果：`Work\web` 下 `\.url` 零匹配，`needs_school` 是唯一命中）。`__syncFromCache`（`main.js:685-689`）也只是把整包交给同一个 `render()` |
| 用户可见后果 | ① **链接字段是纯粹的死数据**：`index.html:51` 的「链接」输入框让用户以为填了以后能点开作业，`main.js:330` 收集、`main.js:339` 上传、`controller.py:533` 落库、`controller.py:464` 回传，然后**在渲染层被丢弃**。用户填完链接保存后，任务卡片上只有平台徽章、课程、标题和「截止：…」时间，**没有任何可点击元素**；想交作业只能自己凭记忆去平台找，手动任务尤其如此（手动任务连 `site_name` 都是「手动」，没有任何来源可回忆）。② `start` 同理从未显示，用户在列表里只能看到截止时间，看不到开始时间；这一条影响较小（截止时间是用户关心的），但字段本身确认是无出口的死数据 |

---

# 严重程度：低（已排除 / 有意的设计选择，附证据）

| 键名 / 现象 | 生产位置 | 消费位置 | 结论与用户可见后果 |
| --- | --- | --- | --- |
| `set_autostart()` 的 `error` | `controller.py:308-323`（成功 `{"ok": True}` / 失败 `{"error": str(e)}`） | `main.js:643-662`：第 656-659 行 `if (r && r.error) { alert('设置失败：' + r.error); return; }` | **已排除**。前端确认读取并弹出错误文本，且第 661-662 行只在无 `error` 时才 `refreshAutostart()`，不会把失败渲染成成功。与嫌疑点描述相反，这条契约是完整的 |
| `save_site_credentials()` 各分支 | `controller.py:632-656`：写盘失败 `{"error": "写入 sites.yaml 失败：…"}`（第 652 行）、站点不存在 `{"error": "站点不存在"}`（第 656 行）、成功 `{"ok": True}`（第 655 行） | `main.js:581-615`：第 600-604 行 `if (r && r.error) { credError.textContent = r.error; return; }`；成功分支隐式落到第 605 行关窗 | **已排除**。三个分支全部被正确处理；`r.error` 会显示在凭据弹窗的错误行（`index.html:94`），且提前 `return` 不会关窗，用户能重填。注：后端**不存在**「成功与 error 同时出现」的返回，故 `main.js:600` 先判 `error` 的写法无副作用 |
| `add_task()` 的 `error` / `uid` | `controller.py:517-556`：空标题 `{"error": "标题不能为空"}`（第 520 行）、空截止 `{"error": "截止时间不能为空"}`（第 522 行）、成功 `{"ok": True, "uid": uid}`（第 556 行） | `error`：`main.js:344-347` 读取并写入 `modalError.textContent`（**已排除**，且 `main.js:332-333` 的前端预校验与后端校验并存不冲突）。`uid`：**无消费点**（`main.js:339` 的返回值只用于判 `r.error`） | `uid` 为死数据，但无用户可见后果：前端成功后关窗（`main.js:349`），随后由后端 `_sync_main()`（`controller.py:555`）与下一次 `load()` 刷新列表，不需要前端知道新 uid。属冗余，非缺陷 |
| `delete_task()` 的 `error` | `controller.py:558-573`：非手动任务 `{"error": "只能删除手动任务"}`（第 561 行）、成功 `{"ok": True}`（第 573 行） | `main.js:235-254`：第 246-250 行读取 `r.error` 并 `alert('删除失败：' + r.error)`，随后 `return`（不本地删除） | **已排除**。异常路径（DB 抛错）由 `main.js:242-245` 的 `catch` 覆盖 |
| `list_sites()` 的 `has_basic_auth` / `supports_silent_login` | `controller.py:601-603`（`"has_basic_auth": bool(...)`、`"supports_silent_login": stype != "yuketang"`） | **无消费点**（`grep` 全 `Work\web\` 零命中）。前端只用 `s.id`/`s.name`/`s.type`/`s.enabled`/`s.has_session`/`s.credentials_broken`/`s.has_credentials`/`s.needs_school`（`main.js:382-383`、`400-401`、`417-424`、`430-432`、`532`） | 死字段，无用户可见后果：`type !== 'yuketang'` 在第 417 行前端自行判断，`has_basic_auth` 对应的 UI（Basic Auth 输入框）由第 533 行按 `info.type === 'tyche'` 显示。若将来 tyche 之外出现需要 Basic Auth 的站点，`has_basic_auth` 才会变成真实缺口 |
| `get_site_credentials()` 的 `type` | `controller.py:626` `"type": s.get("type", "")` | **无消费点**：`main.js:543-548` 读了 `username`/`password`/`school`/`basic_user`/`basic_pass`/`undecryptable`，未读 `type`；标签用 `info.type`（`main.js:531`、`566`），来源是 `list_sites` | 死字段，无用户可见后果。前端有 `|| {}` 兜底（`main.js:531`），未打开过设置面板时退化为通用标签「账号 / 密码」（`main.js:566`），不会报错 |
| `get_autostart()` 的 `command` | `controller.py:300` `return {"enabled": bool(val), "command": val}` | **无消费点**（`main.js:632` 只读 `enabled`） | 死字段。附带影响与 H2 同源：后端其实能拿到注册表里的启动命令行，但前端不显示，用户无法核对「已开启」指向的是哪个 exe / bat（`_get_start_command()`，`controller.py:285-290`），换了程序路径后注册表残留旧路径也无从发现。无独立可见后果，故并入 H2，不单列 |
| 浮窗 `toggle_done()` 的 `ok` | `controller.py:481-503` 只回 `{"ok": True}`（失败走异常） | `floating-common.js:73` `try { await api().toggle_done(checkbox.dataset.uid, true); } catch (_) {}`；`update()`（`floating-common.js:49-76`）渲染的复选框**没有 `checked`**，且第 72 行先 `checkbox.disabled = true` | **已排除**。成功/失败在前端都没有可区分的显示，且主窗口的勾选路径（`main.js:216-233`）有完整的失败回滚（第 222-227 行 `e.target.checked = !done; alert(...)`）。浮窗是无确认单向操作，失败时用户下次 `layout_all()` 会看到该项仍在列表里。属有意的静默设计，非契约错配 |
| `alert_ready`/`panel_ready`/`show_main`/`hide_main`/`reset_floating`/`minimize_*`/`dismiss_*` 的 `ok` | `controller.py:330-340`、`343-345`、`356`、`369`、`378`、`388`、`397`、`426` | 全部为 fire-and-forget：`floating-common.js:36`（`await api()[...]()`，无字段读取）、`floating-common.js:42`、`floating-common.js:46`、`floating-common.js:80`、`main.js:299` | **已排除**。这些方法内部把异常都吞在 `try/except` 里（如 `controller.py:350-354`、`366-368`），永远返回 `{"ok": True}`，`ok` 本身也没有失败态可表达；前端不读它不会掩盖任何真实错误。`is_main_visible` 的 `visible` 则被 `floating-common.js:29` 正确读取 |
| `get_homeworks()` 返回字典**没有** `error` 键 | `controller.py:478-479` 只有 `items`/`errors`/`need_login`/`startup`；整体失败时把原因塞进 `errors`（`controller.py:434-437`，`site_name: "抓取"`）+ `fetch_all` 的 `errors`（`site_runner.py:251-254` 网络超时、`site_runner.py:298-301` 并发兜底、`site_runner.py:400-403`、`415-418` 单站异常） | `main.js:265-271`：只读 `items`/`errors`/`need_login`/`startup`，并用 `catch`（`main.js:272-274`）覆盖 RPC 抛异常 | **已排除**：结构一致（`errors` 元素的 `site_name`/`message` 与 `main.js:105` 的读取一致）。但记录一个**前端侧的边界问题**：`main.js:272-273` 的 `catch` 只清空 `banners` 并写错误横幅，**没有清空 `items`**，因此 `get_homeworks` 整体抛异常时，`render()`（第 271 行不会执行）留下的旧任务卡片继续显示在错误横幅下方，用户会同时看到「出错了：…」和一份过期的任务列表。此外，此时 `layout_all()`（`controller.py:472`）也不会执行，红/白浮窗保持上一次的内容不更新。这属于前端异常路径未收敛，不是字段契约错配，故不列入主表 |

---

# 附：`Api` 类全量 return 路径清单（`Work\core\controller.py`）

| 方法 | 行号 | 全部返回键 |
| --- | --- | --- |
| `alert_ready` | 330-334 | `ok` |
| `panel_ready` | 336-340 | `ok` |
| `show_main` | 343-345 | `ok` |
| `hide_main` | 347-356 | `ok` |
| `is_main_visible` | 358-359 | `visible` |
| `minimize_alert` | 362-369 | `ok` |
| `minimize_panel` | 371-378 | `ok` |
| `dismiss_alert` | 381-388 | `ok` |
| `dismiss_panel` | 390-397 | `ok` |
| `reset_floating` | 400-426 | `ok` |
| `get_homeworks` | 429-479 | `items`, `errors`, `need_login`, `startup`（异常分支 434-437 只改值不改键） |
| `toggle_done` | 481-503 | `ok`（无 `error` 分支，失败抛异常） |
| `login` | 506-514 | `ok` |
| `add_task` | 517-556 | `error`（520、522） / `ok`+`uid`（556） |
| `delete_task` | 558-573 | `error`（561） / `ok`（573） |
| `list_sites` | 576-605 | `sites` |
| `get_site_credentials` | 607-630 | `ok`+`username`+`password`+`school`+`basic_user`+`basic_pass`+`type`+`undecryptable`（619-629） / `error`（630） |
| `save_site_credentials` | 632-656 | `error`（652、656） / `ok`（655） |
| `apply_sites_changes` | 675-707 | `error`（706） / `ok`（707） |
| `get_autostart` | 710-711 | 转发 `get_autostart_status()`：`enabled`+`command`（300） / `enabled`（303） / `enabled`+`error`（305） |
| `set_autostart` | 713-714 | 转发 `set_autostart_status()`：`ok`（321） / `error`（323） |

## 前端 `window.pywebview.api.*` 调用点清单

| 调用点 | 文件:行 | 读取的字段 |
| --- | --- | --- |
| `login` | `main.js:129` | `r.ok`（130） |
| `toggle_done`（主窗口） | `main.js:221` | 仅 `await`，失败靠 `catch`（222-227）回滚勾选 |
| `delete_task` | `main.js:241` | `r.error`（246-248） |
| `get_homeworks` | `main.js:268` | `res.items`（269）、`res.errors`（270）、`res.need_login`（270）、`res.startup`（270） |
| `reset_floating` | `main.js:299` | 仅 `await` |
| `add_task` | `main.js:339` | `r.error`（344-346）；`r.uid` 未读 |
| `list_sites` | `main.js:375` | `res.sites`（381）；元素只用 id/name/type/enabled/has_session/credentials_broken/has_credentials/needs_school |
| `apply_sites_changes` | `main.js:491` | **无**（返回值未接收） |
| `get_site_credentials` | `main.js:537` | `r.ok`（543）、`username`（544）、`password`（545）、`school`（546）、`basic_user`（547）、`basic_pass`（548）、`undecryptable`（549）、`r.error`（553-555）；`type` 未读 |
| `save_site_credentials` | `main.js:595` | `r.error`（600-602）；成功隐式（605） |
| `get_autostart` | `main.js:626` | `r.enabled`（632）；`error`、`command` 未读 |
| `set_autostart` | `main.js:649` | `r.error`（656-658） |
| `is_main_visible` | `floating-common.js:28` | `result.visible`（29） |
| `hide_main` / `show_main` | `floating-common.js:36` | 仅 `await` |
| `minimize_alert` / `minimize_panel` | `floating-common.js:42` | 仅 `await` |
| `dismiss_alert` / `dismiss_panel` | `floating-common.js:46` | 仅 `await` |
| `toggle_done`（浮窗） | `floating-common.js:73` | 仅 `await`，`catch` 为空 |
| `alert_ready` / `panel_ready` | `floating-common.js:80` | 仅 `await` |

---

# 修复优先级建议

1. **H1（必修）**：`main.js:489-507` 接收 `apply_sites_changes` 的返回值，`if (r && r.error)` 时把 `applyHint` 设为红色错误文案并**提前 `return`**（不要显示「✓ 已应用」，也不要让用户以为改动生效）。建议复用 `save_site_credentials` 已验证的写法（`main.js:600-604`）。
2. **H2（必修）**：`main.js:632` 之后增加 `if (r && r.error)` 分支，把状态区显示为「读取失败」并把 `r.error` 作为 `title` 或提示文案，避免「未开启」这种确定性误报。
3. **M2（建议）**：`controller.py:514` 的 `login()` 改为回传 `login_site()` 的 `(ok, reason)`（`site_runner.py:592-614` 需同步扩展签名），前端在 `main.js:136-137` 用 `LOGIN_HINTS[r.reason]` 给出与 `need_login` 分支一致的话术。
4. **M3（建议，产品决策）**：在 `main.js:200-213` 的卡片模板中，当 `it.url` 非空时把标题渲染成可点击链接（`target="_blank"`），否则用户填的「链接」和适配器抓到的原网页地址始终无从使用。
5. **回归测试**：S1 已由并发改动修复，需要覆盖两条路径 —— ①首选目录不可写 + `%LOCALAPPDATA%` 可写 → 顶部出现黄色 `startup` 提示条（`main.js:78-86`，样式 `main.css:85-89`）；②未回退时 `startup` 为空数组 → 无提示条（`paths.py:161-162`）。
