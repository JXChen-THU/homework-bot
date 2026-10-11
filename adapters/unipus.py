import os
from datetime import datetime
from playwright.sync_api import sync_playwright
from .base import Homework, LoginRequiredError
from .auth import login_with_credentials


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


def fetch_homeworks(site, base_dir):
    """静默抓取：加载会话 → 打开页面 → 调接口"""
    session = os.path.join(base_dir, site["session"])
    if not os.path.exists(session):
        raise LoginRequiredError(site["id"], site["name"])

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="msedge")
        ctx = browser.new_context(storage_state=session)
        page = ctx.new_page()
        page.goto(site["base_url"])
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(3000)

        # 检查是否被踢回登录页
        if "login" in page.url.lower() or "sso" in page.url.lower():
            browser.close()
            raise LoginRequiredError(site["id"], site["name"])

        data = _fetch_exams(page)
        browser.close()

        if not isinstance(data, dict) or not data.get("success"):
            raise LoginRequiredError(site["id"], site["name"])

        return _extract(data, site)


def _login_success(page, _site):
    data = _fetch_exams(page)
    return isinstance(data, dict) and bool(data.get("success"))


def login(site, base_dir, manual_required=None):
    """自动填写 Unipus 账号密码，遇到二次验证时保留浏览器供用户处理。"""
    return login_with_credentials(
        site, base_dir, _login_success,
        manual_required or (lambda _name, _auto: None),
    )
