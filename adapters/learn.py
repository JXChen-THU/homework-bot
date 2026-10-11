import os
import json
from playwright.sync_api import sync_playwright
from .base import Homework, LoginRequiredError
from .auth import login_with_credentials


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


def fetch_homeworks(site, base_dir):
    """静默抓取所有课程的作业"""
    session = os.path.join(base_dir, site["session"])
    if not os.path.exists(session):
        raise LoginRequiredError(site["id"], site["name"])

    # 课程列表缓存（避免每次扫描）
    cache_file = os.path.join(base_dir, f"{site['id']}_courses.json")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="msedge")
        ctx = browser.new_context(storage_state=session)
        page = ctx.new_page()
        page.goto(site["base_url"])
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3000)

        # 判断是否登录
        if "login" in page.url.lower() or "sso" in page.url.lower():
            browser.close()
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
            browser.close()
            return []

        # 从 cookie 里拿 CSRF token
        csrf = ''
        for c in ctx.cookies():
            n = c['name'].lower()
            if 'csrf' in n or 'xsrf' in n:
                csrf = c['value']
                break

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

        browser.close()
        return all_homeworks


def _login_success(page, _site):
    url = page.url.lower()
    if any(part in url for part in ("login", "sso", "auth")):
        return False
    try:
        return "wlxt" in page.content() or "课程" in page.content()
    except Exception:
        return False


def login(site, base_dir, manual_required=None):
    """自动填写账号密码；统一身份认证的额外验证由用户完成。"""
    return login_with_credentials(
        site, base_dir, _login_success,
        manual_required or (lambda _name, _auto: None),
    )
