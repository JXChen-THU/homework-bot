import os
import sys
import json
import sqlite3
import threading
import time
import uuid
import shutil
from datetime import datetime
import webview

from scraper import fetch_all, login_site

if getattr(sys, 'frozen', False):
    BASE = os.path.dirname(sys.executable)
    RES = getattr(sys, '_MEIPASS', BASE)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
    RES = BASE

DB = os.path.join(BASE, "homework.db")


def ensure_sites_yaml():
    target = os.path.join(BASE, "sites.yaml")
    if os.path.exists(target):
        return
    template = os.path.join(RES, "sites.yaml.template")
    if os.path.exists(template):
        shutil.copyfile(template, target)
        print(f"[init] 已生成 {target}")


ALERT_W = 360
ALERT_MARGIN = 20
GAP = 8

AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "HomeworkTasks"

_main_window = None
_alert_window = None
_panel_window = None
_alert_pos_x = 20
_items_cache = []
_cache_lock = threading.Lock()
_alert_ready = False
_panel_ready = False
_main_visible = True


def get_conn():
    conn = sqlite3.connect(DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS done (
            uid TEXT PRIMARY KEY,
            done_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS manual_tasks (
            uid TEXT PRIMARY KEY,
            title TEXT,
            course TEXT,
            due TEXT,
            url TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    return conn


def _parse_time(s):
    if not s:
        return None
    s = s.strip().replace("T", " ").replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def compute_urgent(items):
    now = datetime.now()
    result = []
    for it in items:
        if it.get("done"):
            continue
        end = it.get("end")
        if not end:
            continue
        due = _parse_time(end)
        if due is None:
            continue
        h = (due - now).total_seconds() / 3600
        if h <= 24:
            result.append({
                "uid": it["uid"],
                "title": it["title"],
                "hours_left": h,
            })
    result.sort(key=lambda x: x["hours_left"])
    return result


def compute_normal(items, urgent):
    urgent_uids = {u["uid"] for u in urgent}
    result = []
    for it in items:
        if it.get("done"):
            continue
        if it["uid"] in urgent_uids:
            continue
        result.append({
            "uid": it["uid"],
            "title": it["title"],
            "end": it.get("end") or "",
        })
    result.sort(key=lambda x: x["end"] or "9999")
    return result


def _calc_height(n, header=42, row=22, max_rows=8, max_h=260):
    if n <= 0:
        return 60
    rows = min(n, max_rows)
    h = header + rows * row + 16
    if n > max_rows:
        h += 18
    return max(70, min(h, max_h))


def _sync_main():
    if not _main_window:
        return
    with _cache_lock:
        items = list(_items_cache)
    try:
        js = f'window.__syncFromCache && window.__syncFromCache({json.dumps(items, ensure_ascii=False)})'
        _main_window.evaluate_js(js)
    except Exception:
        pass


def layout_all():
    if not _alert_window or not _panel_window:
        return

    with _cache_lock:
        items = list(_items_cache)

    urgent = compute_urgent(items)
    normal = compute_normal(items, urgent)

    x = _alert_pos_x
    y = ALERT_MARGIN

    if urgent:
        h = _calc_height(len(urgent), header=42, row=20, max_rows=8, max_h=260)
        try:
            _alert_window.resize(ALERT_W, h)
            _alert_window.move(x, y)
            _alert_window.show()
        except Exception:
            pass
        if _alert_ready:
            js = f'window.__updateUrgent && window.__updateUrgent({json.dumps(urgent, ensure_ascii=False)})'
            try:
                _alert_window.evaluate_js(js)
            except Exception as e:
                print(f"[alert] {e}")
        y += h + GAP
    else:
        try:
            _alert_window.hide()
        except Exception:
            pass

    if normal:
        h = _calc_height(len(normal), header=46, row=22, max_rows=12, max_h=360)
        try:
            _panel_window.resize(ALERT_W, h)
            _panel_window.move(x, y)
            _panel_window.show()
        except Exception:
            pass
        if _panel_ready:
            js = (f'window.__updateNormal && window.__updateNormal('
                  f'{json.dumps(normal, ensure_ascii=False)})')
            try:
                _panel_window.evaluate_js(js)
            except Exception as e:
                print(f"[panel] {e}")
    else:
        try:
            _panel_window.hide()
        except Exception:
            pass


def _get_start_command():
    if getattr(sys, 'frozen', False):
        return f'"{sys.executable}"'
    else:
        bat = os.path.join(BASE, "start.bat")
        return f'"{bat}"'


def get_autostart_status():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_READ)
        try:
            val, _ = winreg.QueryValueEx(key, AUTOSTART_NAME)
            winreg.CloseKey(key)
            return {"enabled": bool(val), "command": val}
        except FileNotFoundError:
            winreg.CloseKey(key)
            return {"enabled": False}
    except Exception as e:
        return {"enabled": False, "error": str(e)}


def set_autostart_status(enabled):
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_WRITE)
        if enabled:
            cmd = _get_start_command()
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


class Api:
    def alert_ready(self):
        global _alert_ready
        _alert_ready = True
        layout_all()
        return {"ok": True}

    def panel_ready(self):
        global _panel_ready
        _panel_ready = True
        layout_all()
        return {"ok": True}

    def show_main(self):
        global _main_visible
        if _main_window:
            try:
                _main_window.show()
                _main_window.restore()
            except Exception:
                pass
        _main_visible = True
        return {"ok": True}

    def hide_main(self):
        global _main_visible
        if _main_window:
            try:
                _main_window.hide()
            except Exception:
                pass
        _main_visible = False
        return {"ok": True}

    def is_main_visible(self):
        return {"visible": _main_visible}

    def get_homeworks(self):
        global _items_cache
        try:
            homeworks, errors, need_login = fetch_all()
        except Exception as e:
            homeworks = []
            errors = [{"site_name": "抓取", "message": f"{type(e).__name__}: {e}"}]
            need_login = []

        conn = get_conn()
        try:
            done_rows = dict(conn.execute("SELECT uid, done_at FROM done").fetchall())
            manual_rows = conn.execute(
                "SELECT uid, title, course, due, url FROM manual_tasks"
            ).fetchall()
        finally:
            conn.close()

        items = []
        for hw in homeworks:
            d = hw.to_dict()
            d["done"] = d["uid"] in done_rows
            d["manual"] = False
            items.append(d)

        for uid, title, course, due, url in manual_rows:
            items.append({
                "site": "manual",
                "site_name": "手动",
                "uid": uid,
                "title": title or "",
                "course": course or "",
                "start": "",
                "end": due or "",
                "url": url or "",
                "done": uid in done_rows,
                "manual": True,
            })

        with _cache_lock:
            _items_cache = items

        layout_all()
        return {"items": items, "errors": errors, "need_login": need_login}

    def toggle_done(self, uid, done):
        conn = get_conn()
        try:
            if done:
                conn.execute(
                    "INSERT OR REPLACE INTO done VALUES (?, ?)",
                    (uid, datetime.now().isoformat()),
                )
            else:
                conn.execute("DELETE FROM done WHERE uid=?", (uid,))
            conn.commit()
        finally:
            conn.close()

        with _cache_lock:
            for it in _items_cache:
                if it["uid"] == uid:
                    it["done"] = done
                    break

        layout_all()
        _sync_main()
        return {"ok": True}

    def login(self, site_id):
        from scraper import load_sites
        for s in load_sites(include_disabled=True):
            if s["id"] == site_id and s.get("type") == "tyche":
                if not (s.get("basic_user") and s.get("basic_pass")):
                    return {"ok": False, "error": "need_credentials"}
        ok = login_site(site_id)
        return {"ok": ok}

    def add_task(self, title, course, due, url):
        global _items_cache
        if not title or not title.strip():
            return {"error": "标题不能为空"}
        if not due or not due.strip():
            return {"error": "截止时间不能为空"}

        uid = "manual:" + uuid.uuid4().hex[:12]
        title = title.strip()
        course = (course or "").strip()
        due = due.strip()
        url = (url or "").strip()

        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO manual_tasks VALUES (?, ?, ?, ?, ?, ?)",
                (uid, title, course, due, url, datetime.now().isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

        with _cache_lock:
            _items_cache.append({
                "site": "manual",
                "site_name": "手动",
                "uid": uid,
                "title": title,
                "course": course,
                "start": "",
                "end": due,
                "url": url,
                "done": False,
                "manual": True,
            })

        layout_all()
        _sync_main()
        return {"ok": True, "uid": uid}

    def delete_task(self, uid):
        global _items_cache
        if not uid.startswith("manual:"):
            return {"error": "只能删除手动任务"}
        conn = get_conn()
        try:
            conn.execute("DELETE FROM manual_tasks WHERE uid=?", (uid,))
            conn.execute("DELETE FROM done WHERE uid=?", (uid,))
            conn.commit()
        finally:
            conn.close()
        with _cache_lock:
            _items_cache = [x for x in _items_cache if x["uid"] != uid]
        layout_all()
        _sync_main()
        return {"ok": True}

    def list_sites(self):
        from scraper import load_sites, BASE as _BASE
        sites = load_sites(include_disabled=True)
        result = []
        for s in sites:
            sess = s.get("session")
            has_session = False
            if sess:
                has_session = os.path.exists(os.path.join(_BASE, sess))
            has_cred = True
            if s.get("type") == "tyche":
                has_cred = bool(s.get("basic_user") and s.get("basic_pass"))
            result.append({
                "id": s["id"],
                "name": s["name"],
                "type": s["type"],
                "enabled": s.get("enabled", True),
                "has_session": has_session,
                "has_credentials": has_cred,
            })
        return {"sites": result}

    def get_site_credentials(self, site_id):
        from scraper import load_sites
        for s in load_sites(include_disabled=True):
            if s["id"] == site_id:
                return {
                    "ok": True,
                    "basic_user": s.get("basic_user", ""),
                    "basic_pass": s.get("basic_pass", ""),
                }
        return {"error": "站点不存在"}

    def save_site_credentials(self, site_id, basic_user, basic_pass):
        from scraper import load_sites, save_sites
        sites = load_sites(include_disabled=True)
        for s in sites:
            if s["id"] == site_id:
                s["basic_user"] = (basic_user or "").strip()
                s["basic_pass"] = (basic_pass or "").strip()
                save_sites(sites)
                return {"ok": True}
        return {"error": "站点不存在"}

    def apply_sites_changes(self, changes):
        from scraper import load_sites, save_sites, BASE as _BASE
        sites = load_sites(include_disabled=True)
        by_id = {s["id"]: s for s in sites}

        to_delete = set()

        def _wipe_session(sid):
            s = by_id.get(sid)
            if not s:
                return
            sess = s.get("session")
            if sess:
                p = os.path.join(_BASE, sess)
                if os.path.exists(p):
                    os.remove(p)
            cache = os.path.join(_BASE, f"{sid}_courses.json")
            if os.path.exists(cache):
                os.remove(cache)

        for ch in changes:
            sid = ch.get("id")
            if sid not in by_id:
                continue
            if ch.get("clear_session"):
                _wipe_session(sid)
            if ch.get("deleted"):
                to_delete.add(sid)
                _wipe_session(sid)

        for ch in changes:
            sid = ch.get("id")
            if sid in to_delete or sid not in by_id:
                continue
            by_id[sid]["enabled"] = bool(ch.get("enabled", True))

        new_sites = [s for s in sites if s["id"] not in to_delete]
        save_sites(new_sites)
        return {"ok": True}

    def get_autostart(self):
        return get_autostart_status()

    def set_autostart(self, enabled):
        return set_autostart_status(bool(enabled))


def background_ticker():
    while True:
        time.sleep(60)
        try:
            layout_all()
        except Exception as e:
            print(f"[bg] {e}")


def _on_main_closing():
    global _main_visible
    _main_visible = False
    try:
        _main_window.hide()
    except Exception:
        pass
    return False


def main():
    global _main_window, _alert_window, _panel_window, _alert_pos_x

    ensure_sites_yaml()

    api = Api()
    index_path = os.path.join(RES, "index.html")
    alert_path = os.path.join(RES, "alert.html")
    panel_path = os.path.join(RES, "panel.html")

    _main_window = webview.create_window(
        "任务清单", index_path, js_api=api,
        width=1000, height=720,
    )
    _main_window.events.closing += _on_main_closing

    _alert_window = webview.create_window(
        "警告", alert_path, js_api=api,
        frameless=True, on_top=True, hidden=True,
        width=ALERT_W, height=140,
        background_color="#e74c3c",
    )
    _panel_window = webview.create_window(
        "面板", panel_path, js_api=api,
        frameless=True, on_top=True, hidden=True,
        width=ALERT_W, height=200,
        background_color="#ffffff",
    )

    threading.Thread(target=background_ticker, daemon=True).start()

    def _after_start():
        global _alert_pos_x
        try:
            s = webview.screens[0]
            W, H = s.width, s.height
        except Exception:
            W, H = 1920, 1080

        _alert_pos_x = W - ALERT_W - ALERT_MARGIN

        MW, MH = 1000, 720
        if W < MW + 40:
            MW = W - 40
        if H < MH + 80:
            MH = H - 80
        try:
            _main_window.resize(MW, MH)
            _main_window.move(W - MW - 20, H - MH - 60)
        except Exception:
            pass

        time.sleep(0.3)
        layout_all()

    webview.start(_after_start)


if __name__ == "__main__":
    main()