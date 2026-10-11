"""Shared browser login helpers for username/password learning platforms."""
import os
import re
import time

from playwright.sync_api import sync_playwright


_USERNAME_HINTS = ("user", "account", "login", "student", "number", "phone", "email", "学号", "工号", "账号", "用户名", "手机号")
_IGNORE_HINTS = ("captcha", "verify", "verification", "sms", "otp", "code", "验证码", "校验码", "短信")
_LOGIN_TEXT = re.compile(r"登录|登 录|login|sign in|密码登录|账号登录", re.I)
_DEFAULT_LOGIN_URLS = {
    "learn": "https://learn.tsinghua.edu.cn/f/login",
}


def site_credentials(site):
    """Read new credential fields while remaining compatible with old Tyche config."""
    return (
        site.get("username") or site.get("basic_user") or "",
        site.get("password") or site.get("basic_pass") or "",
    )


def _visible(locator):
    try:
        return locator.is_visible()
    except Exception:
        return False


def _field_hint(locator):
    bits = []
    for attr in ("name", "id", "placeholder", "aria-label", "autocomplete", "type"):
        try:
            bits.append(locator.get_attribute(attr) or "")
        except Exception:
            pass
    return " ".join(bits).lower()


def _find_form(page, site):
    password_selector = site.get("login_password_selector") or 'input[type="password"]'
    scopes = [page]
    try:
        scopes.extend(frame for frame in page.frames if frame != page.main_frame)
    except Exception:
        pass
    for scope in scopes:
        try:
            passwords = scope.locator(password_selector)
            password = next((item for item in passwords.all() if _visible(item)), None)
        except Exception:
            continue
        if password is None:
            continue

        username_selector = site.get("login_username_selector")
        if username_selector:
            try:
                username = scope.locator(username_selector).first
                if _visible(username):
                    return username, password
            except Exception:
                pass

        try:
            form = password.locator("xpath=ancestor::form[1]")
            candidates = form.locator("input").all() if form.count() else []
            if not candidates:
                candidates = scope.locator("input").all()
        except Exception:
            candidates = scope.locator("input").all()

        ranked = []
        for field in candidates:
            if not _visible(field):
                continue
            try:
                field_type = (field.get_attribute("type") or "text").lower()
            except Exception:
                field_type = "text"
            if field_type in {"password", "hidden", "submit", "button", "checkbox", "radio", "file"}:
                continue
            hint = _field_hint(field)
            if any(word in hint for word in _IGNORE_HINTS):
                continue
            score = sum(4 for word in _USERNAME_HINTS if word in hint)
            if field_type in {"text", "email", "tel", "number"}:
                score += 1
            ranked.append((score, field))
        if ranked:
            ranked.sort(key=lambda entry: entry[0], reverse=True)
            return ranked[0][1], password
    return None


def _open_password_login(page):
    """Open an account/password form when the landing page only shows an entry button."""
    try:
        links = page.locator("a[href]").all()
    except Exception:
        links = []
    for link in links:
        if not _visible(link):
            continue
        try:
            href = link.get_attribute("href") or ""
            label = link.inner_text() or ""
            if re.search(r"login|sign.?in|auth|登录", href + " " + label, re.I):
                link.click(timeout=2500)
                page.wait_for_timeout(700)
                return True
        except Exception:
            continue

    for role in ("link", "button"):
        try:
            items = page.get_by_role(role, name=_LOGIN_TEXT).all()
        except Exception:
            items = []
        for item in items:
            if not _visible(item):
                continue
            try:
                item.click(timeout=2500)
                page.wait_for_timeout(700)
                return True
            except Exception:
                continue
    return False


def _submit_form(page, password, site):
    selector = site.get("login_submit_selector")
    if selector:
        try:
            button = page.locator(selector).first
            if _visible(button):
                button.click(timeout=4000)
                return
        except Exception:
            pass

    try:
        form = password.locator("xpath=ancestor::form[1]")
        buttons = form.locator('button, input[type="submit"], [role="button"]').all()
    except Exception:
        buttons = page.locator('button, input[type="submit"], [role="button"]').all()
    for button in buttons:
        if not _visible(button):
            continue
        try:
            label = " ".join(filter(None, [button.inner_text(), button.get_attribute("value"), button.get_attribute("aria-label")]))
        except Exception:
            label = ""
        if _LOGIN_TEXT.search(label):
            try:
                button.click(timeout=4000)
                return
            except Exception:
                pass
    try:
        password.press("Enter")
    except Exception:
        pass


def _is_success(validate, page, site):
    try:
        return bool(validate(page, site))
    except Exception:
        return False


def login_with_credentials(site, base_dir, validate, manual_required, *, manual_only=False, use_http_auth=False):
    """Try an account/password form, then leave the headed browser for manual completion."""
    username, password = site_credentials(site)
    session_path = os.path.join(base_dir, site["session"])
    os.makedirs(base_dir, exist_ok=True)
    login_url = (site.get("login_url") or _DEFAULT_LOGIN_URLS.get(site.get("type"))
                 or site.get("url") or site["base_url"])

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False, channel="msedge")
        context_args = {}
        if use_http_auth and username and password:
            context_args["http_credentials"] = {"username": username, "password": password}
        context = browser.new_context(**context_args)
        page = context.new_page()
        try:
            try:
                page.goto(login_url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(900)
            except Exception:
                # Keep the headed browser available; the user may still navigate
                # to the login page manually after the fallback notice appears.
                pass

            auto_attempted = False
            if not manual_only and username and password:
                form = _find_form(page, site)
                if form is None and _open_password_login(page):
                    form = _find_form(page, site)
                if form is not None:
                    user_field, pass_field = form
                    try:
                        user_field.fill(username, timeout=5000)
                        pass_field.fill(password, timeout=5000)
                        _submit_form(page, pass_field, site)
                        auto_attempted = True
                    except Exception:
                        auto_attempted = False

                deadline = time.monotonic() + (25 if auto_attempted else 3)
                while time.monotonic() < deadline:
                    if _is_success(validate, page, site):
                        context.storage_state(path=session_path)
                        return True
                    try:
                        page.wait_for_timeout(800)
                    except Exception:
                        return False

            # The browser remains open here so the user can handle captcha, MFA,
            # terms confirmation, or any site-specific login step.
            manual_required(site.get("name", "该站点"), auto_attempted)
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                if _is_success(validate, page, site):
                    context.storage_state(path=session_path)
                    return True
                try:
                    page.wait_for_timeout(1200)
                except Exception:
                    return False
            return False
        finally:
            browser.close()
