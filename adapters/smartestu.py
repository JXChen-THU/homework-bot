import os
from datetime import datetime
from .base import Homework, LoginRequiredError, FetchFailedError, session_path


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


def is_logged_in(page, site):
    """已登录判定：/api/auth/session 能返回用户信息。"""
    try:
        data = _fetch_session(page)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    # 未登录时接口会返回 401/403 或空对象
    if data.get("_http") or data.get("_err"):
        return False
    return bool(data.get("user"))


# 静默登录配置
# 数你最灵要求先选「学校」，再填学号/工号 + 密码。
# 学校名放在 sites.yaml 的 school 字段里，不写死在代码中。
LOGIN = {
    "school_open": ["button:has-text('请选择学校')", "button:has-text('学校')"],
    "school_search": ["input[placeholder*='搜索学校']"],
    "school_pick": [],          # 留空：用 get_by_text 精确点击学校名
    "username": ["#authField", "input[name=schoolUserLocalId]"],
    "password": ["#password", "input[name=password]"],
    "submit": ["button:has-text('继续')", "button[type=submit]"],
    "after_goto_wait": 3500,
}


def fetch_homeworks(site, base_dir):
    """静默抓取：
    1. 加载会话，打开页面
    2. 调 /api/auth/session 拿课程列表
    3. 调 queryHomeworks 拿作业
    """
    session = session_path(base_dir, site)
    if not os.path.exists(session):
        raise LoginRequiredError(site["id"], site["name"])

    from .auth import browser_session, wait_for_ready

    cached = {}

    def _ready(pg):
        """页面能干活了：会话有效，**并且作业接口已经不再返回 409**。

        实测页面刚加载完就调 queryHomeworks 会返回 409，
        必须等它稳定下来。以作业接口为准比固定 sleep 更可靠。
        """
        if not is_logged_in(pg, site):
            return False
        data = _fetch_session(pg)
        if not isinstance(data, dict) or data.get("_http") or data.get("_err"):
            return False
        courses = _extract_courses(data)
        cached["session"] = data
        cached["courses"] = courses
        if not courses:
            # 已登录但一门课都没有：这是"没有课程"，不是"需要登录"。
            # 以前这里返回 False，于是白等满 20 秒、最终仍被当成未登录，
            # 用户被叫去"确认一次验证码"，而其实只是没选课/选课未同步。
            cached["homeworks"] = {"success": True, "data": {"courseHomeworkDTOList": []}}
            return True
        ids = [c["id"] for c in courses]
        hw = _fetch_homeworks(pg, ids)
        if isinstance(hw, dict) and hw.get("success"):
            cached["homeworks"] = hw
            return True
        return False

    with browser_session() as browser:
        ctx = browser.new_context(storage_state=session)
        try:
            page = ctx.new_page()
            page.goto(site["base_url"], wait_until="domcontentloaded")

            ready = wait_for_ready(page, _ready, timeout_ms=20000,
                                   interval_ms=500, what="数你最灵作业接口")

            # 1. 拿课程列表
            sess_data = cached.get("session") or _fetch_session(page)
            courses = cached.get("courses") if "courses" in cached else _extract_courses(sess_data)
            if not courses:
                if is_logged_in(page, site):
                    # 有效会话 + 没有课程 = 完成 0 条，不是"需要登录"
                    return []
                raise LoginRequiredError(site["id"], site["name"])

            course_ids = [c["id"] for c in courses]
            course_map = {c["id"]: c["name"] for c in courses}

            # 2. 拿作业
            data = cached.get("homeworks")
            if data is None:
                data = _fetch_homeworks(page, course_ids)
            if not ready and not (isinstance(data, dict) and data.get("success")):
                # 就绪轮询和这里都失败：再补一次机会
                page.wait_for_timeout(2000)
                data = _fetch_homeworks(page, course_ids)

            if not (isinstance(data, dict) and data.get("success")):
                # 接口失败要和"未登录"分开：会话有效时这是抓取失败（网络/接口），
                # 报成"需要登录"会触发一次毫无意义的静默重登。
                detail = ""
                if isinstance(data, dict):
                    detail = f"HTTP {data['_http']}" if data.get("_http") else (data.get("_err") or "success=false")
                if is_logged_in(page, site):
                    raise FetchFailedError(site["id"], site["name"], f"作业接口失败（{detail}）")
                raise LoginRequiredError(site["id"], site["name"])

            return _extract_homeworks(data, site, course_map)
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def login(site, base_dir, manual=True):
    """登录。

    manual=False 时无头静默登录（需要 sites.yaml 里配好 school 和账号密码）；
    manual=True 时弹出可见窗口让用户手动登录。
    """
    from .auth import silent_login, interactive_login
    if manual:
        return interactive_login(site, base_dir)
    ok, _reason = silent_login(site, base_dir)
    return ok
