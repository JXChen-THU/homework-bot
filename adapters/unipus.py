import os
from datetime import datetime
from playwright.sync_api import sync_playwright
from .base import Homework, LoginRequiredError


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


def login(site, base_dir):
    """弹窗让用户手动登录，成功后保存会话。
    通过调考试接口判断是否真正登录成功。"""
    session = os.path.join(base_dir, site["session"])
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="msedge")
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(site["base_url"])

        logged_in = False
        # 最多等 10 分钟
        for _ in range(200):
            page.wait_for_timeout(3000)
            try:
                data = _fetch_exams(page)
                # 接口返回 success=true 才认为登录成功
                if isinstance(data, dict) and data.get("success"):
                    page.wait_for_timeout(3000)
                    logged_in = True
                    break
            except Exception:
                pass

        if logged_in:
            ctx.storage_state(path=session)
        browser.close()
        return logged_in