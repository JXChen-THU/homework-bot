"""静默登录公共设施。

设计目标：有账号密码时，会话过期后程序在后台静默重登，用户无感知。

每个适配器只需要提供三样东西（在各自模块里定义）：
    LOGIN          登录配置（登录页 URL、选择器、提交方式等）
    is_logged_in(page, site)   判断当前浏览器上下文是否已登录
    可选的 helper  例如 _get_courses(page)

本模块据此提供：
    silent_login(site, base_dir)   无头静默登录；失败返回 False，不抛异常
    interactive_login(site, base_dir)  可见窗口登录，供人工扫码／过验证码

两者都会在成功后写入 data/<session>.json。
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
        return value or ""
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
    name = site.get("session")
    if not name:
        name = f"{site['id']}_session.json"
    return os.path.join(base_dir, name)


def login_url(site):
    return (site.get("login_url")
            or _DEFAULT_LOGIN_URLS.get(site.get("type"))
            or site.get("url")
            or site.get("base_url")
            or "")


def wait_for_ready(page, condition, *, timeout_ms=15000, interval_ms=200, what=""):
    """轮询等待某个条件成立就立刻继续（替代固定 sleep 和 networkidle）。

    为什么不用 networkidle：它要求网络彻底安静 500ms，而很多学习平台都有
    轮询/埋点请求，会一直等到超时。实测网络学堂在 domcontentloaded 后 1.3 秒
    就能拿到登录态和 CSRF，但 networkidle 还要再等 4 秒。

    condition(page) 返回真值即视为就绪。返回是否在超时前就绪。
    """
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        try:
            if condition(page):
                return True
        except Exception:
            pass
        try:
            page.wait_for_timeout(interval_ms)
        except Exception:
            return False
    if what:
        print(f"[wait] {what} 等待超时（{timeout_ms}ms），继续执行")
    return False


def _fill_first(page, selectors, value, timeout=8000):
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

    deadline = time.time() + timeout_ms / 1000.0

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
            page.wait_for_timeout(cfg.get("after_goto_wait", 2500))

            # 3) 需要先点开的入口（例如网络学堂的「登录」按钮）
            pre_click = cfg.get("pre_click")
            if pre_click:
                hit = _click_first(page, pre_click, timeout=8000)
                if hit:
                    _log(f"点击登录入口：{hit}")
                    page.wait_for_timeout(cfg.get("after_pre_click_wait", 4000))

            # 3b) 第二段点击（例如清华 OJ：先展开下拉，再点「使用 清华ID 登录」）
            pre_click2 = cfg.get("pre_click2")
            if pre_click2:
                hit2 = _click_first(page, pre_click2, timeout=8000)
                if hit2:
                    _log(f"点击登录入口2：{hit2}")
                    page.wait_for_timeout(cfg.get("after_pre_click2_wait", 5000))

            # 4) 选学校之类的选择器（数你最灵）
            school = site.get("school")
            if school and cfg.get("school_open"):
                _select_school(page, cfg, school, _log)

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
                        page.wait_for_timeout(cfg.get("after_fallback_wait", 4000))
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

            # 8) 等待登录结果
            ok, reason = _wait_for_login(page, validate, site, deadline, cfg)
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
    """
    error_markers = cfg.get("error_markers") or [
        "无法登录", "密码错误", "账号或密码", "账户密码", "用户名或密码错误",
        "密码不正确", "账号不存在", "请确认提供的账户", "登录失败",
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

        # 1) 明确的登录失败提示 → 立刻返回，不要干等
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

        # 2) 真正的二次验证界面
        if _looks_like_mfa(page, cfg):
            return False, "mfa"

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
    browser = p.chromium.launch(headless=False, channel="msedge")
    ctx = browser.new_context(**_context_args(site))
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        pass

    # 先把登录入口点开、账号密码填好，用户只需要过验证码/扫码
    if prefill and user and pwd:
        try:
            page.wait_for_timeout(2500)
            school = site.get("school")
            if school and cfg.get("school_open"):
                _select_school(page, cfg, school, lambda m: print(f"[login] {m}"))
            for key, wait_key, default_wait in (("pre_click", "after_pre_click_wait", 4000),
                                                ("pre_click2", "after_pre_click2_wait", 5000)):
                if cfg.get(key):
                    if _click_first(page, cfg[key], timeout=8000):
                        page.wait_for_timeout(cfg.get(wait_key, default_wait))
            if not _fill_first(page, cfg.get("username") or [], user) and cfg.get("fallback_url"):
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
        try:
            ctx.storage_state(path=sess)
        except Exception:
            pass

    if keep_alive:
        # 调用方负责关闭；把 playwright 句柄挂在 context 上避免被回收
        try:
            ctx._dsh_playwright = p
        except Exception:
            pass
        return ok, browser, ctx, page

    try:
        browser.close()
    except Exception:
        pass
    try:
        p.stop()
    except Exception:
        pass
    return ok
