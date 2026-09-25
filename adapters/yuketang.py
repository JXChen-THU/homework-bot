import os
from datetime import datetime
from playwright.sync_api import sync_playwright
from .base import Homework, LoginRequiredError, session_path


def _ts_to_str(ts):
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _fetch_courses(page):
    js = """async () => {
        try {
            const r = await fetch('/v2/api/web/courses/list?identity=2', {credentials: 'include'});
            if (!r.ok) return {status: 'http_error', code: r.status};
            return await r.json();
        } catch(e) { return {status: 'fetch_error', message: e.toString()}; }
    }"""
    return page.evaluate(js)


def _fetch_activities(page, classroom_id):
    js = """async (cid) => {
        const url = '/v2/api/web/logs/learn/' + cid
                    + '?actype=-1&page=0&offset=500&sort=-1';
        try {
            const r = await fetch(url, {credentials: 'include'});
            if (!r.ok) return {status: 'http_error', code: r.status};
            return await r.json();
        } catch(e) { return {status: 'fetch_error', message: e.toString()}; }
    }"""
    return page.evaluate(js, str(classroom_id))


def _extract_courses(data):
    result = []
    if not isinstance(data, dict):
        return result
    for c in (data.get("data") or {}).get("list") or []:
        cid = c.get("classroom_id")
        name = (c.get("course") or {}).get("name") or c.get("name") or ""
        if cid:
            result.append({"id": cid, "name": name})
    return result


def _extract_tasks(data, site, course_name=""):
    result = []
    if not isinstance(data, dict):
        return result
    acts = (data.get("data") or {}).get("activities") or []
    for a in acts:
        t = a.get("type")
        title = a.get("title") or ""
        end = ""
        if t == 19:
            end = _ts_to_str((a.get("content") or {}).get("score_d"))
        elif t == 5:
            end = _ts_to_str(a.get("deadline"))
        else:
            continue
        if not end or not title:
            continue
        result.append(Homework(
            site=site["id"],
            site_name=site["name"],
            uid=f"{site['id']}:{a.get('id')}",
            title=title,
            course=course_name,
            start=_ts_to_str(a.get("create_time")),
            end=end,
            url=f"https://pro.yuketang.cn/v2/web/studentLog/{a.get('classroom_id', '')}/{a.get('id')}",
        ))
    return result


def fetch_homeworks(site, base_dir):
    session = session_path(site, base_dir)
    if not os.path.exists(session):
        raise LoginRequiredError(site["id"], site["name"])

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="msedge")
        ctx = browser.new_context(storage_state=session)
        page = ctx.new_page()
        page.goto(site["base_url"])
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3000)

        if "login" in page.url.lower():
            browser.close()
            raise LoginRequiredError(site["id"], site["name"])

        courses = _extract_courses(_fetch_courses(page))
        all_homeworks = []
        for c in courses:
            data = _fetch_activities(page, c["id"])
            all_homeworks.extend(_extract_tasks(data, site, c.get("name", "")))

        browser.close()
        return all_homeworks


def login(site, base_dir):
    session = session_path(site, base_dir)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="msedge")
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(site["base_url"])

        logged_in = False
        for _ in range(200):
            page.wait_for_timeout(3000)
            try:
                if "login" not in page.url.lower():
                    if page.query_selector("[class*='course'], [class*='Course']"):
                        logged_in = True
                        break
            except Exception:
                pass

        if logged_in:
            ctx.storage_state(path=session)
        browser.close()
        return logged_in