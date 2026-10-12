import os
from .base import Homework, LoginRequiredError


def _creds(site):
    """从站点配置里取出 Basic Auth 的账号密码。

    注意要走 auth 的解密逻辑：sites.yaml 里存的是密文，
    直接读字段会把 "enc:v1:..." 当账号发出去，服务器回 401。
    """
    from .auth import http_credentials
    return http_credentials(site) or {"username": "", "password": ""}


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
    session = os.path.join(base_dir, site["session"])
    if not os.path.exists(session):
        raise LoginRequiredError(site["id"], site["name"])

    from .auth import browser_session, wait_for_ready

    with browser_session() as browser:
        # 关键：http_credentials 自动填 Basic Auth 弹窗
        ctx = browser.new_context(
            storage_state=session,
            http_credentials=_creds(site),
        )
        page = ctx.new_page()
        page.goto(site["base_url"], wait_until="domcontentloaded")

        # 轮询 ShowGroup 接口：一旦能拿到数据就立刻继续，
        # 比等 networkidle 快，也顺便完成了登录判定。
        data = {}
        wait_for_ready(
            page,
            lambda pg: _is_success(_fetch_group(pg, site["gid"])),
            timeout_ms=15000, what="Tyche 作业接口")
        data = _fetch_group(page, site["gid"])
        ctx.close()

        if not _is_success(data):
            raise LoginRequiredError(site["id"], site["name"])

        return _extract(data, site)


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
