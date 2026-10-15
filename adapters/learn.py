import os
import json
from .base import Homework, LoginRequiredError, FetchFailedError, session_path


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
# 因此只认 mfa_phrases / mfa_selectors 里那些"二次验证页面上的真实字样"，避免误判。
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


def _read_courses_cache(cache_file):
    """读课程缓存；不存在或损坏都返回 None。

    以前这里直接 `json.load`，缓存被写坏（写盘中断、磁盘满、同步工具）后
    **每一次刷新**都在这里抛 JSONDecodeError，而 site_runner 的"会话损坏"
    判据只看会话文件，于是既不自愈也不重登 —— 用户只能去设置面板点「清除登录」。
    损坏的缓存应当被丢弃并重建，而不是让整个站点永久失败。
    """
    if not os.path.exists(cache_file):
        return None
    try:
        with open(cache_file, encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, list) else None
    except Exception as e:
        print(f"[learn] 课程缓存损坏，已丢弃并重新扫描：{type(e).__name__}: {e}")
        try:
            os.remove(cache_file)
        except Exception:
            pass
        return None


def _write_courses_cache(cache_file, courses):
    """原子写课程缓存（这里原先是全仓唯一一处绕过 atomic_write_text 的数据直写）。"""
    text = json.dumps(courses, ensure_ascii=False, indent=2)
    try:
        from core.site_runner import atomic_write_text
        ok, err = atomic_write_text(cache_file, text)
        if not ok:
            print(f"[learn] 课程缓存写入失败（不影响本次抓取）：{err}")
    except Exception as e:
        print(f"[learn] 课程缓存写入异常：{type(e).__name__}: {e}")


def fetch_homeworks(site, base_dir):
    """静默抓取所有课程的作业"""
    session = session_path(base_dir, site)
    if not os.path.exists(session):
        raise LoginRequiredError(site["id"], site["name"])

    # 课程列表缓存（避免每次扫描）
    cache_file = os.path.join(base_dir, f"{site['id']}_courses.json")

    from .auth import (browser_session, wait_for_ready, login_url, timeout_detail,
                       page_on_login_page, site_urls_of,
                       SESSION_PROBE_MS, DATA_READY_MS)

    with browser_session() as browser:
        ctx = browser.new_context(storage_state=session)
        page = ctx.new_page()
        page.goto(site["base_url"], wait_until="domcontentloaded")
        logins = [login_url(site), "id.tsinghua.edu.cn"]
        # 先给一个**很短的**预检（1 秒）：会话有效时 1.3 秒左右就绪，
        # 会话失效时页面已经跳到登录页 —— wait_for_ready 会当场结束等待，
        # 不再白睡 15 秒（实测过期会话 0.1~0.3 秒就判定出"要登录"）。
        ready = wait_for_ready(
            page, _ready, timeout_ms=SESSION_PROBE_MS,
            what="网络学堂页面就绪（会话预检）",
            login_urls=logins, site_urls=site_urls_of(site))
        if not ready:
            # 预检没就绪：如果已经落在登录页/认证页上，那就是**确定**会话失效，
            # 不必再等（这正是以前白睡 15 秒的那条路）。
            if page_on_login_page(page, logins, site_urls=site_urls_of(site)):
                ctx.close()
                raise LoginRequiredError(site["id"], site["name"])
            # 不在登录页上，也没就绪 —— 可能只是页面/接口慢：给一次数据时限。
            wait_for_ready(
                page, _ready, timeout_ms=DATA_READY_MS,
                what="网络学堂页面就绪（数据）",
                login_urls=logins, site_urls=site_urls_of(site))

        # 判断是否登录
        if not _on_logged_in_page(page):
            if page_on_login_page(page, logins, site_urls=site_urls_of(site)):
                ctx.close()
                raise LoginRequiredError(site["id"], site["name"])
            # ★ 等了 DATA_READY_MS 还没内容，又**没有任何**失效信号
            #   （页面没跳登录页）—— 这时候判不出来，就不能报"需要登录"：
            #   那会触发一次白跑的后台重登（每站 10~60 秒，还可能弹窗）。
            #   走 FetchFailedError：播报一条可读的错误，用户点刷新重试。
            ctx.close()
            raise FetchFailedError(site["id"], site["name"],
                                   timeout_detail("网络学堂页面"), timeout=True)

        # 优先用配置 -> 其次缓存 -> 最后现场扫描
        configured = site.get("courses") or []
        if configured:
            courses = [{"id": c["wlkcid"], "name": c.get("name", "")} for c in configured]
        else:
            raw = _read_courses_cache(cache_file)
            if raw is not None:
                # 兼容旧缓存格式
                if raw and isinstance(raw[0], str):
                    courses = [{"id": x, "name": ""} for x in raw]
                else:
                    courses = raw
            else:
                courses = _get_courses(page)
                if courses:
                    _write_courses_cache(cache_file, courses)

        if not courses:
            ctx.close()
            return []

        # 从 cookie 里拿 CSRF token
        csrf = _csrf_from_ctx(ctx)

        # 逐门课抓作业
        all_homeworks = []
        failed_courses = []
        for c in courses:
            data = _fetch_one_course(page, c["id"], csrf)
            # httpStatus 一直是产出了却没人读的字段：401/500 的错误页会被
            # json.loads 静默吞掉，于是"会话过期"被播报成"完成 0 条"，
            # 既不进 errors 也不触发重登。
            status = data.get("httpStatus") if isinstance(data, dict) else None
            body = (data.get("body") if isinstance(data, dict) else "") or ""
            if status in (401, 403):
                ctx.close()
                raise LoginRequiredError(site["id"], site["name"])
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = None
            if parsed:
                all_homeworks.extend(_extract(parsed, site, c.get("name", "")))
            else:
                label = c.get("name") or c["id"]
                failed_courses.append(f"{label}（HTTP {status}）")

        ctx.close()

        if failed_courses and not all_homeworks:
            # 所有课程都没有可用响应：这是抓取失败，不是"没有作业"。
            raise FetchFailedError(
                site["id"], site["name"],
                "作业接口没有一条可用响应：" + "、".join(failed_courses[:3]))
        if failed_courses:
            print(f"[learn] {len(failed_courses)} 门课的接口没有可用响应，已跳过："
                  + "、".join(failed_courses[:3]))

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
