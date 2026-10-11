import os
from datetime import datetime
from playwright.sync_api import sync_playwright
from .base import Homework, LoginRequiredError
from .auth import login_with_credentials


def _iso_to_local(s):
    """ISO 8601 UTC 时间 → 本地时间字符串
    例：'2026-09-28T15:00:00.000Z' → '2026-09-28 23:00:00'"""
    if not s:
        return ""
    try:
        if s.endswith("Z"):
            s2 = s[:-1] + "+00:00"
        else:
            s2 = s
        dt = datetime.fromisoformat(s2)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return s.replace("T", " ").replace("Z", "").strip()


def _fetch_session(page):
    """调 /api/auth/session 拿用户信息和课程列表"""
    js = """async () => {
        try {
            const r = await fetch('/api/auth/session', {credentials: 'include'});
            if (!r.ok) return {_http: r.status};
            return await r.json();
        } catch(e) {
            return {_err: e.toString()};
        }
    }"""
    return page.evaluate(js)


def _fetch_homeworks(page, course_ids):
    """调作业接口，带上 courseIds"""
    js = """async (courseIds) => {
        try {
            const r = await fetch('/api/homework/student/mark/queryHomeworks', {
                method: 'POST',
                credentials: 'include',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    courseIds: courseIds,
                    scene: 'homework',
                    pageSize: 100,
                    pageNo: 1
                })
            });
            if (!r.ok) return {success: false, _http: r.status};
            return await r.json();
        } catch(e) {
            return {success: false, _err: e.toString()};
        }
    }"""
    return page.evaluate(js, course_ids)


def _extract_courses(session_data):
    """从 session 响应里提取 [{id, name}]"""
    if not isinstance(session_data, dict):
        return []
    user = session_data.get("user") or {}
    cap = user.get("capabilityProfile") or {}
    result = []
    for c in cap.get("studentCourses") or []:
        cid = c.get("courseId")
        name = c.get("courseName") or ""
        if cid is not None:
            result.append({"id": cid, "name": name})
    return result


def _extract_homeworks(data, site, course_map):
    """把 queryHomeworks 的响应转成 Homework 列表"""
    result = []
    if not isinstance(data, dict) or not data.get("success"):
        return result

    courses = (data.get("data") or {}).get("courseHomeworkDTOList") or []
    for course in courses:
        cid = course.get("courseId")
        cname = course.get("courseName") or course_map.get(cid, "")
        for hw in course.get("studentCourseHomeworkDTOList") or []:
            hid = hw.get("id")
            if not hid:
                continue
            result.append(Homework(
                site=site["id"],
                site_name=site["name"],
                uid=f"{site['id']}:{hid}",
                title=hw.get("name") or "未命名作业",
                course=cname,
                start=_iso_to_local(hw.get("startTime")),
                end=_iso_to_local(hw.get("endTime")),
                url="https://smartestu.cn/",
            ))
    return result


def fetch_homeworks(site, base_dir):
    """静默抓取：
    1. 加载会话，打开页面
    2. 调 /api/auth/session 拿课程列表
    3. 调 queryHomeworks 拿作业
    """
    session = os.path.join(base_dir, site["session"])
    if not os.path.exists(session):
        raise LoginRequiredError(site["id"], site["name"])

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="msedge")
        ctx = browser.new_context(storage_state=session)
        page = ctx.new_page()
        page.goto(site["base_url"])
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(2000)

        # 1. 拿课程列表
        sess_data = _fetch_session(page)
        courses = _extract_courses(sess_data)
        if not courses:
            browser.close()
            raise LoginRequiredError(site["id"], site["name"])

        course_ids = [c["id"] for c in courses]
        course_map = {c["id"]: c["name"] for c in courses}

        # 2. 拿作业
        data = _fetch_homeworks(page, course_ids)
        browser.close()

        return _extract_homeworks(data, site, course_map)


def _login_success(page, _site):
    data = _fetch_session(page)
    return isinstance(data, dict) and bool(data.get("user"))


def login(site, base_dir, manual_required=None):
    """自动填写账号密码；验证码或其他验证交由用户完成。"""
    return login_with_credentials(
        site, base_dir, _login_success,
        manual_required or (lambda _name, _auto: None),
    )
