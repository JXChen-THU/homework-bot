import os
from datetime import datetime
from .base import Homework, LoginRequiredError, FetchFailedError, session_path


def _ts_to_local(ms):
    """毫秒时间戳 → 本地时间字符串"""
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _fetch_exams(page):
    """调考试列表接口"""
    js = """async () => {
        try {
            const r = await fetch('/api/portal/v1/exam/student-exams', {
                credentials: 'include'
            });
            if (!r.ok) return {success: false, _http: r.status};
            return await r.json();
        } catch(e) {
            return {success: false, _err: e.toString()};
        }
    }"""
    return page.evaluate(js)


def _extract(data, site):
    """把接口返回转成 Homework 列表"""
    result = []
    if not isinstance(data, dict) or not data.get("success"):
        return result

    items = (data.get("data") or {}).get("items") or []
    for ex in items:
        eid = ex.get("examId")
        if not eid:
            continue

        title = ex.get("examName") or "未命名考试"
        # 加上标签前缀
        tags = ex.get("tagNames") or []
        if tags:
            title = "【" + "·".join(tags) + "】" + title

        result.append(Homework(
            site=site["id"],
            site_name=site["name"],
            uid=f"{site['id']}:{eid}",
            title=title,
            course=ex.get("venueName") or "",
            start=_ts_to_local(ex.get("examStartTime")),
            end=_ts_to_local(ex.get("examEndTime")),
            url=ex.get("examUrl") or "https://uai.unipus.cn/portal",
        ))
    return result


def is_logged_in(page, site):
    """已登录判定：考试接口返回 success 才算真正登录。"""
    try:
        data = _fetch_exams(page)
    except Exception:
        return False
    return isinstance(data, dict) and bool(data.get("success"))


# 静默登录配置
# 实测流程：首页点「登 录」→ 跳 /portal/sso/login → 填手机号 + 密码
#           → 勾「记住我」和用户协议 → 提交 → 回门户，考试接口可用。
LOGIN = {
    "pre_click": ["button[class*=loginBtn]", "button:has-text('登 录')",
                  "button:has-text('登录')"],
    "after_pre_click_wait": 6000,
    # 万一点击没有跳转，直接打开登录页
    "fallback_url": "https://uai.unipus.cn/portal/sso/login",
    "username": ["#username", "input[placeholder*='手机号']"],
    "password": ["#password", "input[type=password]"],
    "agree": ["#agreement"],
    "remember": ["#remember"],
    "submit": ["button:has-text('登 录')", "button[type=submit]", "button:has-text('登录')"],
    "after_goto_wait": 4000,
}


def fetch_homeworks(site, base_dir):
    """静默抓取：加载会话 → 打开页面 → 调接口"""
    session = session_path(base_dir, site)
    if not os.path.exists(session):
        raise LoginRequiredError(site["id"], site["name"])

    from .auth import (browser_session, wait_for_ready, login_url, timeout_detail,
                       page_on_login_page, site_urls_of,
                       SESSION_PROBE_MS, DATA_READY_MS, guard_in_page_fetch)

    with browser_session() as browser:
        ctx = browser.new_context(storage_state=session)
        try:
            page = ctx.new_page()
            guard_in_page_fetch(page)   # 页面内 fetch 超时保护，见 auth.guard_in_page_fetch
            page.goto(site["base_url"], wait_until="domcontentloaded")

            def _ok(pg):
                d = _fetch_exams(pg)
                return isinstance(d, dict) and bool(d.get("success"))

            logins = [login_url(site)]
            # 短预检（1 秒）：接口能返回 success 就立刻继续；被踢回登录页时
            # wait_for_ready 会当场结束等待，不再白睡 15 秒。
            got = wait_for_ready(
                page, _ok,
                timeout_ms=SESSION_PROBE_MS, what="U校园考试接口（会话预检）",
                login_urls=logins, site_urls=site_urls_of(site))

            # 预检没成功、也没被跳到登录页 → 可能只是接口慢：再给一次数据时限。
            # 无论走哪条，下面的正式取值 + 401/403 分流都不变，
            # 所以"慢"不会被误判成"需要登录"。
            if not got:
                got = wait_for_ready(
                    page, _ok,
                    timeout_ms=DATA_READY_MS, what="U校园考试接口（数据）",
                    login_urls=logins, site_urls=site_urls_of(site))

            # 检查是否被踢回登录页（会话失效时这是最确定的信号）
            if page_on_login_page(page, logins, site_urls=site_urls_of(site)):
                raise LoginRequiredError(site["id"], site["name"])

            data = _fetch_exams(page)

            if isinstance(data, dict) and data.get("success"):
                return _extract(data, site)

            # 接口失败 ≠ 未登录。顺序就是优先级：
            #   1) 401/403        → 确定的会话失效，该重登就重登
            #   2) 5xx / 网络异常  → 抓取失败（不触发重登）
            #   3) 两段轮询都超时、又没有失效信号 → **不要**猜成"需要登录"：
            #      那会触发一次白跑的静默重登。报抓取超时，让用户刷新重试。
            code = data.get("_http") if isinstance(data, dict) else None
            if code in (401, 403):
                raise LoginRequiredError(site["id"], site["name"])
            if code or (isinstance(data, dict) and data.get("_err")):
                raise FetchFailedError(
                    site["id"], site["name"],
                    f"考试接口失败（{('HTTP %s' % code) if code else data.get('_err')}）")
            if not got:
                raise FetchFailedError(site["id"], site["name"],
                                       timeout_detail("U校园考试接口"), timeout=True)
            raise LoginRequiredError(site["id"], site["name"])
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def login(site, base_dir, manual=True):
    """登录。manual=False 时无头静默登录（手机号 + 密码）。"""
    from .auth import silent_login, interactive_login
    if manual:
        return interactive_login(site, base_dir)
    ok, _reason = silent_login(site, base_dir)
    return ok
