import os
import re
from urllib.parse import urlparse
from .base import Homework, LoginRequiredError


# 匹配形如 "2026-10-09 23:59:00" 的日期
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def _extract_course_name(page):
    """从课程主页标题里取课程名，例如「离散数学（1）马老师课堂」。

    TUOJ 的课程名不带固定 id，这里用几个候选选择器依次尝试，
    取不到就返回空字符串（不影响作业抓取）。
    """
    for sel in ["h1", "h2", ".course-title", ".page-header", "title"]:
        try:
            el = page.query_selector(sel)
            if not el:
                continue
            text = (el.inner_text() or "").strip()
            if not text:
                continue
            # <title> 形如「课程主页 - TUOJ」，取主标题部分
            text = text.split(" - ")[0].strip()
            text = re.sub(r"\s+", " ", text)
            # 标题里常带学期徽标，例如「离散数学（1）马老师课堂 2026秋」
            m = re.match(r"^(.*?)\s*\d{4}\s*[春秋夏冬]\s*$", text)
            if m:
                text = m.group(1).strip()
            if text and text not in ("课程主页", "TUOJ"):
                return text
        except Exception:
            continue
    return ""


def _extract(page, site):
    """从页面里抠出所有作业，返回 Homework 列表"""
    parsed = urlparse(site["url"])
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    course_name = _extract_course_name(page)
    result = []

    # 每个作业装在一个 div.panel.panel-default 里
    for panel in page.query_selector_all("div.panel.panel-default"):
        title_el = panel.query_selector(".panel-heading a")
        codes = panel.query_selector_all("code")
        if not title_el or len(codes) < 2:
            continue

        start = codes[0].inner_text().strip()
        end = codes[1].inner_text().strip()
        # 两个都必须是日期格式，否则跳过
        if not DATE_RE.fullmatch(start) or not DATE_RE.fullmatch(end):
            continue

        title = title_el.inner_text().strip()
        href = title_el.get_attribute("href") or ""
        # 从 /course/8/contest/82 里抠出 "82"
        m = re.search(r"/course/\d+/contest/(\d+)", href)
        if not m:
            continue
        contest_id = m.group(1)

        result.append(Homework(
            site=site["id"],
            site_name=site["name"],
            uid=f"{site['id']}:{contest_id}",
            title=title,
            course=course_name,
            start=start,
            end=end,
            url=base_url + href,
        ))
    return result


def _is_logged_in(page, site):
    """判断是否已登录：URL 没跳到登录页，且能找到作业或用户菜单"""
    u = page.url.lower()
    if "login" in u or "sso" in u or "auth" in u or "id.tsinghua.edu.cn" in u:
        return False
    if len(_extract(page, site)) > 0:
        return True
    # 没有作业时（例如课程还没布置作业）用退出登录入口判断
    try:
        body = page.locator("body").inner_text()
    except Exception:
        return False
    return ("退出" in body) or ("Logout" in body) or ("logout" in body)


def is_logged_in(page, site):
    """给静默登录用的校验函数。"""
    return _is_logged_in(page, site)


# 静默登录配置
# 清华 OJ 的本地账号密码表单实测不可用（/api/user/login 直接返回 500），
# 因此走「使用 清华ID 登录」→ 清华统一身份认证（id.tsinghua.edu.cn）。
LOGIN = {
    # 先展开导航栏「登录」下拉，再点「使用 清华ID 登录」
    "pre_click": ["a.dropdown-toggle:has-text('登录')"],
    "after_pre_click_wait": 1200,
    "pre_click2": ["a:has-text('使用 清华ID 登录')", "//a[contains(.,'清华ID')]"],
    "after_pre_click2_wait": 5000,
    "username": ["#i_user", "input[name=i_user]", "input[name=username]"],
    "password": ["#i_pass", "input[name=i_pass]", "input[type=password]"],
    "submit": ["a:has-text('登录')", "#login", "button[type=submit]",
               "input[type=submit]", "button:has-text('登录')"],
    "after_goto_wait": 2500,
    # 清华 SSO 对无头浏览器**必定**要求二次验证（实测稳定复现），
    # 而真实窗口会被直接放行。所以后台重登时跳过无头那一档，
    # 直接开屏幕外真实窗口。
    "skip_headless": True,
    # 清华 SSO 的二次验证页面（人工兜底时会停在这里）
    "mfa_selectors": [".second-verify", "#secondVerify", ".verify-way", ".auth-way"],
    "mfa_phrases": ["请选择以下方式之一获取验证码", "验证码到您的微信",
                    "二次验证", "二次认证", "本次登录需要进行二次验证"],
}


def fetch_homeworks(site, base_dir):
    """静默抓取：加载会话 → 打开页面 → 抠数据"""
    session = os.path.join(base_dir, site["session"])
    if not os.path.exists(session):
        raise LoginRequiredError(site["id"], site["name"])

    from .auth import browser_session, wait_for_ready

    with browser_session() as browser:
        ctx = browser.new_context(storage_state=session)
        page = ctx.new_page()
        page.goto(site["url"], wait_until="domcontentloaded")
        # 等到作业列表出现就继续（不等 networkidle）
        got = wait_for_ready(
            page, lambda pg: len(_extract(pg, site)) > 0,
            timeout_ms=15000, what="清华 OJ 作业列表")
        if not got:
            page.wait_for_timeout(2000)

        if not _is_logged_in(page, site):
            ctx.close()
            raise LoginRequiredError(site["id"], site["name"])

        items = _extract(page, site)
        ctx.close()
        return items


def login(site, base_dir, manual=True):
    """登录。

    manual=False 时走无头静默登录（清华 ID）；
    若清华 SSO 要求二次验证，静默登录会失败，由上层决定是否弹出窗口。
    """
    from .auth import silent_login, interactive_login
    if manual:
        return interactive_login(site, base_dir)
    ok, _reason = silent_login(site, base_dir)
    return ok
