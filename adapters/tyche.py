import os
from .base import Homework, LoginRequiredError, FetchFailedError, session_path


def _creds(site):
    """从站点配置里取出 Basic Auth 的账号密码。

    注意要走 auth 的解密逻辑：sites.yaml 里存的是密文，
    直接读字段会把 "enc:v1:..." 当账号发出去，服务器回 401。
    """
    from .auth import http_credentials
    # 没有 Basic Auth 就返回 None：不要伪造一对空凭据传给浏览器。
    # 空 Authorization 头会抑制 Basic Auth 弹窗（用户看不到输入框），
    # 而 auth._context_args 在同一件事上早就有"空就不传这个键"的守卫。
    return http_credentials(site)


def _fetch_group(page, gid):
    """在浏览器里调接口拿某个课程组的全部作业（JSON）"""
    js = """async (gid) => {
        const r = await fetch('/tyche/group/ShowGroup?gid=' + gid, {credentials: 'include'});
        if (!r.ok) return {status: 'http_error', code: r.status};
        try { return await r.json(); } catch(e) { return {status: 'parse_error'}; }
    }"""
    return page.evaluate(js, str(gid))


def _is_success(data):
    """接口返回是否成功"""
    return isinstance(data, dict) and data.get("status") == "success" and data.get("group")


def _extract(data, site):
    """把接口返回的 JSON 转成 Homework 列表"""
    base = site["base_url"].rstrip("/")
    gid = site["gid"]
    result = []
    group = data.get("group") or {}
    tasks = group.get("tasks") or []

    for t in tasks:
        tid = t.get("tid")
        if tid is None:
            continue
        # "2026-09-27T16:00:00" → "2026-09-27 16:00:00"
        start = (t.get("startTime") or "").replace("T", " ")
        end = (t.get("endTime") or "").replace("T", " ")
        result.append(Homework(
            site=site["id"],
            site_name=site["name"],
            uid=f"{site['id']}:{tid}",
            title=t.get("title") or f"作业 {tid}",
            course=group.get("name") or "",
            start=start,
            end=end,
            url=f"{base}/#!html/Group.html&gid={gid}",
        ))
    return result


def is_logged_in(page, site):
    """判断当前浏览器上下文是否已登录 Tyche。
    登录成功时 base_url 会自动跳到 Group.html，且 ShowGroup 接口返回 success。"""
    try:
        if "showgroup" in page.url.lower() or "group.html" in page.url.lower():
            return _is_success(_fetch_group(page, site["gid"]))
        return _is_success(_fetch_group(page, site["gid"]))
    except Exception:
        return False


# 静默登录配置：Tyche 自带用户名/密码表单，可直接后台登录
LOGIN = {
    "username": ["#username", "input[name=username]"],
    "password": ["#password", "input[name=password]"],
    "submit": ["button.submit", "button:has-text('登录')", "input[type=submit]"],
    "after_goto_wait": 2500,
}


def fetch_homeworks(site, base_dir):
    """静默抓取"""
    session = session_path(base_dir, site)
    if not os.path.exists(session):
        raise LoginRequiredError(site["id"], site["name"])

    from .auth import (browser_session, wait_for_ready, login_url, timeout_detail,
                       page_on_login_page, site_urls_of,
                       SESSION_PROBE_MS, DATA_READY_MS)

    with browser_session() as browser:
        # 关键：http_credentials 自动填 Basic Auth 弹窗。
        # 但**没填/填不全时不能传空凭据**：那会让浏览器对挑战回一个空
        # Authorization 头，直接把 Basic Auth 弹窗顶掉。与 auth._context_args
        # 的守卫保持同一语义。
        ctx_args = {"storage_state": session}
        hc = _creds(site)
        if hc:
            ctx_args["http_credentials"] = hc
        ctx = browser.new_context(**ctx_args)
        try:
            page = ctx.new_page()
            page.goto(site["base_url"], wait_until="domcontentloaded")

            # 轮询 ShowGroup 接口：一旦能拿到数据就立刻继续，
            # 比等 networkidle 快，也顺便完成了登录判定。
            # 短预检（1 秒）：能返回就立刻继续；被跳到登录页就当场结束等待，
            # 不再白睡 15 秒。
            def _ok(pg):
                return _is_success(_fetch_group(pg, site["gid"]))

            logins = [login_url(site)]
            got = wait_for_ready(
                page, _ok,
                timeout_ms=SESSION_PROBE_MS, what="Tyche 作业接口（会话预检）",
                login_urls=logins, site_urls=site_urls_of(site))
            if not got:
                # 预检没成功、也没被跳到登录页 → 可能只是接口慢，再给一次数据
                # 时限。无论走哪条，下面的正式取值与状态码分流都不变。
                got = wait_for_ready(
                    page, _ok,
                    timeout_ms=DATA_READY_MS, what="Tyche 作业接口（数据）",
                    login_urls=logins, site_urls=site_urls_of(site))

            # ★ 两段等待之后一定要显式再判一次登录页：接口 401/403 是硬信号，
            #   但"接口一直没成功"时唯一能确定失效的依据就是 URL —— 少了这一步，
            #   会话失效会被误报成"抓取超时"，用户就拿不到重新登录的入口了。
            if page_on_login_page(page, logins, site_urls=site_urls_of(site)):
                raise LoginRequiredError(site["id"], site["name"])

            data = _fetch_group(page, site["gid"])

            if not _is_success(data):
                status = data.get("status") if isinstance(data, dict) else None
                code = data.get("code") if isinstance(data, dict) else None
                # 接口层错误要和"未登录"分开，否则一次 500 或网络抖动就会被
                # 当成会话失效，触发一次毫无意义的静默重登，还给出错误横幅。
                if status == "http_error":
                    if code in (401, 403):
                        raise LoginRequiredError(site["id"], site["name"])
                    raise FetchFailedError(site["id"], site["name"], f"作业接口 HTTP {code}")
                if status == "fetch_error":
                    raise FetchFailedError(
                        site["id"], site["name"],
                        f"作业接口请求失败：{data.get('message') or '未知网络错误'}")
                # parse_error / 非成功数据：**不直接当未登录**。
                # 两段轮询都超时、又没有 401/403 这种明确失效信号时，
                # 按抓取超时处理 —— 否则"接口只是慢"也会被拉去做一次白跑的静默重登。
                if not got:
                    raise FetchFailedError(site["id"], site["name"],
                                           timeout_detail("Tyche 作业接口"))
                raise LoginRequiredError(site["id"], site["name"])

            return _extract(data, site)
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def login(site, base_dir, manual=True):
    """登录。

    manual=False 时走无头静默登录（有账号密码就直接后台登录）；
    manual=True 时弹出可见窗口，等待用户手动完成。
    """
    from .auth import silent_login, interactive_login
    if manual:
        return interactive_login(site, base_dir)
    ok, _reason = silent_login(site, base_dir)
    return ok
