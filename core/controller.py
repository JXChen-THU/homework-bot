# ============================================================
# 任务清单 - 主程序
# ============================================================

import os
import sys
import json
import threading
import time
import uuid
import shutil
from datetime import datetime
import webview

from core.database import get_conn
from core.task_logic import compute_urgent, compute_normal
from core import paths

from core.site_runner import fetch_all, login_site


# ============================================================
# 路径处理
# 导入期只解析静态资源路径，**不建任何目录**（见 core/paths.py 的说明）。
# 数据目录在 main() 里经 core.paths.resolve() 决定，不可写时回退到
# %LOCALAPPDATA%\HomeworkTasks，避免用户把程序放在 Program Files 时
# 在导入期就抛 PermissionError 导致连界面都出不来。
# ============================================================
if getattr(sys, 'frozen', False):
    BASE = os.path.dirname(sys.executable)
    RES = getattr(sys, '_MEIPASS', BASE)
else:
    BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    RES = BASE

DATA_DIR = os.path.join(BASE, "data")
DB = os.path.join(DATA_DIR, "homework.db")

# ============================================================
# 常量
# ============================================================
ALERT_W = 360
ALERT_MARGIN = 20
GAP = 8

AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "HomeworkTasks"


# ============================================================
# 全局状态
# ============================================================
_main_window = None
_alert_window = None
_panel_window = None
_screen_w = 1920
_items_cache = []
_cache_lock = threading.Lock()

_alert_ready = False
_panel_ready = False
_main_visible = False
_first_load_done = False
_alert_shown = False
_panel_shown = False
_main_positioned = False
_quit_flag = False

# 用户最小化浮窗（缩到任务栏）
_alert_minimized = False
_panel_minimized = False
# 用户关闭浮窗（× 掉，不再自动弹出）
_alert_dismissed = False
_panel_dismissed = False



# ============================================================
# 配置初始化
# ============================================================
def ensure_sites_yaml():
    """首次启动时从模板生成 sites.yaml。返回 True 表示新生成。"""
    from core import paths as _paths
    target = _paths.SITES_FILE or os.path.join(BASE, "sites.yaml")
    if os.path.exists(target):
        return False
    template = os.path.join(RES, "sites.yaml.template")
    if os.path.exists(template):
        try:
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            shutil.copyfile(template, target)
            print(f"[init] 已生成 {target}")
            return True
        except Exception as e:
            print(f"[init] 生成 sites.yaml 失败：{type(e).__name__}: {e}")
    return False


def upgrade_credentials():
    """启动时把 sites.yaml 里残留的明文账号密码加密。

    历史配置文件（或用户手工写的明文）会在这一步自动变成密文，
    用户不需要做任何操作。
    """
    try:
        from core.site_runner import upgrade_plaintext_credentials
        upgrade_plaintext_credentials()
    except Exception as e:
        print(f"[secrets] 加密升级跳过：{type(e).__name__}: {e}")


def _setup_runtime_paths():
    """解析数据目录并创建它。返回 (ok, 错误信息)。

    必须在任何读写 data/ 或 sites.yaml 的动作之前调用。
    """
    from core import paths as _paths

    global DATA_DIR, DB
    _paths.resolve()
    if not _paths.ensure_dirs():
        return False, f"无法创建数据目录：{_paths.DATA_DIR}"

    # 同步到本模块与 site_runner（后者有自己的模块级常量）
    DATA_DIR = _paths.DATA_DIR
    DB = os.path.join(DATA_DIR, "homework.db")
    try:
        from core import site_runner
        site_runner.apply_paths()
    except Exception as e:
        print(f"[init] 同步站点调度器路径失败：{type(e).__name__}: {e}")

    return True, None


def _fatal_dialog(title, message):
    """用系统消息框告知用户。

    窗口化打包（console=False）下没有控制台可看，出问题必须靠弹窗，
    否则用户只看到「程序没反应」。
    """
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)  # MB_ICONERROR
    except Exception:
        pass


# ============================================================
# 浮窗布局
# ============================================================
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


def show_main_window():
    global _main_visible, _main_positioned
    if not _main_window:
        return
    try:
        try:
            s = webview.screens[0]
            W, H = s.width, s.height
        except Exception:
            W, H = 1920, 1080

        MW, MH = 1000, 720
        if W < MW + 40:
            MW = W - 40
        if H < MH + 80:
            MH = H - 80

        # 先 restore 从任务栏拉出来，再 resize + move 到右下角，最后 show
        _main_window.restore()
        _main_window.resize(MW, MH)
        _main_window.move(W - MW - 20, H - MH - 60)
        _main_window.show()
        _main_positioned = True
    except Exception:
        pass
    _main_visible = True


def layout_all():
    """重新布局红窗和白窗。
    - 用户最小化或关闭的浮窗不再自动弹出
    """
    global _alert_shown, _panel_shown
    if not _alert_window or not _panel_window:
        return

    with _cache_lock:
        items = list(_items_cache)

    urgent = compute_urgent(items)
    normal = compute_normal(items, urgent)

    W = _screen_w
    y_default = ALERT_MARGIN

    # ========== 红窗 ==========
    if urgent and not _alert_dismissed:
        w = ALERT_W
        h = _calc_height(len(urgent), header=42, row=20, max_rows=8, max_h=260)

        if not _alert_minimized:
            try:
                _alert_window.show()
                _alert_window.resize(w, h)
                if not _alert_shown:
                    x = W - ALERT_W - ALERT_MARGIN
                    _alert_window.move(x, y_default)
                    _alert_shown = True
            except Exception:
                pass

            if _alert_ready:
                js = (f'window.__updateUrgent && '
                      f'window.__updateUrgent({json.dumps(urgent, ensure_ascii=False)})')
                try:
                    _alert_window.evaluate_js(js)
                except Exception as e:
                    print(f"[alert] {e}")

        alert_bottom = y_default + h + GAP
    else:
        try:
            _alert_window.hide()
        except Exception:
            pass
        alert_bottom = y_default

    # ========== 白窗 ==========
    if normal and not _panel_dismissed:
        w = ALERT_W
        h = _calc_height(len(normal), header=46, row=22, max_rows=12, max_h=360)

        if not _panel_minimized:
            try:
                _panel_window.show()
                _panel_window.resize(w, h)
                if not _panel_shown:
                    x = W - ALERT_W - ALERT_MARGIN
                    y = alert_bottom if (urgent and not _alert_dismissed) else y_default
                    _panel_window.move(x, y)
                    _panel_shown = True
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


# ============================================================
# 开机自启动
# ============================================================
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


# ============================================================
# Api 类
# ============================================================
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

    # ---------- 主窗口显示/隐藏 ----------
    def show_main(self):
        show_main_window()
        return {"ok": True}

    def hide_main(self):
        """彻底隐藏主窗口（从任务栏消失）。"""
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

    # ---------- 浮窗：最小化到任务栏 ----------
    def minimize_alert(self):
        global _alert_minimized
        _alert_minimized = True
        try:
            _alert_window.minimize()
        except Exception:
            pass
        return {"ok": True}

    def minimize_panel(self):
        global _panel_minimized
        _panel_minimized = True
        try:
            _panel_window.minimize()
        except Exception:
            pass
        return {"ok": True}

    # ---------- 浮窗：× 关闭（不再自动弹出） ----------
    def dismiss_alert(self):
        global _alert_dismissed
        _alert_dismissed = True
        try:
            _alert_window.hide()
        except Exception:
            pass
        return {"ok": True}

    def dismiss_panel(self):
        global _panel_dismissed
        _panel_dismissed = True
        try:
            _panel_window.hide()
        except Exception:
            pass
        return {"ok": True}

    # ---------- 浮窗：重新打开 ----------
    def reset_floating(self):
        """重置浮窗的关闭/最小化状态，让它们重新显示。
        由主面板的「重置浮窗」按钮调用。
        """
        global _alert_dismissed, _panel_dismissed
        global _alert_minimized, _panel_minimized
        global _alert_shown, _panel_shown

        _alert_dismissed = False
        _panel_dismissed = False
        _alert_minimized = False
        _panel_minimized = False
        # 重置定位标志，让浮窗重新回到右上角（避免上次拖走后位置怪怪的）
        _alert_shown = False
        _panel_shown = False

        # 关键：从任务栏恢复（如果之前被最小化），show() 对已最小化的窗口无效
        for w in (_alert_window, _panel_window):
            if w is None:
                continue
            try:
                w.restore()
            except Exception:
                pass

        layout_all()
        return {"ok": True}

    # ---------- 任务数据 ----------
    def get_homeworks(self):
        global _items_cache, _first_load_done

        try:
            homeworks, errors, need_login = fetch_all()
        except Exception as e:
            homeworks = []
            errors = [{"site_name": "抓取", "message": f"{type(e).__name__}: {e}"}]
            need_login = []

        conn = get_conn(DB)
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

        if not _first_load_done:
            _first_load_done = True
            show_main_window()

        return {"items": items, "errors": errors, "need_login": need_login,
                "startup": paths.startup_messages()}

    def toggle_done(self, uid, done):
        conn = get_conn(DB)
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

    # ---------- 登录 ----------
    def login(self, site_id):
        """用户点「登录」时调用。

        login_site 会先试一次无头静默登录，不行再弹出可见窗口；
        对于清华 SSO 这种要求二次验证的站点，可见窗口里的账号密码
        已经自动填好，用户只需要输入验证码。
        """
        ok = login_site(site_id)
        return {"ok": ok}

    # ---------- 手动任务 ----------
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

        conn = get_conn(DB)
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
        conn = get_conn(DB)
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

    # ---------- 设置面板：站点管理 ----------
    def list_sites(self):
        from core.site_runner import load_sites, DATA_DIR as _DATA
        from adapters.auth import _plain, has_credentials, credentials_broken
        sites = load_sites(include_disabled=True)
        result = []
        for s in sites:
            sess = s.get("session")
            has_session = False
            if sess:
                has_session = os.path.exists(os.path.join(_DATA, sess))
            # 所有站点都支持「填账号密码 → 后台静默登录」，雨课堂除外（必须扫码）
            stype = s.get("type")
            result.append({
                "id": s["id"],
                "name": s["name"],
                "type": stype,
                "enabled": s.get("enabled", True),
                "has_session": has_session,
                # 用解密后的值判断，密文解不开时视为「未填」
                "has_credentials": has_credentials(s),
                "credentials_broken": credentials_broken(s),
                "needs_school": stype == "smartestu",
                "school": s.get("school", ""),
                # Tyche 还要一对 Basic Auth 账号密码（浏览器弹窗用）
                # 用解密后的值判断，密文解不开时视为没填
                "has_basic_auth": bool(_plain(s.get("basic_user"))
                                       and _plain(s.get("basic_pass"))),
                "supports_silent_login": stype != "yuketang",
            })
        return {"sites": result}

    def get_site_credentials(self, site_id):
        """把站点账号密码解密后交给设置界面（空账号密码会清掉旧会话，故只回显当前值）。"""
        from core.site_runner import load_sites
        from adapters.auth import _plain
        for s in load_sites(include_disabled=True):
            if s["id"] == site_id:
                broken = False
                try:
                    from adapters.auth import credentials_broken
                    broken = credentials_broken(s)
                except Exception:
                    pass
                return {
                    "ok": True,
                    "username": _plain(s.get("username", "")),
                    "password": _plain(s.get("password", "")),
                    "school": s.get("school", ""),
                    "basic_user": _plain(s.get("basic_user", "")),
                    "basic_pass": _plain(s.get("basic_pass", "")),
                    "type": s.get("type", ""),
                    # 密文解不开（换了电脑/Windows 账户）时提醒用户重填
                    "undecryptable": broken,
                }
        return {"error": "站点不存在"}

    def save_site_credentials(self, site_id, username, password,
                              school=None, basic_user=None, basic_pass=None):
        """保存站点账号密码（加密后写入 sites.yaml）。"""
        from core.site_runner import load_sites, save_sites
        from core.secrets import encrypt
        sites = load_sites(include_disabled=True)
        for s in sites:
            if s["id"] == site_id:
                s["username"] = encrypt((username or "").strip())
                s["password"] = encrypt((password or "").strip())
                if school is not None:
                    s["school"] = (school or "").strip()
                if basic_user is not None:
                    s["basic_user"] = encrypt((basic_user or "").strip())
                if basic_pass is not None:
                    s["basic_pass"] = encrypt((basic_pass or "").strip())
                ok, err = save_sites(sites)
                if not ok:
                    # 不能只 print：窗口化 exe 下 print 会被丢弃，
                    # 用户会看到"保存成功"而文件其实没变。
                    return {"error": f"写入 sites.yaml 失败：{err}"}
                # 账号密码变了，清掉旧会话，下次刷新会用新账号重新登录
                self._wipe_session_files(s["id"], s.get("session"))
                return {"ok": True}
        return {"error": "站点不存在"}

    @staticmethod
    def _wipe_session_files(site_id, session_name):
        from core.site_runner import DATA_DIR as _DATA
        if session_name:
            p = os.path.join(_DATA, session_name)
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
        cache = os.path.join(_DATA, f"{site_id}_courses.json")
        if os.path.exists(cache):
            try:
                os.remove(cache)
            except Exception:
                pass

    def apply_sites_changes(self, changes):
        from core.site_runner import load_sites, save_sites, DATA_DIR as _DATA
        sites = load_sites(include_disabled=True)
        by_id = {s["id"]: s for s in sites}
        to_delete = set()

        def _wipe_session(sid):
            s = by_id.get(sid)
            if not s:
                return
            self._wipe_session_files(sid, s.get("session"))

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
        ok, err = save_sites(new_sites)
        if not ok:
            return {"error": f"写入 sites.yaml 失败：{err}"}
        return {"ok": True}

    # ---------- 开机自启动 ----------
    def get_autostart(self):
        return get_autostart_status()

    def set_autostart(self, enabled):
        return set_autostart_status(bool(enabled))


# ============================================================
# 主窗口关闭：任何方式关闭 → 退出程序
# ============================================================
def _on_main_closing():
    """主窗口被关闭（标题栏 X / 任务栏右键关闭 / Alt+F4）
    → 销毁所有窗口，退出程序。"""
    global _quit_flag
    _quit_flag = True

    def _close_all():
        time.sleep(0.2)
        for w in (_alert_window, _panel_window):
            if w is None:
                continue
            try:
                w.destroy()
            except Exception:
                pass

    threading.Thread(target=_close_all, daemon=True).start()
    return True


def _on_main_minimized():
    """主窗被最小化到任务栏 → 逻辑上视为不可见，浮窗按钮切换为「显示主面板」。"""
    global _main_visible
    _main_visible = False


def _on_main_restored():
    """主窗从任务栏恢复 → 逻辑上视为可见。"""
    global _main_visible
    _main_visible = True


def _startup_show_fallback():
    global _first_load_done
    for _ in range(600):
        time.sleep(0.5)
        if _quit_flag or _first_load_done:
            return
    if not _first_load_done and not _quit_flag:
        _first_load_done = True
        show_main_window()


# ============================================================
# 主函数
# ============================================================
def main():
    global _main_window, _alert_window, _panel_window, _screen_w

    # 1) 先解析数据目录。必须在任何 data/ 读写之前。
    ok, err = _setup_runtime_paths()
    if not ok:
        _fatal_dialog("任务清单 - 无法启动", err)
        sys.exit(1)

    # 2) 首次启动生成配置；把残留明文账密加密
    try:
        ensure_sites_yaml()
        upgrade_credentials()
    except Exception as e:
        print(f"[init] 启动初始化失败：{type(e).__name__}: {e}")

    api = Api()

    index_path = os.path.join(RES, "web", "index.html")
    alert_path = os.path.join(RES, "web", "alert.html")
    panel_path = os.path.join(RES, "web", "panel.html")

    # 3) 建窗口与启动 GUI。目标机缺少 WebView2 Runtime 等情况下，
    #    异常会在 webview.start() 抛出——必须给出中文、可操作的提示，
    #    否则用户只看到"程序没反应"。
    try:
        _main_window = webview.create_window(
            "任务清单", index_path, js_api=api,
            width=1000, height=720,
            minimized=True,
        )
        _main_window.events.closing += _on_main_closing
        _main_window.events.minimized += _on_main_minimized
        _main_window.events.restored += _on_main_restored

        _alert_window = webview.create_window(
            "警告", alert_path, js_api=api,
            frameless=True, on_top=True, hidden=True,
            width=ALERT_W, height=140,
            transparent=True,
        )
        _panel_window = webview.create_window(
            "提醒", panel_path, js_api=api,
            frameless=True, on_top=True, hidden=True,
            width=ALERT_W, height=200,
            transparent=True,
        )

        threading.Thread(target=_startup_show_fallback, daemon=True).start()

        def _after_start():
            global _screen_w
            try:
                s = webview.screens[0]
                W, H = s.width, s.height
            except Exception:
                W, H = 1920, 1080
            _screen_w = W
            time.sleep(0.3)
            layout_all()

        webview.start(_after_start)
    except Exception as e:
        print(f"[gui] 界面启动失败：{type(e).__name__}: {e}")
        _fatal_dialog(
            "任务清单 - 界面无法启动",
            "程序界面启动失败，最常见的原因是缺少 Microsoft Edge WebView2 运行时。\n\n"
            "请到微软官网搜索并安装“WebView2 Runtime”（常青版即可），然后重新打开本程序。\n\n"
            f"技术信息：{type(e).__name__}: {e}"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()