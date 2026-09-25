import re
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
from .base import Homework, LoginRequiredError, session_path

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def _extract(page, site):
    parsed = urlparse(site["url"])
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    result = []
    for panel in page.query_selector_all("div.panel.panel-default"):
        title_el = panel.query_selector(".panel-heading a")
        codes = panel.query_selector_all("code")
        if not title_el or len(codes) < 2:
            continue
        start = codes[0].inner_text().strip()
        end = codes[1].inner_text().strip()
        if not DATE_RE.fullmatch(start) or not DATE_RE.fullmatch(end):
            continue
        title = title_el.inner_text().strip()
        href = title_el.get_attribute("href") or ""
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
    u = page.url.lower()
    if "login" in u or "sso" in u or "auth" in u:
        return False
    return len(_extract(page, site)) > 0


def fetch_homeworks(site, base_dir):
    session = session_path(site, base_dir)
    if not session or not __import__("os").path.exists(session):
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


def login(site, base_dir):
    session = session_path(site, base_dir)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="msedge")
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(site["url"])

        logged_in = False
        for _ in range(100):
            page.wait_for_timeout(3000)
            try:
                if _is_logged_in(page, site):
                    logged_in = True
                    break
            except Exception:
                pass

        if logged_in:
            ctx.storage_state(path=session)
        browser.close()
        return logged_in