import os
from playwright.sync_api import sync_playwright
from .base import Homework, LoginRequiredError


def _creds(site):
    """从站点配置里取出 Basic Auth 的账号密码"""
    return {
        "username": site.get("basic_user", ""),
        "password": site.get("basic_pass", ""),
    }


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


def fetch_homeworks(site, base_dir):
    """静默抓取"""
    session = os.path.join(base_dir, site["session"])
    if not os.path.exists(session):
        raise LoginRequiredError(site["id"], site["name"])

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="msedge")
        # 关键：http_credentials 自动填 Basic Auth 弹窗
        ctx = browser.new_context(
            storage_state=session,
            http_credentials=_creds(site),
        )
        page = ctx.new_page()
        page.goto(site["base_url"])
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)

        data = _fetch_group(page, site["gid"])
        browser.close()

        if not _is_success(data):
            raise LoginRequiredError(site["id"], site["name"])

        return _extract(data, site)


def login(site, base_dir):
    """弹窗让用户登录"""
    session = os.path.join(base_dir, site["session"])
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="msedge")
        ctx = browser.new_context(http_credentials=_creds(site))
        page = ctx.new_page()
        page.goto(site["base_url"])

        logged_in = False
        for _ in range(100):
            page.wait_for_timeout(3000)
            try:
                data = _fetch_group(page, site["gid"])
                if _is_success(data):
                    logged_in = True
                    break
            except Exception:
                pass

        if logged_in:
            ctx.storage_state(path=session)
        browser.close()
        return logged_in