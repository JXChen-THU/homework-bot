"""静默登录公共设施。

设计目标：有账号密码时，会话过期后程序在后台静默重登，用户无感知。

每个适配器只需要提供三样东西（在各自模块里定义）：
    LOGIN          登录配置（登录页 URL、选择器、提交方式等）
    is_logged_in(page, site)   判断当前浏览器上下文是否已登录
    可选的 helper  例如 _get_courses(page)

本模块据此提供：
    silent_login(site, base_dir, *, timeout_ms=60000, log=None, offscreen=False)
        无头（或屏幕外真实窗口）静默登录；返回 `(ok, reason)`，不抛异常。
        reason 取值：
          ok / session_valid / session_save_unusable / no_credentials / no_adapter_support /
          mfa / bad_credentials / school_select_failed / no_login_form /
          login_page_error / timeout / exception
    interactive_login(site, base_dir, *, timeout_s=600, keep_alive=False, prefill=True)
        可见窗口登录，供人工扫码／过验证码；返回布尔（keep_alive 时返回四元组）

两者都会在成功后写入 data/<session>.json。会话文件路径统一走 `session_path()`。
"""

import os
import threading
import time
from contextlib import contextmanager

from playwright.sync_api import sync_playwright


# ============================================================
# 浏览器会话
# 每个站点各自启动一个无头 Edge，用完就关。
#
# 曾经尝试过「一次抓取共享一个浏览器」来省启动开销，实测否决：
# 启动一个 Edge 只要 0.24 秒（新建 context 0.19 秒），6 个站点各自启动
# 也就 1.4 秒，并行下更少，省不到 1 秒；而 Playwright 的同步 API 是
# 线程绑定的，跨线程复用同一个 browser 会直接崩
# （TargetClosedError / greenlet cannot switch to a different thread）。
# ============================================================
@contextmanager
def browser_session():
    """提供一个无头 Edge 浏览器，退出时自动关闭。

    各站点用独立 context（storage_state 不同），cookie 互不干扰。
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="msedge")
        try:
            yield browser
        finally:
            try:
                browser.close()
            except Exception:
                pass


# 站点没有显式配置 login_url 时，按类型猜测登录页
_DEFAULT_LOGIN_URLS = {
    "learn": "https://learn.tsinghua.edu.cn/f/login",
}


# ============================================================
# 小工具
# ============================================================
def _plain(value):
    """把 sites.yaml 里读到的字段还原成明文。

    兼容三种情况：密文（DPAPI）、旧的明文、以及空值。
    密文解不开时（换了电脑或换了 Windows 账户）返回空串，
    这样 has_credentials 会判为「没填账号密码」，前端会提示用户重新填。
    """
    try:
        from core.secrets import decrypt
    except Exception:
        # 拿不到解密模块时**不能**把原值当明文返回：
        # 那会把 "enc:v1:..." 当成账号密码填进登录表单。
        # 按「解不开」处理，让上层提示用户重新填写。
        return ""
    plain, _ok = decrypt(value)
    return plain


def credentials(site):
    """取出站点账号密码（已解密）；兼容 Tyche 历史上的 basic_user/basic_pass 字段。"""
    user = _plain(site.get("username")) or _plain(site.get("basic_user"))
    pwd = _plain(site.get("password")) or _plain(site.get("basic_pass"))
    return user, pwd


def has_credentials(site):
    """站点有没有可用的账号密码。

    注意用的是「解密后的值」：如果 sites.yaml 里的密文在别的电脑上解不开，
    这里会返回 False，让上层提示用户重新填写，而不是拿空密码去登录。
    """
    user, pwd = credentials(site)
    return bool(user and pwd)


def credentials_broken(site):
    """账号密码存在但解不开（多半是换了电脑/Windows 账户）。"""
    try:
        from core.secrets import looks_undecryptable
    except Exception:
        return False
    for key in ("username", "password", "basic_user", "basic_pass"):
        if looks_undecryptable(site.get(key)):
            return True
    return False


def http_credentials(site):
    """Tyche 需要 HTTP Basic Auth（打开站点时的浏览器弹窗）。"""
    user = _plain(site.get("basic_user"))
    pwd = _plain(site.get("basic_pass"))
    if user and pwd:
        return {"username": user, "password": pwd}
    return None


def session_path(base_dir, site):
    """会话文件路径（转发到 base.session_path，保证全仓一套口径）。"""
    from .base import session_path as _base_session_path
    return _base_session_path(base_dir, site)


def login_url(site):
    return (site.get("login_url")
            or _DEFAULT_LOGIN_URLS.get(site.get("type"))
            or site.get("url")
            or site.get("base_url")
            or "")


def site_urls_of(site):
    """站点自己的地址（用来把"候选登录页 == 站点自身地址"排除掉）。

    为什么必须有这个：`login_url()` 在没配 `login_url`、类型也没默认值时会
    **回退成站点自己的 url**（清华 OJ 就是这种）。那种情况下"登录页"和
    "内容页"是同一个地址，`page_on_login_page` 会把每一次正常抓取都判成
    "需要登录" —— 每次刷新都被提前中断、还触发一次白跑的重登。
    """
    return [u for u in (site.get("url"), site.get("base_url")) if u]


def wait_for_ready(page, condition, *, timeout_ms=5000, interval_ms=200, what="",
                   login_urls=None, site_hosts=(), site_urls=()):
    """轮询等待某个条件成立就立刻继续（替代固定 sleep 和 networkidle）。

    为什么不用 networkidle：它要求网络彻底安静 500ms，而很多学习平台都有
    轮询/埋点请求，会一直等到超时。实测网络学堂在 domcontentloaded 后 1.3 秒
    就能拿到登录态和 CSRF，但 networkidle 还要再等 4 秒。

    ★ `login_urls`：给了就在**每次轮询时**顺手看一眼当前 URL，只要落在登录页上
    就立刻结束等待（返回 False）。这是「会话过期」这条路径上最值钱的一步：
    页面早就跳到登录页了，而我们要等的东西（作业列表）永远不会出现，
    于是只能把 timeout_ms 睡满。实测过期会话从 15 秒降到 0.1~0.3 秒。
    反过来，**只有在确认是登录页时才提前结束**，"页面只是慢"不会被误判 ——
    这正是敢把预检超时压到 SESSION_PROBE_MS 的前提。

    condition(page) 返回真值即视为就绪。返回是否在超时前就绪。
    """
    deadline = time.time() + timeout_ms / 1000.0
    while True:
        try:
            if condition(page):
                return True
        except Exception:
            pass
        if login_urls and page_on_login_page(page, login_urls, site_hosts, site_urls):
            return False
        if time.time() >= deadline:
            break
        try:
            page.wait_for_timeout(interval_ms)
        except Exception:
            return False
    if what:
        print(f"[wait] {what} 等待超时（{timeout_ms}ms），继续执行")
    return False


# ============================================================
# 抓取期的两个超时（会话预检 / 数据就绪）
#
# 为什么要把它们分开、而且预检可以压到 1 秒：
#   「这个会话还能不能用」这件事本身是**免费**的 —— 会话有效时页面/接口
#   几百毫秒内就给出内容；会话失效时浏览器是**被直接跳到登录页**的，
#   作业列表永远不会出现。实测（本机，无头请求）：
#       learn 首页 → 0.10s 且最终 URL 已经是 /f/login
#       清华 OJ 作业页 0.06s / U校园 0.08s / 数你最灵 0.04s
#   也就是说"等 15 秒"里的 15 秒全是干等：等一个永远不来的条件。
#
#   敢压到 1 秒的前提是 wait_for_ready(login_urls=...) 的**提前结束**：
#   一旦确认页面在登录页上就立刻返回，不睡满；而"页面只是慢"不会被误判，
#   因为只有"确实在登录页"才提前结束。
#
# 数据就看 session 有效时的内容就绪，给 DATA_READY_MS：
#   学习平台的作业接口偶尔要几秒（尤其网络学堂要逐门课拉），
#   抢在 1 秒丢掉会让一次本来能成功的抓取变成"需要登录"，
#   进而触发一次白跑的重登 —— 那比多等几秒更慢。
#
# ★ 用户要求把这一档从 15 秒压到 5 秒（原话："而且把 15s 改成 5s"）。
#   配套的行为变化很关键：超时**不再只是报错** —— site_runner 会把
#   "超时且没有任何失效信号"当成"会话可能已被静默作废"，清除该站点的
#   登录记录并走一次静默重登（见 core/site_runner._discard_reason）。
#   所以这个值同时决定了"慢站点被误判成失效"的概率：压得越短越容易触发
#   一次真实的重新登录。5 秒是用户权衡后的选择。
# ============================================================
SESSION_PROBE_MS = 1000
DATA_READY_MS = 5000


def timeout_detail(what, timed_out=True):
    """"等了但没就绪"时给用户看的说明（5 个适配器共用一份口径）。

    这种超时**不能确定**会话是不是失效了：
      - 会话真失效时，绝大多数平台会把页面跳到登录页（那条路已经被
        wait_for_ready 的早退当场抓住，走的是 LoginRequiredError）；
      - 剩下这种"没跳到登录页、只是迟迟没内容"的情况，既可能是网络/平台慢，
        也**可能**是某个 SPA 静默登出了（不给任何失效信号）。

    现在的处置是：**任何抓取失败都会尝试静默重登（最多连续 3 次）**；这条超时
    额外会在重登前先清掉该站点的登录记录（`site_runner._should_clear_session_before_relogin`），
    因为"一个字节都没回"最可能就是会话被静默作废。所以这段文字只会在
    「连续重登 3 次仍超时」或「压根没法自动重登（没账密 / 平台不支持 / 账密解不开）」
    时出现在界面上 —— 那时给用户一个手动出口仍然是必要的。
    """
    what = what or "页面"
    return (f"{what} 在 {DATA_READY_MS // 1000} 秒内没有返回内容。"
            "可能是网络或平台较慢，也可能登录状态已失效；"
            "若点「刷新任务」重试仍不行，请点「登录」手动登录一次。")



# "认证域名/路径"的特征词。凡是 URL 里带这些字样且**不属于已配置的站点主机**，
# 就认为当前页面在认证流程里。
# 为什么可以只看整个 URL：站点自己的内容页不会出现 /sso、/auth/login、
# passport、cas 这类路径段；而真正的 SSO 页正是靠这些字样识别的。
_AUTH_URL_HINTS = ("sso", "passport", "cas", "/auth/login", "id.tsinghua.edu.cn")


def _split_url(u):
    """把 URL 拆成 (host, path)。没有主机名的（例如 "id.tsinghua.edu.cn"）按主机名处理。"""
    u = str(u or "").strip().lower()
    if not u:
        return "", ""
    if "://" in u:
        u = u.split("://", 1)[1]
    u = u.split("?", 1)[0].split("#", 1)[0]
    if "/" in u:
        host, path = u.split("/", 1)
        return host, "/" + path
    return u, ""


def page_on_login_page(page, login_urls, site_hosts=(), site_urls=()):
    """当前页面是否落在登录页/认证页上。

    判定只看 **URL**，不看正文 —— 正文里的"登录""课程"字样在 SSO 页面上会
    互相污染，本仓库已经在这上面踩过坑。这个函数在本仓库里踩了三次，
    每次都是把**正常内容页**判成了登录页（那会让每一次抓取都被提前中断）：

      1. 只看主机名 → `learn.tsinghua.edu.cn` 下所有页面（含作业列表）全中；
      2. 主机名与路径**分开**匹配 → `uai.unipus.cn/portal/sso/login` 的
         路径段从第一个 `/` 起算，`/portal` 命中了 `uai.unipus.cn/portal`；
      3. **候选登录页本身等于站点自己的地址** → `login_url(site)` 在没有
         `login_url` / 没有类型默认值时会回退成 `site["url"]`
         （清华 OJ 就是这样），于是"作业页等于登录页"，永远判成需要登录。
         这一条由 `site_urls` 兜底：等于站点自己地址的候选一律不算登录页。

    ★ 两个档必须分清：
      - 配置里的登录页：与内容页**同主机**，必须"主机 + 路径"落在同一段 URL 上；
      - 认证流程页（`id.tsinghua.edu.cn`、URL 里带 /sso、/auth/login、passport、cas）：
        整站/整段都是认证，按 URL 特征认；`site_hosts` 里声明过的站点主机不参与
        （避免某个平台把自己的内容页放在看起来像认证的路径下被误伤）。

    调用方仍会再做一次正式判定（各自的 _is_logged_in / _on_logged_in_page），
    所以这里宁可宽一点：多提前结束一次，代价只是快一点，不会改变结论。
    """
    try:
        url = (page.url or "")
    except Exception:
        return False
    if not url:
        return False
    low = url.lower()

    for u in login_urls:
        host, path = _split_url(u)
        if not host:
            continue
        # 候选就是站点自己的地址 → 它不是登录页
        if _same_location(u, site_urls):
            continue
        # 档 1：配置里的登录页 → 主机和路径必须落在同一段 URL 上
        if path and len(path) > 1:
            start = low.find(host)
            if start >= 0 and path in low[start + len(host):]:
                return True
        # 档 2：认证流程页 → URL 特征命中，且不在站点自己的主机上
        host_in_site = any(h and h in low for h in site_hosts)
        if not host_in_site and any(h in low for h in _AUTH_URL_HINTS):
            return True
    return False


def _same_location(candidate, others):
    """candidate 是否与 others 中的任意一条指向同一个位置（忽略 scheme / 尾斜杠）。"""
    ch, cp = _split_url(candidate)
    if not ch:
        return False
    ckey = (ch, cp.rstrip("/"))
    for o in others or ():
        oh, op = _split_url(o)
        if oh and (oh, op.rstrip("/")) == ckey:
            return True
    return False

    """按顺序尝试选择器，填第一个存在的可见元素。返回用到的选择器或 None。"""
    for sel in selectors:
        if not sel:
            continue
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            try:
                if not loc.is_visible():
                    continue
            except Exception:
                pass
            loc.fill(value, timeout=timeout)
            return sel
        except Exception:
            continue
    return None


def _wait_for_any(page, selectors, timeout_ms, interval_ms=200):
    """轮询等待任意一个选择器出现可见元素；出现就立刻返回 True。

    替代登录流程里的固定 `wait_for_timeout`：固定 sleep 两头都吃亏 ——
    快站点白等好几秒（用户实测"给出账号密码错误要等很久"），慢站点还可能等不够。
    这里沿用 `wait_for_ready` 的思路：条件成立就走，最多等 timeout_ms（上限不变）。
    """
    sels = [s for s in (selectors or []) if s]
    if not sels:
        return False
    deadline = time.time() + timeout_ms / 1000.0
    while True:
        for sel in sels:
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    return True
            except Exception:
                pass
        if time.time() >= deadline:
            return False
        try:
            page.wait_for_timeout(interval_ms)
        except Exception:
            return False


def _fill_first(page, selectors, value, timeout=8000):
    """按顺序尝试选择器，填第一个存在的可见元素。返回用到的选择器或 None。

    ★ 这个函数曾经从工作区里消失（定义被删、8 处调用还在），后果是
      silent_login() 一进到"填账号密码"就抛 NameError，被外层 except 吞成
      reason="exception"，于是**所有平台的静默登录全部失效**，用户看到的是
      "改对了密码也登不上"。从 git HEAD 恢复，实现与 _click_first 对称。
      改这个文件时请留意：调用点有 8 处，缺了定义不会有语法错误，
      只会在运行到那一步时才炸。
    """
    for sel in selectors:
        if not sel:
            continue
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            try:
                if not loc.is_visible():
                    continue
            except Exception:
                pass
            loc.fill(value, timeout=timeout)
            return sel
        except Exception:
            continue
    return None


def _click_first(page, selectors, timeout=6000):
    """按顺序尝试选择器，点第一个存在的可见元素。返回用到的选择器或 None。"""
    for sel in selectors:
        if not sel:
            continue
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            try:
                if not loc.is_visible():
                    continue
            except Exception:
                pass
            loc.click(timeout=timeout)
            return sel
        except Exception:
            continue
    return None


def _context_args(site, storage_state=None):
    args = {}
    if storage_state:
        args["storage_state"] = storage_state
    hc = http_credentials(site)
    if hc and hc["username"]:
        args["http_credentials"] = hc
    return args


# ============================================================
# 静默登录
# ============================================================
def silent_login(site, base_dir, *, timeout_ms=60000, log=None, offscreen=False):
    """静默登录。

    offscreen=False（默认）：无头浏览器。
    offscreen=True：真实窗口（headless=False），但把窗口移到屏幕外。
        实测清华 SSO 会挑战无头浏览器、却放行真实窗口；窗口放在屏幕外
        用户看不见，效果等同于「后台静默」，只是多了一个不可见的窗口。

    返回 (ok, reason)：
        ok=True            登录成功，会话已保存
        ok=False           失败；reason 说明原因
                           （no_credentials / mfa / bad_credentials /
                             no_login_form / login_page_error / timeout / exception）
    """
    def _log(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass
        else:
            print(f"[silent-login] {site.get('id')}: {msg}")

    user, pwd = credentials(site)
    if not (user and pwd):
        return False, "no_credentials"

    module = _adapter_module(site)
    if module is None or not hasattr(module, "is_logged_in"):
        return False, "no_adapter_support"

    validate = getattr(module, "is_logged_in")
    cfg = getattr(module, "LOGIN", {}) or {}
    url = login_url(site)
    sess = session_path(base_dir, site)
    os.makedirs(base_dir, exist_ok=True)

    # 判定窗口在**登录页真正打开之后**才开始计时（原实现在启动浏览器之前就定 deadline，
    # 慢站上 page.goto 吃掉 45 秒后 _wait_for_login 一进去就只剩几秒，
    # 用户拿到误导性的 timeout 并进 300 秒冷却，而真实原因只是"页面慢"）。
    deadline = None

    # 屏幕外窗口：位置给到 -32000，Windows 会夹到约 -21845，仍在所有显示器之外
    launch_args = {}
    if offscreen:
        launch_args = {
            "headless": False,
            "args": ["--window-position=-32000,-32000", "--window-size=1200,800"],
        }
    else:
        launch_args = {"headless": True}

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", **launch_args)

        # 带上已有会话：既能直接判断「会话还有效」，也让清华 SSO 之类的站点
        # 有机会复用上下文里的凭证。文件损坏时退回全新上下文。
        seed = sess if os.path.exists(sess) else None
        try:
            ctx = browser.new_context(**_context_args(site, storage_state=seed))
        except Exception as e:
            _log(f"载入旧会话失败，改用全新上下文：{str(e)[:120]}")
            ctx = browser.new_context(**_context_args(site))

        page = ctx.new_page()
        page.set_default_timeout(20000)

        # 登录表单的选择器：下面所有"等页面就绪"都以"表单出现"为准，
        # 不再睡固定秒数（见 _wait_for_any）。
        form_sels = list(cfg.get("username") or []) + list(cfg.get("password") or [])

        def _wait_form(cap_ms, what):
            if _wait_for_any(page, form_sels, cap_ms):
                return True
            _log(f"{what}：{cap_ms}ms 内没看到登录输入框，继续按流程尝试")
            return False

        try:
            # 1) 先看现有会话是否还有效（有 storage_state 时）
            if seed:
                try:
                    page.goto(site.get("base_url") or url,
                              wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(2500)
                    if _safe_validate(validate, page, site):
                        _log("原有会话仍然有效")
                        browser.close()
                        return True, "session_valid"
                except Exception as e:
                    _log(f"复用会话检查失败：{str(e)[:120]}")

            # 2) 打开登录页
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                _log(f"打开登录页失败：{str(e)[:120]}")
                browser.close()
                return False, "login_page_error"
            # 3) 需要先点开的入口（例如网络学堂的「登录」按钮）
            pre_click = cfg.get("pre_click")
            if pre_click:
                # 有入口要点的站点，表单要点了才出来：先等入口出现再点，
                # 点完再等表单（都是"出现就走"，不再睡固定秒数）。
                _wait_for_any(page, pre_click, cfg.get("after_goto_wait", 2500))
                hit = _click_first(page, pre_click, timeout=8000)
                if hit:
                    _log(f"点击登录入口：{hit}")
                    _wait_form(cfg.get("after_pre_click_wait", 4000), "点击登录入口后")
            else:
                _wait_form(cfg.get("after_goto_wait", 2500), "打开登录页后")

            # 3b) 第二段点击（例如清华 OJ：先展开下拉，再点「使用 清华ID 登录」）
            pre_click2 = cfg.get("pre_click2")
            if pre_click2:
                _wait_for_any(page, pre_click2, cfg.get("after_pre_click_wait", 4000))
                hit2 = _click_first(page, pre_click2, timeout=8000)
                if hit2:
                    _log(f"点击登录入口2：{hit2}")
                    _wait_form(cfg.get("after_pre_click2_wait", 5000), "点击登录入口2后")

            # 4) 选学校之类的选择器（数你最灵）
            #    选校失败不能直接当成"密码错误"：以前丢弃返回值继续提交，
            #    服务端回「用户名或密码错误」，前端就把用户引去改一个正确的密码，
            #    还白搭 1800 秒冷却。这里只记录状态，等真正失败时再降级 reason。
            school_failed = False
            school = site.get("school")
            if school and cfg.get("school_open"):
                if not _select_school(page, cfg, school, _log):
                    school_failed = True
                    _log(f"选校未成功（{school}），若随后登录失败将按「选校失败」上报")

            # 5) 填账号密码
            user_sel = _fill_first(page, cfg.get("username") or [], user)
            pass_sel = _fill_first(page, cfg.get("password") or [], pwd)

            if not (user_sel and pass_sel):
                # 还没填表就撞上二次验证（清华 SSO 会在某些情况下跳过账密、直接要求验证）
                if _looks_like_mfa(page, cfg):
                    _log("打开登录页后直接进入二次验证，需要人工介入")
                    browser.close()
                    return False, "mfa"

                # 有些站点点「登录」不一定跳到表单，直接打开登录页兜底
                if cfg.get("fallback_url"):
                    _log(f"未出现登录表单，改用兜底登录页 {cfg['fallback_url']}")
                    try:
                        page.goto(cfg["fallback_url"], wait_until="domcontentloaded", timeout=45000)
                        _wait_form(cfg.get("after_fallback_wait", 4000), "兜底登录页")
                    except Exception as e:
                        _log(f"兜底登录页打开失败：{str(e)[:120]}")
                    if _looks_like_mfa(page, cfg):
                        _log("兜底登录页也是二次验证，需要人工介入")
                        browser.close()
                        return False, "mfa"
                    user_sel = _fill_first(page, cfg.get("username") or [], user)
                    pass_sel = _fill_first(page, cfg.get("password") or [], pwd)

            if not (user_sel and pass_sel):
                _log(f"找不到登录输入框（user={user_sel} pass={pass_sel}）")
                browser.close()
                return False, "no_login_form"
            _log(f"已填写账号密码（{user_sel} / {pass_sel}）")
            page.wait_for_timeout(300)

            # 6) 勾选「记住我」和协议类复选框
            for sel in list(cfg.get("remember") or []) + list(cfg.get("agree") or []):
                try:
                    loc = page.locator(sel).first
                    if loc.count() and not loc.is_checked():
                        loc.check(timeout=4000)
                        _log(f"已勾选 {sel}")
                except Exception:
                    pass

            # 7) 提交
            submit = cfg.get("submit") or []
            hit = _click_first(page, submit, timeout=8000)
            if hit:
                _log(f"点击登录按钮：{hit}")
            else:
                try:
                    page.press(pass_sel, "Enter")
                    _log("回车提交")
                except Exception:
                    pass

            # 8) 等待登录结果 —— 判定窗口从这一刻开始算（见上面的 deadline 说明）
            deadline = time.time() + timeout_ms / 1000.0
            ok, reason = _wait_for_login(page, validate, site, deadline, cfg)
            if not ok and reason == "bad_credentials" and school_failed:
                # 选校没成功却被服务端回了凭据错误：真正坏掉的是选校。
                # 降级 reason 可以同时避免 1800 秒冷却和"去改密码"的错误引导。
                _log("登录失败原因是凭据错误，但本次选校未成功 → 按「选校失败」上报")
                reason = "school_select_failed"
            if ok:
                # 等会话稳定再存盘：刚跳转完成时 cookie 可能还没落定，
                # 存早了会得到一个「当前页面能用、重新加载却无效」的会话文件。
                try:
                    page.wait_for_timeout(1500)
                except Exception:
                    pass

                ctx.storage_state(path=sess)

                # 关键：用存出来的文件重建上下文再验一次，
                # 确保下次 fetch_homeworks 重新加载这个文件时确实可用。
                saved_ok, why = _verify_saved_session(
                    browser, site, sess, validate, cfg, _context_args(site), _log)
                if saved_ok:
                    _log("静默登录成功，会话已保存并验证可用")
                    browser.close()
                    return True, "ok"

                _log(f"会话保存后复核失败（{why}），重试一次")
                try:
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
                ctx.storage_state(path=sess)
                saved_ok2, why2 = _verify_saved_session(
                    browser, site, sess, validate, cfg, _context_args(site), _log)
                if saved_ok2:
                    _log("静默登录成功（第二次复核通过）")
                    browser.close()
                    return True, "ok"

                _log(f"会话保存后仍不可用（{why2}）")
                browser.close()
                return False, "session_save_unusable"

            _log(f"静默登录失败：{reason}")
            browser.close()
            return False, reason
        except Exception as e:
            _log(f"静默登录异常：{type(e).__name__}: {str(e)[:160]}")
            try:
                browser.close()
            except Exception:
                pass
            return False, "exception"


def _adapter_module(site):
    """按站点 type 找到对应适配器模块。"""
    from adapters import REGISTRY
    return REGISTRY.get(site.get("type"))


def _safe_validate(validate, page, site, *, navigate=False, cfg=None, timeout=20000):
    """调用适配器的 is_logged_in。

    navigate=True 时，如果直接判断失败，会再导航到站点的目标页面重试一次。
    这是必要的：有些站点登录后会停在首页（清华 OJ 停在 `/`），
    而作业在 `/course/8/home`，在首页上抠不到东西会被误判成未登录。

    关键：**停在登录页/二次验证页时绝不导航**。
    否则会把「需要人工验证」的现场冲掉，导致判定不出来、只能一路等到超时。
    """
    try:
        if validate(page, site):
            return True
    except Exception:
        pass

    if not navigate:
        return False

    # 还在登录/验证流程里 → 不做导航复核
    try:
        url = page.url.lower()
    except Exception:
        url = ""
    login_markers = ("login", "sso", "auth", "id.tsinghua.edu.cn", "checksingle")
    if any(k in url for k in login_markers):
        return False
    if cfg and _looks_like_mfa(page, cfg):
        return False

    target = site.get("url") or site.get("base_url")
    if not target:
        return False
    try:
        if url.split("#")[0] == target.split("#")[0].lower():
            # 已经在目标页了，再跳一次没意义
            return False
        page.goto(target, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        return bool(validate(page, site))
    except Exception:
        return False


def _select_school(page, cfg, school, log):
    """数你最灵：先点开学校选择、搜索、点中目标学校。"""
    try:
        opened = _click_first(page, cfg.get("school_open") or [], timeout=8000)
        if not opened:
            return False
        page.wait_for_timeout(1500)
        searched = _fill_first(page, cfg.get("school_search") or [], school, timeout=6000)
        if searched:
            page.wait_for_timeout(1500)
        picked = _click_first(page, cfg.get("school_pick") or [], timeout=6000)
        if not picked:
            # 回退：直接用文本点击
            try:
                page.get_by_text(school, exact=True).first.click(timeout=6000)
                picked = "text"
            except Exception:
                pass
        log(f"选择学校 {school}：{picked}")
        page.wait_for_timeout(1200)
        return bool(picked)
    except Exception as e:
        log(f"选择学校失败：{str(e)[:120]}")
        return False


def _looks_like_mfa(page, cfg):
    """判断当前页面是不是「必须人工介入」的二次验证页。

    只看可见的选择器和明确的句子，不用泛化的「验证码」字样
    （正常登录页本身也可能出现「验证码登录」）。
    """
    for sel in (cfg.get("mfa_selectors") or [".second-verify", "#secondVerify"]):
        if not sel:
            continue
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                return True
        except Exception:
            pass

    # URL 上出现明确的二次认证路径（清华 SSO 的 checkSingle）
    try:
        url = page.url.lower()
    except Exception:
        url = ""
    if any(k in url for k in ("checksingle", "secondverify", "second-verify", "/mfa")):
        return True

    phrases = cfg.get("mfa_phrases") or []
    if not phrases:
        return False
    try:
        text = page.evaluate(
            "() => (document.body ? document.body.innerText : '').slice(0, 1500)")
    except Exception:
        return False
    return bool(text) and any(p in text for p in phrases)


def _verify_saved_session(browser, site, sess_path, validate, cfg, ctx_args, log):
    """用「存出来的会话文件」新建一个上下文，确认登录态真的可用。

    这一步是为了消除「当前页面校验通过、但存盘文件里的会话不完整」的假成功：
    下次 fetch_homeworks 是从这个文件重建上下文的，文件不可用就等于没登录。

    注意：必须复用调用方已经打开的 browser。Playwright 的同步 API
    不允许嵌套 sync_playwright()，否则会报「inside the asyncio loop」。
    返回 (ok, 说明)。
    """
    target = site.get("url") or site.get("base_url") or login_url(site)
    try:
        ctx = browser.new_context(storage_state=sess_path, **ctx_args)
    except Exception as e:
        return False, f"重建上下文失败 {type(e).__name__}: {str(e)[:100]}"

    try:
        page = ctx.new_page()
        page.set_default_timeout(20000)
        page.goto(target, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
        if validate(page, site):
            return True, "ok"
        # 有些站点登录后停在首页，导航复核一次
        if _safe_validate(validate, page, site, navigate=True, cfg=cfg):
            return True, "ok(导航复核)"
        return False, "复用的会话无法通过校验"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def _wait_for_login(page, validate, site, deadline, cfg):
    """轮询判断是否登录成功；同时识别「账号密码错误」和「必须人工介入」两类情况。

    注意：不能拿页面正文去匹配「验证码」这种泛化关键词——
    正常登录页本身就可能出现「验证码登录」之类的字样（U校园就是如此）。
    必须用「可见的二次验证元素 / 明确的二次验证句子」来判断。

    判定顺序也有讲究：**二次验证优先于正文扫描**。
    正文扫描用的是"页面里出现某句话"这种弱证据，而二次验证页面
    （清华 SSO、校园 MFA）的正文里完全可能出现「登录失败」「无法登录」
    这类帮助/提示文案；顺序反了就会把真 MFA 判成"密码错"，
    冷却从 300 秒变 1800 秒，还把用户引去改一个正确的密码。
    """
    # 元素扫描可以宽一点（错误类元素本身就限定了范围）
    error_markers = cfg.get("error_markers") or [
        # 用足够长的整句，避免把正常登录页上的「忘记密码」之类误判成失败
        "您的用户名或密码不正确", "用户名或密码不正确", "用户名或密码错误",
        "账号或密码不正确", "账号或密码错误", "密码不正确", "密码错误",
        "账号不存在", "账户不存在", "请确认提供的账户",
    ]
    # 正文扫描只认足够独特的长句：泛化短语（「无法登录」「登录失败」）
    # 在正常页脚、帮助文案、MFA 说明里都可能出现，不能作为判据。
    body_markers = cfg.get("body_error_markers") or [
        "您的用户名或密码不正确", "用户名或密码不正确", "用户名或密码错误",
        "账号或密码不正确", "账号或密码错误", "密码不正确", "密码错误",
        "账号不存在", "账户不存在", "请确认提供的账户",
        "账号已被锁定", "账户已被锁定", "密码输入次数过多",
    ]

    nav_checked = set()   # 已经为哪些 URL 做过「导航到目标页复核」
    while time.time() < deadline:
        if _safe_validate(validate, page, site):
            return True, "ok"

        # 直接判断失败时，导航到站点目标页复核一次
        # （覆盖「登录后停在首页、作业在子页面」的站点，例如清华 OJ）
        try:
            cur = page.url.split("#")[0]
        except Exception:
            cur = ""
        if cur and cur not in nav_checked:
            nav_checked.add(cur)
            if _safe_validate(validate, page, site, navigate=True, cfg=cfg):
                return True, "ok"

        # 1) 明确的登录失败提示（错误类元素）→ 立刻返回，不要干等
        try:
            err = page.evaluate(
                r"""() => {
                    const sel = '[class*=error],[class*=Error],[role=alert],.ant-message,.usso-message';
                    const out = [];
                    document.querySelectorAll(sel).forEach(el => {
                        const t = (el.innerText || '').replace(/\s+/g,' ').trim();
                        if (t && t.length < 200) out.push(t);
                    });
                    return out.slice(0, 6).join(' | ');
                }""")
        except Exception:
            err = ""
        if err and any(m in err for m in error_markers):
            return False, "bad_credentials"

        # 2) 真正的二次验证界面（必须排在正文扫描之前）
        if _looks_like_mfa(page, cfg):
            return False, "mfa"

        # 3) 正文里的明确失败长句
        #    只扫元素是不够的：清华 SSO 报「您的用户名或密码不正确，请重试！」
        #    时那句话不在任何 error 类元素里，结果会一路轮询到超时（实测白等 60 秒）。
        try:
            body = page.evaluate(
                "() => (document.body ? document.body.innerText : '').slice(0, 2000)")
        except Exception:
            body = ""
        if body and any(m in body for m in body_markers):
            return False, "bad_credentials"

        try:
            page.wait_for_timeout(1200)
        except Exception:
            break

    # 超时前最后再复核一次（有些站点跳转较慢）
    if _safe_validate(validate, page, site, navigate=True, cfg=cfg):
        return True, "ok"
    return False, "timeout"


# ============================================================
# 人工登录（可见窗口）
# ============================================================
def interactive_login(site, base_dir, *, timeout_s=600, keep_alive=False, prefill=True):
    """打开可见浏览器让用户完成登录（扫码、验证码、二次认证等）。

    prefill=True 时会尽量先把账号密码和「登录」入口点好，
    这样清华 SSO 的二次验证场景下用户只需要输入验证码。

    keep_alive=True 时不关闭浏览器，返回 (ok, browser, context, page)，
    由调用方负责后续处理（用于「弹出窗口让用户指认元素」的场景）。
    否则返回布尔值。
    """
    module = _adapter_module(site)
    validate = getattr(module, "is_logged_in", None) if module else None
    if validate is None:
        print(f"[interactive-login] {site.get('id')}: 适配器未提供 is_logged_in")
        return (False, None, None, None) if keep_alive else False


    cfg = getattr(module, "LOGIN", {}) or {}
    user, pwd = credentials(site)
    sess = session_path(base_dir, site)
    os.makedirs(base_dir, exist_ok=True)
    url = login_url(site)

    p = sync_playwright().start()
    browser = None
    handed_off = False
    try:
        browser = p.chromium.launch(headless=False, channel="msedge")
        ctx = browser.new_context(**_context_args(site))
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            # 打不开也把窗口留给用户：至少能看到浏览器、手动输地址
            print(f"[login] 打开登录页失败（可手动输地址）：{type(e).__name__}: {str(e)[:120]}")

        # 先把登录入口点开、账号密码填好，用户只需要过验证码/扫码。
        # 顺序与 silent_login 对齐：先点入口 → 再选学校 → 最后填表。
        # （人工窗口里原先把选校放在点入口之前，需要"先点入口再选学校"的站点会点空。）
        if prefill and user and pwd:
            try:
                page.wait_for_timeout(2500)
                for key, wait_key, default_wait in (("pre_click", "after_pre_click_wait", 4000),
                                                    ("pre_click2", "after_pre_click2_wait", 5000)):
                    if cfg.get(key):
                        if _click_first(page, cfg[key], timeout=8000):
                            page.wait_for_timeout(cfg.get(wait_key, default_wait))
                school = site.get("school")
                if school and cfg.get("school_open"):
                    _select_school(page, cfg, school, lambda m: print(f"[login] {m}"))
                # 只在"第一次没填上并且走了兜底页"之后才重填一次；
                # 原实现无条件又填了一遍用户名，是纯重复动作。
                filled_user = _fill_first(page, cfg.get("username") or [], user)
                if not filled_user and cfg.get("fallback_url"):
                    page.goto(cfg["fallback_url"], wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(cfg.get("after_fallback_wait", 4000))
                    _fill_first(page, cfg.get("username") or [], user)
                _fill_first(page, cfg.get("password") or [], pwd)
                for sel in list(cfg.get("remember") or []) + list(cfg.get("agree") or []):
                    try:
                        loc = page.locator(sel).first
                        if loc.count() and not loc.is_checked():
                            loc.check(timeout=4000)
                    except Exception:
                        pass
                print(f"[login] {site.get('name', site.get('id'))}: 账号密码已自动填入，"
                      f"请完成验证码/二次认证后点「登录」")
            except Exception as e:
                print(f"[login] 自动预填失败（请手动登录）：{type(e).__name__}: {str(e)[:120]}")

        deadline = time.time() + timeout_s
        ok = False
        nav_checked = set()
        while time.time() < deadline:
            if _safe_validate(validate, page, site):
                ok = True
                break
            # 用户可能只登录到了首页，而目标页在子路径（清华 OJ），复核一次
            try:
                cur = page.url.split("#")[0]
            except Exception:
                cur = ""
            if cur and cur not in nav_checked:
                nav_checked.add(cur)
                if _safe_validate(validate, page, site, navigate=True, cfg=cfg):
                    ok = True
                    break
            try:
                page.wait_for_timeout(2000)
            except Exception:
                break

        if ok:
            # 存盘失败必须让"成功"作废：磁盘满/目录只读时以前仍然返回成功，
            # 用户看到横幅消失，下次刷新立刻又"需要登录"，
            # 形成"登录成功但抓不到作业"的循环。
            saved = False
            try:
                ctx.storage_state(path=sess)
                saved = os.path.exists(sess) and os.path.getsize(sess) > 0
            except Exception as e:
                print(f"[login] 会话存盘失败：{type(e).__name__}: {e}")
            if not saved:
                print(f"[login] {site.get('name', site.get('id'))}: 会话文件不可用（{sess}），"
                      f"本次登录不算成功")
                ok = False

        if keep_alive:
            # 调用方负责关闭；把 playwright 句柄挂在 context 上避免被回收
            try:
                ctx._dsh_playwright = p
            except Exception:
                pass
            handed_off = True
            return ok, browser, ctx, page
        return ok
    except Exception as e:
        print(f"[login] 人工登录未预期异常：{type(e).__name__}: {str(e)[:160]}")
        return (False, None, None, None) if keep_alive else False
    finally:
        # 异常路径也必须释放浏览器与 driver：以前只有正常尾部才 close/stop，
        # new_context / new_page 抛异常时会一直留着浏览器和 msedge 进程。
        if not handed_off:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            try:
                p.stop()
            except Exception:
                pass
