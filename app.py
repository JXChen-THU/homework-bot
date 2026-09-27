# ============================================================
# 任务清单 - 主程序
# 职责：创建窗口、管理数据、连接前端(HTML/JS)和后端(Python)
# ============================================================

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


# ============================================================
# 路径处理
# 打包成 exe 后 __file__ 指向临时目录，所以要区分两种情况：
#   BASE = 用户数据目录（sites.yaml、session、db 都放这）
#   RES  = 程序资源目录（html 文件放这）
# ============================================================
if getattr(sys, 'frozen', False):
    BASE = os.path.dirname(sys.executable)
    RES = getattr(sys, '_MEIPASS', BASE)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
    RES = BASE

# 所有自动生成的数据（db、session、缓存）放到 data/ 子目录
DATA_DIR = os.path.join(BASE, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB = os.path.join(DATA_DIR, "homework.db")

# ============================================================
# 常量
# ============================================================
ALERT_W = 360           # 浮窗宽度
ALERT_MARGIN = 20       # 浮窗距屏幕边缘的像素
GAP = 8                 # 红窗和白窗之间的间隔

AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"  # 注册表自启动位置
AUTOSTART_NAME = "HomeworkTasks"                                    # 注册表项名


# ============================================================
# 全局状态（跨函数共享）
# ============================================================
_main_window = None         # 主窗口对象
_alert_window = None        # 红色浮窗
_panel_window = None        # 白色浮窗
_screen_w = 1920            # 屏幕宽度，layout_all 里用
_items_cache = []           # 内存中的任务列表（避免重复读数据库）
_cache_lock = threading.Lock()  # 保护 _items_cache 的线程锁

_alert_ready = False        # 红窗 JS 是否加载完
_panel_ready = False        # 白窗 JS 是否加载完
_main_visible = True        # 主窗口是否可见


# ============================================================
# 数据库
# ============================================================
def get_conn():
    """打开数据库连接，如果表不存在就建表，返回连接对象。
    每次调用都新建连接（SQLite 连接不能跨线程）。"""
    conn = sqlite3.connect(DB)
    # done 表：记录哪些任务已完成
    conn.execute("""
        CREATE TABLE IF NOT EXISTS done (
            uid TEXT PRIMARY KEY,
            done_at TEXT
        )
    """)
    # manual_tasks 表：用户手动添加的任务
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


# ============================================================
# 配置初始化
# ============================================================
def ensure_sites_yaml():
    """首次启动时，把打包进 exe 的 sites.yaml.template
    复制成 sites.yaml（用户配置文件）。"""
    target = os.path.join(BASE, "sites.yaml")
    if os.path.exists(target):
        return  # 已存在就不动
    template = os.path.join(RES, "sites.yaml.template")
    if os.path.exists(template):
        shutil.copyfile(template, target)
        print(f"[init] 已生成 {target}")


# ============================================================
# 时间解析
# ============================================================
def _parse_time(s):
    """把各种格式的时间字符串解析成 datetime，失败返回 None。
    支持：2026-10-09 23:59:59 / 2026-10-09 23:59 / 带 T 的 ISO 格式。"""
    if not s:
        return None
    # 归一化：T 换成空格，/ 换成 -
    s = s.strip().replace("T", " ").replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


# ============================================================
# 任务分类
# ============================================================
def compute_urgent(items):
    """筛出"紧急"任务：未完成 + 距截止 ≤ 24 小时（含已过期）。
    返回列表，按剩余时间升序。红窗用。"""
    now = datetime.now()
    result = []
    for it in items:
        if it.get("done"):
            continue                       # 已完成，跳过
        end = it.get("end")
        if not end:
            continue                       # 没有截止时间，跳过
        due = _parse_time(end)
        if due is None:
            continue                       # 时间格式解析失败，跳过
        h = (due - now).total_seconds() / 3600   # 剩余小时数（可能为负）
        if h <= 24:                        # 24 小时内或已过期
            result.append({
                "uid": it["uid"],
                "title": it["title"],
                "hours_left": h,
            })
    result.sort(key=lambda x: x["hours_left"])
    return result


def compute_normal(items, urgent):
    """筛出"普通"未完成任务（不紧急的），白窗用。
    urgent 是已算好的紧急任务列表，用来排除。"""
    now = datetime.now()
    urgent_uids = {u["uid"] for u in urgent}
    result = []
    for it in items:
        if it.get("done"):
            continue
        if it["uid"] in urgent_uids:
            continue                       # 已在紧急列表里，跳过
        end = it.get("end")
        h = None
        if end:
            due = _parse_time(end)
            if due is not None:
                h = (due - now).total_seconds() / 3600
        result.append({
            "uid": it["uid"],
            "title": it["title"],
            "end": end or "",
            "hours_left": h,
        })
    result.sort(key=lambda x: x["end"] or "9999")
    return result


# ============================================================
# 浮窗布局
# ============================================================
def _calc_height(n, header=42, row=22, max_rows=8, max_h=260):
    """根据任务数量计算浮窗应该多高。
    n         任务数
    header    标题栏高度
    row       每条任务高度
    max_rows  最多显示几条
    max_h     窗口最大高度
    """
    if n <= 0:
        return 60
    rows = min(n, max_rows)
    h = header + rows * row + 16   # +16 是底部留白
    if n > max_rows:
        h += 18                     # "还有 N 条"那一行
    return max(70, min(h, max_h))


def _sync_main():
    """把内存里的任务列表推给主窗口，让它同步刷新（不重新抓取）。
    勾选/添加/删除任务后调用。"""
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
    """重新布局红窗和白窗。
    - 红窗：显示紧急任务；没有就隐藏
    - 白窗：显示普通任务；没有就隐藏
    - 白窗排在红窗下方
    """
    if not _alert_window or not _panel_window:
        return

    # 从内存缓存取一份数据
    with _cache_lock:
        items = list(_items_cache)

    urgent = compute_urgent(items)
    normal = compute_normal(items, urgent)

    W = _screen_w
    y = ALERT_MARGIN   # 从屏幕顶端往下留边距

    # ========== 红窗 ==========
    if urgent:
        w = ALERT_W
        h = _calc_height(len(urgent), header=42, row=20, max_rows=8, max_h=260)
        x = W - ALERT_W - ALERT_MARGIN

        try:
            _alert_window.resize(w, h)
            _alert_window.move(x, y)
            _alert_window.show()
        except Exception:
            pass

        if _alert_ready:
            js = (f'window.__updateUrgent && '
                  f'window.__updateUrgent({json.dumps(urgent, ensure_ascii=False)})')
            try:
                _alert_window.evaluate_js(js)
            except Exception as e:
                print(f"[alert] {e}")

        y += h + GAP   # 白窗从红窗下面开始
    else:
        try:
            _alert_window.hide()
        except Exception:
            pass

    # ========== 白窗 ==========
    if normal:
        w = ALERT_W
        h = _calc_height(len(normal), header=46, row=22, max_rows=12, max_h=360)
        x = W - ALERT_W - ALERT_MARGIN

        try:
            _panel_window.resize(w, h)
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


# ============================================================
# 开机自启动（写注册表）
# ============================================================
def _get_start_command():
    """返回开机自启动要执行的命令行。
    打包后 → 指向 exe；开发模式 → 指向 start.bat。"""
    if getattr(sys, 'frozen', False):
        return f'"{sys.executable}"'
    else:
        bat = os.path.join(BASE, "start.bat")
        return f'"{bat}"'


def get_autostart_status():
    """查注册表，看自启动是否已开启。"""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_READ)
        try:
            val, _ = winreg.QueryValueEx(key, AUTOSTART_NAME)
            winreg.CloseKey(key)
            return {"enabled": bool(val), "command": val}
        except FileNotFoundError:
            # 注册表里没这一项 = 没开启
            winreg.CloseKey(key)
            return {"enabled": False}
    except Exception as e:
        return {"enabled": False, "error": str(e)}


def set_autostart_status(enabled):
    """开启或关闭开机自启动（往注册表写/删）。"""
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
# Api 类：前端 JS 可调用的所有方法
# JS 用法：window.pywebview.api.方法名(参数...)
# ============================================================
class Api:
    # ---------- 浮窗就绪通知 ----------
    def alert_ready(self):
        """红窗 JS 加载完时调用。之后 Python 才能安全推送数据。"""
        global _alert_ready
        _alert_ready = True
        layout_all()
        return {"ok": True}

    def panel_ready(self):
        """白窗 JS 加载完时调用。"""
        global _panel_ready
        _panel_ready = True
        layout_all()
        return {"ok": True}

    # ---------- 主窗口显示/隐藏 ----------
    def show_main(self):
        """显示主窗口（从浮窗点"打开"时调）。"""
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
        """隐藏主窗口（从浮窗点"关闭"时调）。"""
        global _main_visible
        if _main_window:
            try:
                _main_window.hide()
            except Exception:
                pass
        _main_visible = False
        return {"ok": True}

    def is_main_visible(self):
        """查询主窗口是否可见（浮窗每秒查一次，用于切换按钮文字）。"""
        return {"visible": _main_visible}

    # ---------- 任务数据 ----------
    def get_homeworks(self):
        """刷新任务：重新抓取所有网站，返回完整列表。
        返回 {"items": [...], "errors": [...], "need_login": [...]}"""
        global _items_cache

        # 1. 抓各平台的作业
        try:
            homeworks, errors, need_login = fetch_all()
        except Exception as e:
            homeworks = []
            errors = [{"site_name": "抓取", "message": f"{type(e).__name__}: {e}"}]
            need_login = []

        # 2. 从数据库读完成状态和手动任务
        conn = get_conn()
        try:
            done_rows = dict(conn.execute("SELECT uid, done_at FROM done").fetchall())
            manual_rows = conn.execute(
                "SELECT uid, title, course, due, url FROM manual_tasks"
            ).fetchall()
        finally:
            conn.close()

        # 3. 合并成统一格式
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

        # 4. 更新内存缓存
        with _cache_lock:
            _items_cache = items

        layout_all()
        return {"items": items, "errors": errors, "need_login": need_login}

    def toggle_done(self, uid, done):
        """勾选/取消勾选一个任务。"""
        # 1. 更新数据库
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

        # 2. 更新内存缓存
        with _cache_lock:
            for it in _items_cache:
                if it["uid"] == uid:
                    it["done"] = done
                    break

        # 3. 刷新浮窗 + 主窗口
        layout_all()
        _sync_main()
        return {"ok": True}

    # ---------- 登录 ----------
    def login(self, site_id):
        """触发某站点的登录流程。
        Tyche 需要 Basic Auth，如果没填账号密码就拒绝。"""
        from scraper import load_sites
        for s in load_sites(include_disabled=True):
            if s["id"] == site_id and s.get("type") == "tyche":
                if not (s.get("basic_user") and s.get("basic_pass")):
                    return {"ok": False, "error": "need_credentials"}
        ok = login_site(site_id)
        return {"ok": ok}

    # ---------- 手动任务 ----------
    def add_task(self, title, course, due, url):
        """手动添加一条任务。"""
        global _items_cache
        if not title or not title.strip():
            return {"error": "标题不能为空"}
        if not due or not due.strip():
            return {"error": "截止时间不能为空"}

        uid = "manual:" + uuid.uuid4().hex[:12]   # 生成唯一 ID
        title = title.strip()
        course = (course or "").strip()
        due = due.strip()
        url = (url or "").strip()

        # 1. 写数据库
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO manual_tasks VALUES (?, ?, ?, ?, ?, ?)",
                (uid, title, course, due, url, datetime.now().isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

        # 2. 加进内存缓存
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
        """删除手动任务（自动抓取的不能删）。"""
        global _items_cache
        if not uid.startswith("manual:"):
            return {"error": "只能删除手动任务"}
        # 1. 从数据库删
        conn = get_conn()
        try:
            conn.execute("DELETE FROM manual_tasks WHERE uid=?", (uid,))
            conn.execute("DELETE FROM done WHERE uid=?", (uid,))
            conn.commit()
        finally:
            conn.close()
        # 2. 从内存缓存删
        with _cache_lock:
            _items_cache = [x for x in _items_cache if x["uid"] != uid]
        layout_all()
        _sync_main()
        return {"ok": True}

    # ---------- 设置面板：站点管理 ----------
    def list_sites(self):
        """返回所有站点的信息（给设置面板显示）。"""
        from scraper import load_sites, DATA_DIR as _DATA
        sites = load_sites(include_disabled=True)
        result = []
        for s in sites:
            sess = s.get("session")
            has_session = False
            if sess:
                has_session = os.path.exists(os.path.join(_DATA, sess))
            # Tyche 需要 Basic Auth 账号密码
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
        """读取某站点的 Basic Auth 账号密码（Tyche 用）。"""
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
        """保存 Basic Auth 账号密码到 sites.yaml。"""
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
        """批量应用设置面板的改动。
        changes 是 [{id, enabled, deleted, clear_session}, ...]"""
        from scraper import load_sites, save_sites, DATA_DIR as _DATA
        sites = load_sites(include_disabled=True)
        by_id = {s["id"]: s for s in sites}
        to_delete = set()

        def _wipe_session(sid):
            """删除某站点的 session 文件和课程缓存。"""
            s = by_id.get(sid)
            if not s:
                return
            sess = s.get("session")
            if sess:
                p = os.path.join(_DATA, sess)
                if os.path.exists(p):
                    os.remove(p)
            cache = os.path.join(_DATA, f"{sid}_courses.json")
            if os.path.exists(cache):
                os.remove(cache)

        # 第一轮：处理"清除登录"和"删除"
        for ch in changes:
            sid = ch.get("id")
            if sid not in by_id:
                continue
            if ch.get("clear_session"):
                _wipe_session(sid)
            if ch.get("deleted"):
                to_delete.add(sid)
                _wipe_session(sid)

        # 第二轮：处理"启用/禁用"
        for ch in changes:
            sid = ch.get("id")
            if sid in to_delete or sid not in by_id:
                continue
            by_id[sid]["enabled"] = bool(ch.get("enabled", True))

        # 保存
        new_sites = [s for s in sites if s["id"] not in to_delete]
        save_sites(new_sites)
        return {"ok": True}

    # ---------- 开机自启动 ----------
    def get_autostart(self):
        return get_autostart_status()

    def set_autostart(self, enabled):
        return set_autostart_status(bool(enabled))


# ============================================================
# 后台线程
# ============================================================
def background_ticker():
    """每 60 秒重新布局一次浮窗，让倒计时数字保持最新，
    也让跨过 24 小时阈值的任务自动从白窗挪到红窗。"""
    while True:
        time.sleep(60)
        try:
            layout_all()
        except Exception as e:
            print(f"[bg] {e}")


def _on_main_closing():
    """主窗口点 X 时触发：隐藏而不是关闭（后台继续跑浮窗）。
    返回 False 阻止真正的关闭。"""
    global _main_visible
    _main_visible = False
    try:
        _main_window.hide()
    except Exception:
        pass
    return False


# ============================================================
# 主函数
# ============================================================
def main():
    global _main_window, _alert_window, _panel_window, _screen_w

    # 首次启动：生成 sites.yaml
    ensure_sites_yaml()

    api = Api()

    # 三个窗口对应的 HTML 文件
    index_path = os.path.join(RES, "web", "index.html")
    alert_path = os.path.join(RES, "web", "alert.html")
    panel_path = os.path.join(RES, "web", "panel.html")

    # ---- 创建三个窗口 ----
    # 主窗口
    _main_window = webview.create_window(
        "任务清单", index_path, js_api=api,
        width=1000, height=720,
    )
    _main_window.events.closing += _on_main_closing

    # 红色浮窗：无边框、置顶、初始隐藏
    _alert_window = webview.create_window(
        "警告", alert_path, js_api=api,
        frameless=True, on_top=True, hidden=True,
        width=ALERT_W, height=140,
        transparent=True,
    )
    _panel_window = webview.create_window(
        "面板", panel_path, js_api=api,
        frameless=True, on_top=True, hidden=True,
        width=ALERT_W, height=200,
        transparent=True,
    )

    # 启动后台倒计时线程
    threading.Thread(target=background_ticker, daemon=True).start()

    def _after_start():
        """GUI 启动后的初始化流程。
        必须写在 webview.start() 里，因为 webview.screens 只有
        在 GUI 启动后才有效。"""
        global _screen_w
        # 拿屏幕尺寸
        try:
            s = webview.screens[0]
            W, H = s.width, s.height
        except Exception:
            W, H = 1920, 1080

        _screen_w = W

        # 主窗口放右下角
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

        # 让浮窗先渲染
        time.sleep(0.3)
        layout_all()

    webview.start(_after_start)


if __name__ == "__main__":
    main()