import os
from datetime import datetime
from playwright.sync_api import sync_playwright
from .base import Homework, LoginRequiredError


def _ts_to_str(ts):
    """毫秒时间戳 → "2026-09-29 23:59:00" """
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _fetch_courses(page):
    """调接口拿当前用户的所有课程"""
    js = """async () => {
        try {
            const r = await fetch('/v2/api/web/courses/list?identity=2', {credentials: 'include'});
            if (!r.ok) return {status: 'http_error', code: r.status};
            return await r.json();
        } catch(e) { return {status: 'fetch_error', message: e.toString()}; }
    }"""
    return page.evaluate(js)


def _fetch_activities(page, classroom_id):
    """拿一门课的全部活动（含作业、习题、试卷）"""
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
    """从课程列表接口返回里提取 (id, name)"""
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
    """从活动列表里提取作业 / 习题 / 试卷
    通过 type 区分：
        19 = 作业（截止时间在 content.score_d）
        5  = 考试（截止时间在 deadline）
        4  = 试卷（截止时间在 deadline）
        14 = 教学，跳过
    """
    result = []
    if not isinstance(data, dict):
        return result

    acts = (data.get("data") or {}).get("activities") or []
    for a in acts:
        t = a.get("type")
        title = a.get("title") or ""
        end = ""

        if t == 19:
            # 作业
            end = _ts_to_str((a.get("content") or {}).get("score_d"))
            title = "【作业】" + title
        elif t == 5:
            # 考试
            end = _ts_to_str(a.get("deadline"))
            title = "【考试】" + title
        elif t == 4:
            # 试卷
            end = _ts_to_str(a.get("deadline"))
            title = "【试卷】" + title
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
    """静默抓取：先拿课程列表，再逐门课拿活动"""
    session = os.path.join(base_dir, site["session"])
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

        # 1) 拿课程列表
        courses = _extract_courses(_fetch_courses(page))

        # 2) 逐门课拿活动
        all_homeworks = []
        for c in courses:
            data = _fetch_activities(page, c["id"])
            all_homeworks.extend(_extract_tasks(data, site, c.get("name", "")))

        browser.close()
        return all_homeworks


def login(site, base_dir):
    """弹窗让用户扫码登录"""
    session = os.path.join(base_dir, site["session"])
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="msedge")
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(site["base_url"])

        logged_in = False
        # 扫码可能要挺久，最多等 10 分钟
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