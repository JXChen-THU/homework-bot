import os
import re
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
from .base import Homework, LoginRequiredError
from .auth import login_with_credentials


# 匹配形如 "2026-10-09 23:59:00" 的日期
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def _extract(page, site):
    """从页面里抠出所有作业，返回 Homework 列表"""
    parsed = urlparse(site["url"])
    base_url = f"{parsed.scheme}://{parsed.netloc}"
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
            course="",
            start=start,
            end=end,
            url=base_url + href,
        ))
    return result


def _is_logged_in(page, site):
    """判断是否已登录：URL 没跳到登录页，且能找到作业"""
    u = page.url.lower()
    if "login" in u or "sso" in u or "auth" in u:
        return False
    if _extract(page, site):
        return True
    try:
        body = page.locator("body").inner_text().lower()
        return "logout" in body or "退出登录" in body or "退出" in body
    except Exception:
        return False


def fetch_homeworks(site, base_dir):
    """静默抓取：加载会话 → 打开页面 → 抠数据"""
    session = os.path.join(base_dir, site["session"])
    if not os.path.exists(session):
        raise LoginRequiredError(site["id"], site["name"])

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="msedge")
        ctx = browser.new_context(storage_state=session)
        page = ctx.new_page()
        page.goto(site["url"])
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)

        if not _is_logged_in(page, site):
            browser.close()
            raise LoginRequiredError(site["id"], site["name"])

        items = _extract(page, site)
        browser.close()
        return items


def login(site, base_dir, manual_required=None):
    """自动填写账号密码；需要人工验证时保持登录窗口打开。"""
    return login_with_credentials(
        site, base_dir, _is_logged_in,
        manual_required or (lambda _name, _auto: None),
    )
