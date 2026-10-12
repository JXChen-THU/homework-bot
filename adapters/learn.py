import os
import json
from .base import Homework, LoginRequiredError


def _get_courses(page):
    """从首页提取所有课程 ID 和名称，扫描 iframe"""
    js = """() => {
        const seen = new Set();
        const courses = [];
        function scan(doc) {
            try {
                doc.querySelectorAll('a[href*="wlkcid"]').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    const m = href.match(/wlkcid=([^&]+)/);
                    if (!m) return;
                    const id = m[1];
                    if (seen.has(id)) return;
                    // 往上找，看有没有"课程名(8位数字-数字)"这段文字
                    let node = a;
                    for (let i = 0; i < 8; i++) {
                        if (!node.parentElement) break;
                        node = node.parentElement;
                        const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                        const mm = text.match(/^(.+?)\\(\\d{8}-\\d+\\)/);
                        if (mm) {
                            seen.add(id);
                            courses.push({id: id, name: mm[1]});
                            break;
                        }
                    }
                });
            } catch(e) {}
            // 递归扫描子 iframe
            try {
                doc.querySelectorAll('iframe').forEach(f => {
                    try { if (f.contentDocument) scan(f.contentDocument); } catch(e) {}
                });
            } catch(e) {}
        }
        scan(document);
        return courses;
    }"""
    try:
        return page.evaluate(js)
    except Exception:
        return []


def _fetch_one_course(page, course_id, csrf):
    """调作业接口，拿某一门课的作业"""
    js = """async (args) => {
        const [courseId, csrf] = args;
        // 网络学堂用的是 DataTables 插件，参数格式固定
        const aoData = [
            {name: "sEcho", value: 1},
            {name: "iColumns", value: 8},
            {name: "sColumns", value: ",,,,,,,"},
            {name: "iDisplayStart", value: 0},
            {name: "iDisplayLength", value: "200"},
            {name: "mDataProp_0", value: "wz"},
            {name: "bSortable_0", value: false},
            {name: "mDataProp_1", value: "bt"},
            {name: "bSortable_1", value: true},
            {name: "mDataProp_2", value: "mxdxmc"},
            {name: "bSortable_2", value: true},
            {name: "mDataProp_3", value: "zywcfs"},
            {name: "bSortable_3", value: true},
            {name: "mDataProp_4", value: "kssj"},
            {name: "bSortable_4", value: true},
            {name: "mDataProp_5", value: "jzsj"},
            {name: "bSortable_5", value: true},
            {name: "mDataProp_6", value: "jzsj"},
            {name: "bSortable_6", value: true},
            {name: "mDataProp_7", value: "function"},
            {name: "bSortable_7", value: false},
            {name: "iSortCol_0", value: 5},
            {name: "sSortDir_0", value: "desc"},
            {name: "iSortCol_1", value: 6},
            {name: "sSortDir_1", value: "desc"},
            {name: "iSortingCols", value: 2},
            {name: "wlkcid", value: courseId}
        ];

        const params = new URLSearchParams();
        params.append('_csrf', csrf);
        params.append('aoData', JSON.stringify(aoData));

        const url = '/b/wlxt/kczy/zy/student/zyListWj?_csrf=' + csrf;

        try {
            const r = await fetch(url, {
                method: 'POST',
                credentials: 'include',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: params.toString()
            });
            const text = await r.text();
            return {httpStatus: r.status, body: text};
        } catch(e) {
            return {httpStatus: 0, body: 'fetch_error: ' + e.toString()};
        }
    }"""
    return page.evaluate(js, [str(course_id), str(csrf)])


def _extract(data, site, course_name=""):
    """把接口返回的 JSON 转成 Homework 列表"""
    result = []
    if not isinstance(data, dict):
        return result
    obj = data.get("object") or {}
    items = obj.get("aaData") or []
    for t in items:
        zyid = t.get("zyid")
        if not zyid:
            continue
        result.append(Homework(
            site=site["id"],
            site_name=site["name"],
            uid=f"{site['id']}:{zyid}",
            title=t.get("bt") or "未命名作业",
            course=course_name,
            start=t.get("kssjStr") or "",
            end=t.get("jzsjStr") or "",
            url="https://learn.tsinghua.edu.cn/b/wlxt/kczy/zy/student/index",
        ))
    return result


def _on_logged_in_page(page):
    """判断当前页面是不是网络学堂已登录后的页面。

    判定顺序很关键：必须**先**确认没有停在统一身份认证页面，
    否则 SSO 页面里出现的「课程」字样会造成误判。
    """
    try:
        url = page.url.lower()
    except Exception:
        return False
    if "id.tsinghua.edu.cn" in url:
        return False
    if any(k in url for k in ("/f/login", "login", "sso", "auth")):
        return False
    if "learn.tsinghua.edu.cn" not in url:
        return False
    try:
        content = page.content()
    except Exception:
        return False
    return ("wlxt" in content) or ("课程" in content)


def is_logged_in(page, site):
    """给静默登录用的校验函数：已登录网络学堂才算 True。"""
    return _on_logged_in_page(page)


# 静默登录配置
# 网络学堂自己没有账号密码表单，点「登录」后跳到清华统一身份认证（id.tsinghua.edu.cn），
# 那里才是 #i_user / #i_pass。注意：清华 SSO 对陌生浏览器会要求二次验证（企业微信/短信），
# 因此 mfa_markers 只匹配二次验证页面上的真实字样，避免误判。
LOGIN = {
    "pre_click": ["#loginButtonId", "input[value=登录]"],
    "after_pre_click_wait": 4000,
    "username": ["#i_user", "input[name=i_user]", "input[name=username]"],
    "password": ["#i_pass", "input[name=i_pass]", "input[type=password]"],
    "submit": ["a:has-text('登录')", "#login", "button[type=submit]",
               "input[type=submit]", "button:has-text('登录')"],
    "after_goto_wait": 2500,
    # 清华 SSO 对无头浏览器**必定**要求二次验证（实测稳定复现），
    # 而真实窗口会被直接放行。所以后台重登时跳过无头那一档，
    # 直接开屏幕外真实窗口：少跑一次注定失败的登录，也少一次多余的 SSO 交互。
    "skip_headless": True,
    # 清华 SSO 的二次验证页面（人工兜底时会停在这里）
    "mfa_selectors": [".second-verify", "#secondVerify", ".verify-way", ".auth-way"],
    "mfa_phrases": ["请选择以下方式之一获取验证码", "验证码到您的微信",
                    "二次验证", "二次认证", "本次登录需要进行二次验证"],
}


def _csrf_from_ctx(ctx):
    """从 cookie 里拿 CSRF token（网络学堂后续接口都要带）。"""
    for c in ctx.cookies():
        n = c["name"].lower()
        if "csrf" in n or "xsrf" in n:
            return c["value"]
    return ""


def _ready(page):
    """页面能干活了：已登录，并且 CSRF cookie 已经下来。

    实测这两样在 domcontentloaded 之后 1.3 秒左右就绪，
    比等 networkidle（还要再等 4 秒）快得多。
    """
    if not _on_logged_in_page(page):
        return False
    return bool(_csrf_from_ctx(page.context))


def fetch_homeworks(site, base_dir):
    """静默抓取所有课程的作业"""
    session = os.path.join(base_dir, site["session"])
    if not os.path.exists(session):
        raise LoginRequiredError(site["id"], site["name"])

    # 课程列表缓存（避免每次扫描）
    cache_file = os.path.join(base_dir, f"{site['id']}_courses.json")

    from .auth import browser_session, wait_for_ready

    with browser_session() as browser:
        ctx = browser.new_context(storage_state=session)
        page = ctx.new_page()
        page.goto(site["base_url"], wait_until="domcontentloaded")
        # 等到登录态 + CSRF 就绪就继续，不等 networkidle
        if not wait_for_ready(page, _ready, timeout_ms=15000,
                              what="网络学堂页面就绪"):
            page.wait_for_timeout(3000)

        # 判断是否登录
        if not _on_logged_in_page(page):
            ctx.close()
            raise LoginRequiredError(site["id"], site["name"])

        # 优先用配置 -> 其次缓存 -> 最后现场扫描
        configured = site.get("courses") or []
        if configured:
            courses = [{"id": c["wlkcid"], "name": c.get("name", "")} for c in configured]
        elif os.path.exists(cache_file):
            with open(cache_file, encoding="utf-8") as f:
                raw = json.load(f)
            # 兼容旧缓存格式
            if raw and isinstance(raw[0], str):
                courses = [{"id": x, "name": ""} for x in raw]
            else:
                courses = raw
        else:
            courses = _get_courses(page)
            if courses:
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(courses, f, ensure_ascii=False, indent=2)

        if not courses:
            ctx.close()
            return []

        # 从 cookie 里拿 CSRF token
        csrf = _csrf_from_ctx(ctx)

        # 逐门课抓作业
        all_homeworks = []
        for c in courses:
            data = _fetch_one_course(page, c["id"], csrf)
            body = data.get("body") if isinstance(data, dict) else ""
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = None
            if parsed:
                all_homeworks.extend(_extract(parsed, site, c.get("name", "")))

        ctx.close()
        return all_homeworks


def login(site, base_dir, manual=True):
    """登录。

    manual=False 时走无头静默登录；若清华 SSO 要求二次验证，
    静默登录会返回失败，由上层决定是否弹出窗口让用户过验证。
    """
    from .auth import silent_login, interactive_login
    if manual:
        return interactive_login(site, base_dir)
    ok, _reason = silent_login(site, base_dir)
    return ok
