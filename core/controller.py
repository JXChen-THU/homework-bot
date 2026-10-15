# ============================================================
# 任务清单 - 主程序
# ============================================================

import os
import sys
import json
import subprocess
import threading
import time
import uuid
import shutil
from datetime import datetime
import webview

from core.database import get_conn
from core.task_logic import compute_urgent, compute_normal
from core import paths

from core.site_runner import fetch_all, login_site, set_progress_sink


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
# 屏幕尺寸用**单个元组**保存：GUI 回调线程会写、worker/js_api 线程会读，
# 两个独立全局变量会被读到"新宽 + 旧高"（元组解包/赋值本身是原子的）。
_screen_size = (1920, 1080)
_items_cache = []
_cache_lock = threading.Lock()

_alert_ready = False
_panel_ready = False
_main_ready = False          # 主窗口前端已就绪，可以安全 evaluate_js
_main_visible = False
_first_load_done = False
_startup_shown = False       # 启动期的首轮显示已经做过（直接显示档用）
_quit_flag = False

# 启动期需要让用户看到的问题（数据目录回退、明文升级失败等）。
# 窗口化 exe 下 print 会被丢弃，这类问题必须有别的出口。
_startup_warnings = []

# 启动行为：silent 启动时窗口全程不出现；visible 启动即显示界面。
# 真正的值从 sites.yaml 读（老配置没有这个键 → 用默认值）。
_startup_mode = "silent"

# 抓取进度：同一时刻只允许一次抓取，避免两轮进度行交叉串台。
_fetch_lock = threading.Lock()

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

    失败必须让用户看见：窗口化 exe 下 print 会被丢弃，而"以为是密文、
    其实磁盘上还是明文"是安全承诺（README）里最不该静默的一条。
    """
    try:
        from core.site_runner import upgrade_plaintext_credentials, upgrade_error
        upgrade_plaintext_credentials()
        err = upgrade_error()
        if err:
            _startup_warnings.append({
                "kind": "err",
                "text": "账号密码加密升级失败，磁盘上可能仍是明文。",
                "detail": str(err),
            })
    except Exception as e:
        _startup_warnings.append({
            "kind": "err",
            "text": "账号密码加密升级未能执行。",
            "detail": f"{type(e).__name__}: {e}",
        })


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
def _calc_height(n, header=44, row=19.19, max_rows=4, pad=0, min_h=0):
    """浮窗高度 = 标题栏 + 可见行数 × 行距 + 列表内边距。

    ★ 这几个数是**实测**出来的，不是估的（DPR=1，Edge/WebView2）：
        .header 高 44px
        .item  高 16.1875px + margin-bottom 3px = 行距 19.19px
        列表内边距：待办窗 0/0、紧急窗 4px/5px
      改 CSS 里这三处任何一处，都必须同步改这里（浮窗行距是硬约束）。

    ★ 历史值为什么是错的：原来是 header=42/46、row=20/22、外加 "+16"，
      再在"超过上限"时 "+18"（给"...还有 N 条"留位置）。
      但 .header 实际是 44、行距实际是 19.19，所以每一行都多算了 2.8px，
      高度一直偏大；而 "+18" 现在也不需要了 —— 条数不再截断，
      超出的部分在窗内滚动（见 #list 的 overflow-y:auto）。

    max_rows 就是"可见行数上限"：紧急窗 4、待办窗 6。任务更多时窗口**不再长高**，
    多出来的行靠滚动看。max_h 仍留作最后一道保险。
    """
    if n <= 0:
        return 60
    rows = min(n, max_rows)
    h = header + rows * row + pad
    return max(min_h, h)


# 两个浮窗的实测参数（与 panel.css / alert.css 配对，改一处就要改另一处）
ALERT_HEADER_H = 44.0
ALERT_LIST_PAD = 9.0    # 紧急窗 #list padding 4px + 5px
ALERT_ROW_H = 19.19     # .item 16.1875 + margin-bottom 3
ALERT_MAX_ROWS = 4      # 最多显示 4 行，再多的滚动
PANEL_HEADER_H = 44.0
PANEL_LIST_PAD = 9.0    # 待办窗 #list padding 4px + 5px（与紧急窗同做法）
PANEL_ROW_H = 19.19
PANEL_MAX_ROWS = 6      # 最多显示 6 行，再多的滚动


def _sync_main():
    """把缓存里的任务同步给主窗口前端。

    必须先看 `_main_ready`：pywebview 的 `evaluate_js` 带 `@_pywebview_ready_call`，
    未就绪时会先阻塞等 20 秒（见 _push_progress 的同一约定）。
    这条路径会被**浮窗勾选**触发（toggle_done → _sync_main），
    主窗口前端若还没上报就绪，浮窗点一下就会白等 20 秒。
    """
    if not _main_ready or not _main_window:
        return
    with _cache_lock:
        items = list(_items_cache)
    try:
        js = f'window.__syncFromCache && window.__syncFromCache({json.dumps(items, ensure_ascii=False)})'
        _main_window.evaluate_js(js)
    except Exception:
        pass


def _main_geometry(W: int, H: int):
    """主窗口的目标几何 (宽, 高, x, y)。

    ★ 单一来源：建窗时的 x= / y= / width= / height= 与 show_main_window() 里的
    resize/move 必须用**同一个算法**。否则建窗时放好了、之后又被挪到别处，
    等于换了个位置再闪一次。
    """
    MW, MH = 1000, 720
    if W < MW + 40:
        MW = max(200, W - 40)
    if H < MH + 80:
        MH = max(200, H - 80)
    return MW, MH, max(0, W - MW - 20), max(0, H - MH - 60)


def show_main_window():
    global _main_visible
    if not _main_window:
        return
    try:
        # ★ 屏幕尺寸优先用启动时那次探测的结果（_primary_screen_size 在
        #   webview.start() 之前就能拿到，且与 pywebview 的 Screen 同单位）。
        #   拿不到才退回 webview.screens / 默认值。
        W, H = _screen_size
        if W <= 0 or H <= 0:
            try:
                first = next(iter(webview.screens))
                W, H = int(first.width), int(first.height)
            except Exception:
                W, H = 1920, 1080

        MW, MH, MX, MY = _main_geometry(W, H)

        # 先 restore 从任务栏拉出来，再 resize + move 到右下角，最后 show。
        # （建窗时已经用同一套几何摆好过，所以这里的 resize/move 通常是空操作，
        #   不会造成"先出现在别处再跳过来"。）
        _main_window.restore()
        _main_window.resize(MW, MH)
        _main_window.move(MX, MY)
        _main_window.show()
    except Exception:
        pass
    _main_visible = True


def _inject_urgent(urgent):
    """把紧急任务推给红窗前端。"""
    if not _alert_ready:
        return
    js = (f'window.__updateUrgent && '
          f'window.__updateUrgent({json.dumps(urgent, ensure_ascii=False)})')
    try:
        _alert_window.evaluate_js(js)
    except Exception as e:
        print(f"[alert] {e}")


def _inject_normal(normal):
    """把普通任务推给白窗前端。"""
    if not _panel_ready:
        return
    js = (f'window.__updateNormal && window.__updateNormal('
          f'{json.dumps(normal, ensure_ascii=False)})')
    try:
        _panel_window.evaluate_js(js)
    except Exception as e:
        print(f"[panel] {e}")


def _on_float_restored(is_alert):
    """浮窗被从任务栏拉回来 → 清掉最小化标志并重排。

    没有这个回调，最小化标志会一直为真：数据注入被跳过、show() 也被跳过，
    窗口从此停在旧内容上，只能回主窗口点「重置浮窗」。
    """
    global _alert_minimized, _panel_minimized
    if is_alert:
        _alert_minimized = False
    else:
        _panel_minimized = False
    layout_all()


# ============================================================
# 抓取进度播报（推到主窗口的进度文字区）
#
# 为什么走 evaluate_js 而不是放进 get_homeworks 的返回值：
# 那个 promise 要等抓完才 resolve，用户在整个抓取过程中看不到任何东西。
#
# 为什么必须先看 _main_ready：
# pywebview 的 evaluate_js 带 @_shown_call，未就绪时会先阻塞等 20 秒再抛异常。
# 抓取期间会被调用很多次，不能白白卡住 worker 线程。
# ============================================================
def _push_progress(line):
    if not _main_ready or _main_window is None:
        return
    try:
        js = ('window.__appendLog && window.__appendLog('
              + json.dumps(line, ensure_ascii=False) + ')')
        _main_window.evaluate_js(js)
    except Exception as e:
        print(f"[progress] 推送失败：{type(e).__name__}: {e}")


def _main_frontend_ready():
    """主窗口前端已注册好全局函数 → 允许 evaluate_js。

    由 main.js 在 pywebviewready 时调用。没有这个信号，
    抓取期间推进度会先白等 20 秒（pywebview 的 _shown_call）。
    """
    global _main_ready
    _main_ready = True


def get_startup_mode():
    return _startup_mode


def set_startup_mode(mode):
    """切换启动行为并落盘。返回 {"ok": True} 或 {"error": ...}。"""
    global _startup_mode
    from core.site_runner import save_startup_mode

    if mode not in ("silent", "visible"):
        return {"error": f"未知的启动行为：{mode}"}
    ok, err = save_startup_mode(mode)
    if not ok:
        # 写盘失败就不要改内存状态，否则界面显示的和下次启动的会不一致
        return {"error": f"保存到 sites.yaml 失败：{err}"}
    _startup_mode = mode
    return {"ok": True, "mode": mode}


def layout_all():
    """重新布局红窗和白窗。
    - 用户最小化或关闭的浮窗不再自动弹出
    """
    if not _alert_window or not _panel_window:
        return

    with _cache_lock:
        items = list(_items_cache)

    urgent = compute_urgent(items)
    normal = compute_normal(items, urgent)

    W = _screen_size[0]
    y_default = ALERT_MARGIN

    # ========== 红窗 ==========
    if urgent and not _alert_dismissed:
        w = ALERT_W
        h = int(round(_calc_height(len(urgent), header=ALERT_HEADER_H, row=ALERT_ROW_H,
                                   max_rows=ALERT_MAX_ROWS, pad=ALERT_LIST_PAD)))

        if not _alert_minimized:
            try:
                # 先摆位再显示：顺序反了会先在系统默认位置闪一帧
                place_float(_alert_window, w, h, W - ALERT_W - ALERT_MARGIN, y_default)
                _alert_window.show()
            except Exception:
                pass

        alert_bottom = y_default + h + GAP
    else:
        try:
            _alert_window.hide()
        except Exception:
            pass
        alert_bottom = y_default

    # 注入放在最小化判断之外：窗口被最小化时「显示/定位」应当跳过，
    # 但数据必须照常推到前端。否则用户把红窗从任务栏拉回来后，
    # 里面还是最小化之前那一批任务，且再也刷不新。
    _inject_urgent(urgent)

    # ========== 白窗 ==========
    if normal and not _panel_dismissed:
        w = ALERT_W
        h = int(round(_calc_height(len(normal), header=PANEL_HEADER_H, row=PANEL_ROW_H,
                                   max_rows=PANEL_MAX_ROWS, pad=PANEL_LIST_PAD)))

        if not _panel_minimized:
            try:
                # 先摆位再显示（理由同红窗）
                y = alert_bottom if (urgent and not _alert_dismissed) else y_default
                place_float(_panel_window, w, h, W - ALERT_W - ALERT_MARGIN, y)
                _panel_window.show()
            except Exception:
                pass
    else:
        try:
            _panel_window.hide()
        except Exception:
            pass

    # 同红窗：数据注入不随「最小化」一起跳过
    _inject_normal(normal)


# ============================================================
# 浮窗定位
# ============================================================
# ★ 为什么要有"先摆位再 show"这回事（修"开机时先闪一下左侧再跳到右下"）：
#   浮窗用 hidden=True 创建，但 Windows 上 pywebview 的 hidden 是
#   "Opacity=0 → Show() → Hide()"，也就是说窗口**真的被显示过一次**。
#   如果此时还没 move()，它会先按系统默认位置画一帧。
#
# ★ 但只调顺序还不够！winforms 后端建窗时是这样定位置的：
#       if window.initial_x is not None and window.initial_y is not None:
#           StartPosition = Manual;  Location = (initial_x, initial_y)
#       else:
#           StartPosition = CenterScreen
#   即"create_window 不传 x/y"的窗口，**第一次显示会被系统摆到屏幕中央**。
#   无论 move() 排得多早，那一帧已经发生在建窗时了 —— 所以必须在建窗时
#   就把坐标给进去（见 main() 里 create_window 的 x= / y=）。
#
# ★ 为什么不自己缓存位置（第一版就是这么写的，结果把「重置浮窗」弄坏了）：
#   缓存里留的是"上次设的目标位置"，而用户可能**手动拖走**过窗口；再点「重置浮窗」
#   时目标与缓存相同 → 判定为"没变" → 不 move，窗口就永远回不到右上角。
#   现在改成读 Window.x / Window.y 拿**真实位置**再比较。这两个属性内部是
#   gui.get_position()，后端已经做了 physical → logical 的换算（/scale），
#   与 move() 期望的 logical 单位一致，可以直接比。
#
# 纯逻辑在 core/float_place.py 里，单独成模块是为了能被直接测试
# （原先的自动验证脚本已随 design-verify/ 一起移除，改这段请人工核对定位行为）。
from core.float_place import place_float


def _primary_screen_size(fallback_w: int = 1920, fallback_h: int = 1080):
    """拿主显示器的**逻辑**尺寸 (宽, 高)（与 pywebview 的 Screen 同单位）。

    必须在 webview.start() 之前也能用 —— 建窗时就要确定主窗口与浮窗的坐标。
    webview.screens 是模块属性，内部 `if not guilib: guilib = initialize()`
    已做防重入，且只 import 平台模块、不建窗口，所以提前调用是安全的。
    """
    try:
        # 用 next(iter(...)) 而不是 [0]：create_window 的参数里也有个 screens，
        # 显式取首个元素可以完全避开同名歧义。
        first = next(iter(webview.screens))
        w, h = int(first.width), int(first.height)
        if w > 0 and h > 0:
            return w, h
    except Exception:
        pass
    # 兜底：直接问系统。
    # ★ 顺序很关键：**必须先 SetProcessDPIAware()，再 GetSystemMetrics()**。
    #   先读的话拿到的是被 DPI 缩放过的值（实测这台机器 2560 的屏读成 1707），
    #   窗口会被摆错位置，反而制造出用户看到的那种"闪一下"。
    #   SetProcessDPIAware 在 Win10+ 是幂等的，pywebview 自己也会调，不冲突。
    try:
        import ctypes
        u = ctypes.windll.user32
        w_phys = h_phys = 0
        dpi = 96
        try:
            u.SetProcessDPIAware()
            w_phys = int(u.GetSystemMetrics(0))       # 感知 DPI 后 = 物理像素
            h_phys = int(u.GetSystemMetrics(1))
            dpi = int(u.GetDpiForSystem())            # Win10 1607+
        except Exception:
            dpi = 96
        if w_phys <= 0:
            w_phys = int(u.GetSystemMetrics(0))
            h_phys = int(u.GetSystemMetrics(1))
        if w_phys > 0 and h_phys > 0:
            # pywebview 的坐标是"逻辑像素"：物理尺寸 ÷ DPI 缩放 = 逻辑尺寸
            sc = (dpi or 96) / 96.0
            return max(1, int(round(w_phys / sc))), max(1, int(round(h_phys / sc)))
    except Exception:
        pass
    return fallback_w, fallback_h


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
# 任务数据：缓存/DB 合并
# ============================================================
def _merge_cached(cached, done_rows, manual_rows):
    """把一批站点任务与 DB 里的手动任务合并成一份 items。幂等。

    启动闸门短路与正常路径共用这一份逻辑。三个约束都是踩过的坑：

    1. **必须把结果写回 `_items_cache`**（见调用处）：`_sync_main()` 与
       `layout_all()` 都以缓存为唯一来源，缓存和返回值一旦不一致，
       用户勾一下复选框就会看到列表被刷空。
    2. **必须幂等**：缓存里可能已经有一份同 uid 的手动任务（本次会话里
       `add_task` 加进去的），再从 DB 追加一遍就会变成两行，其中一行
       没有删除按钮。
    3. **`manual` 标记要保留**：缓存里 `add_task` 写入的那份本来带
       `manual: True`（前端据此显示删除按钮），硬写 False 会让它变成
       "站点任务"。正常路径传进来的 `fetched` 没有这个键 → 仍是 False。
    """
    items = []
    seen = set()
    for it in cached or []:
        d = dict(it)
        uid = d.get("uid")
        if uid:
            if uid in seen:
                continue
            seen.add(uid)
        d["done"] = uid in done_rows
        d["manual"] = bool(d.get("manual"))
        items.append(d)

    for uid, title, course, due, url in manual_rows:
        # 缓存里已经有同一条（本次会话里刚 add_task 过）→ 不再追加
        if uid in seen:
            continue
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
    return items


# ============================================================
# Api 类
# ============================================================
class Api:
    def alert_ready(self):
        """浮窗前端已就绪。

        顺序有讲究：**先**尝试注入布局，**再**置就绪标志。
        反过来的话，layout_all() 抛异常会让这一次调用变成 rejected promise，
        前端 `pywebviewready` 回调里后面的 refreshMainVisible()/setInterval 都不执行，
        而标志已经是 True —— 浮窗从此不再自动刷新，也没有任何重试入口。
        """
        global _alert_ready
        try:
            layout_all()
        except Exception as e:
            print(f"[float] 首帧布局失败（下次刷新会重试）：{type(e).__name__}: {e}")
        _alert_ready = True
        return {"ok": True}

    def panel_ready(self):
        """白窗前端已就绪。理由同 alert_ready。"""
        global _panel_ready
        try:
            layout_all()
        except Exception as e:
            print(f"[float] 首帧布局失败（下次刷新会重试）：{type(e).__name__}: {e}")
        _panel_ready = True
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

        _alert_dismissed = False
        _panel_dismissed = False
        _alert_minimized = False
        _panel_minimized = False

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
        global _items_cache, _first_load_done, _startup_shown

        # 直接显示档：抓取**之前**就把窗口显示出来。
        # 否则等抓完才显示，用户根本看不到进度文字区滚动 —— 需求 B 的核心
        # 就是让用户看着它滚。
        if _startup_mode == "visible" and not _startup_shown:
            _startup_shown = True
            show_main_window()

        # ---------- 启动闸门：一个会话文件都没有的机器不要白等一轮抓取 ----------
        #
        # 判定与理由见 core.site_runner.startup_gate()。这里只做三件事：
        #   1) 跳过这轮抓取（否则 wait_for_network 最多 120 秒 + 每个站点
        #      拉起一个 Playwright 等登录页超时）；
        #   2) 把窗口显示出来（静默档也要显示：用户此时还没有会话文件，
        #      他必须去填账号密码，不显示窗口就完全看不到入口）；
        #   3) 回一份 {"gate": ...} 让前端给出"去填账号密码"的引导。
        #
        # 注意：**不清缓存**。用户可能已经手动加过任务，那批数据还在缓存里，
        # 不能被这次短路清掉；返回前还要把合并结果写回缓存（见下）。
        try:
            from core.site_runner import startup_gate
            gate = startup_gate()
        except Exception as e:
            print(f"[startup] 闸门判定失败，按正常流程抓取：{type(e).__name__}: {e}")
            gate = {"skip": False, "sites": []}

        if gate.get("skip"):
            gate_sites = gate.get("sites") or []
            # 标记首轮已结束：_startup_show_fallback 那条兜底线程本来是为
            # "太久没拿到数据就把窗口显示出来"准备的，而这里窗口已经显示、
            # 而且这一轮压根不会去抓。不标记的话它会白睡最多 300 秒。
            _first_load_done = True
            if not _startup_shown:
                # 可见档在闸门之前已经显示过一次（上面那段），不重复调
                _startup_shown = True
                try:
                    show_main_window()
                except Exception as e:
                    print(f"[startup] 显示主窗口失败：{type(e).__name__}: {e}")
            # 首轮网络等待留给**第一次真正的抓取**（用户填完账号密码之后）。
            # 现在把它标记为"等过了"，那 120 秒的等待就被这次短路吃掉了，
            # 用户填完账号后的第一次抓取反而可能在没网时直接失败。
            try:
                from core import site_runner
                with site_runner._network_lock:
                    site_runner._network_waited = False
            except Exception:
                pass

            with _cache_lock:
                cached = list(_items_cache)
            conn = get_conn(DB)
            try:
                done_rows = dict(conn.execute("SELECT uid, done_at FROM done").fetchall())
                manual_rows = conn.execute(
                    "SELECT uid, title, course, due, url FROM manual_tasks"
                ).fetchall()
            finally:
                conn.close()

            print(f"[startup] 没有会话文件也没有账号密码，本轮跳过抓取"
                  f"（{len(gate_sites)} 个站点待配置），直接显示窗口")
            items = _merge_cached(cached, done_rows, manual_rows)
            # ★ 必须写回缓存：_sync_main() / layout_all() 都只认 _items_cache，
            #   不写回的话用户一勾复选框就会收到一份**不含手动任务**的缓存，
            #   列表当场被刷空（DB 里其实还在），再刷新还会出现重复行。
            with _cache_lock:
                _items_cache = items
            return {
                "items": items,
                "errors": [],
                "need_login": gate_sites,
                "startup": paths.startup_messages() + list(_startup_warnings),
                "gate": {
                    "skipped": True,
                    "sites": gate_sites,
                    "message": (
                        "首次启动：尚未检测到登录状态，本次未执行抓取。"
                        "请在「设置 → 填写账号密码」中完成配置，随后将自动开始抓取。"
                    ),
                },
            }

        # 抓取放到锁里：连点刷新时两轮进度行会交叉串台。
        # 串行的代价是第二次刷新要等第一次结束 —— 比进度错乱可接受。
        with _fetch_lock:
            sink = _push_progress if _main_ready else None
            set_progress_sink(sink)
            try:
                try:
                    homeworks, errors, need_login = fetch_all()
                except Exception as e:
                    homeworks = []
                    errors = [{"site_name": "抓取",
                               "message": f"{type(e).__name__}: {e}"}]
                    need_login = []
            finally:
                set_progress_sink(None)

        conn = get_conn(DB)
        try:
            done_rows = dict(conn.execute("SELECT uid, done_at FROM done").fetchall())
            manual_rows = conn.execute(
                "SELECT uid, title, course, due, url FROM manual_tasks"
            ).fetchall()
        finally:
            conn.close()

        fetched = []
        seen_uids = set()
        for hw in homeworks:
            d = hw.to_dict()
            # 去重：适配器抓回来的同一个作业不能因为出现在两个面板里
            # 就在列表里出现两行（README 承诺的是"合并成一个面板"）
            if d.get("uid") in seen_uids:
                continue
            seen_uids.add(d.get("uid"))
            fetched.append(d)

        items = _merge_cached(fetched, done_rows, manual_rows)

        with _cache_lock:
            _items_cache = items

        layout_all()

        if not _first_load_done:
            _first_load_done = True
            # 静默档：首次抓取完也不弹窗（用户可从浮窗的「主面板」按钮打开）。
            # 直接显示档：上面已经在抓取前显示过了，这里不重复。
            if _startup_mode == "visible" and not _startup_shown:
                _startup_shown = True
                show_main_window()

        return {"items": items, "errors": errors, "need_login": need_login,
                "startup": paths.startup_messages() + list(_startup_warnings)}

    # ---------- 打开原网页 ----------
    def open_url(self, url):
        """用系统默认浏览器打开作业原网页。

        为什么不让前端用 `<a target="_blank">`：那是让 WebView2 自己去导航，
        题目站点普遍要求登录态或 X-Frame-Options，结果是弹出一个空白页或
        登录页，用户以为功能坏了。交给系统浏览器则能复用用户已登录的会话。
        """
        import webbrowser

        u = (url or "").strip()
        # 只放行 http(s)：链接可能来自 sites.yaml / 手动任务输入，
        # 不能让 file:// 或 javascript: 之类的伪协议落到系统 shell 上。
        if not u.lower().startswith(("http://", "https://")):
            return {"error": "仅支持 http/https 链接"}
        try:
            if not webbrowser.open(u):
                return {"error": "系统没有可用的浏览器"}
            return {"ok": True}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

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

        必须把 reason 带出去：前端为每种失败原因准备了不同话术
        （密码错 / 需要验证码 / 平台不支持 / 网络故障），
        以前只回 {"ok": ok}，用户只能看到"登录未完成，点击重试"，
        分不清是该去改密码还是该去过验证码，于是反复点同一个按钮、
        反复拉起真实 Edge 窗口。
        """
        ok, reason = login_site(site_id)
        return {"ok": ok, "reason": reason}

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
        from core.site_runner import load_sites, DATA_DIR as _DATA, supports_silent_login
        from adapters.auth import _plain, has_credentials, credentials_broken, session_path
        sites = load_sites(include_disabled=True)
        result = []
        for s in sites:
            # 会话文件名要与登录/抓取用同一套口径：配置里没写 session 时按 id 推断，
            # 否则这里永远显示"未登录"。
            has_session = False
            try:
                has_session = os.path.exists(session_path(_DATA, s))
            except Exception:
                pass
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
                # 能力位来自适配器模块（唯一出处），前端据此决定要不要显示
                # 「填账号密码」按钮，不再自己硬编码类型字面量。
                "supports_silent_login": supports_silent_login(stype),
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
        """保存站点账号密码（加密后写入 sites.yaml）。

        整段「读—改—写」放在 config_transaction() 里：pywebview 给每次 js_api
        调用起一个独立线程，而 apply_sites_changes/save_startup_mode 走的是
        同一条"整体覆盖 sites 数组"的路径；不加这把锁，两次调用重叠时后完成者
        会用旧快照把前一次改动整块抹掉，而两次都返回 {"ok": True}。
        """
        from core.site_runner import load_sites, save_sites, config_transaction, _relogin_clear
        from core.secrets import encrypt
        with config_transaction():
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
                    self._wipe_session_files(s["id"])
                    # 同时清掉自动重登的冷却与上次失败原因：这是最该清冷却的路径，
                    # 否则用户改对了密码，30 分钟内自动重登仍被挡，横幅继续写
                    # "账号或密码可能不对"。
                    try:
                        _relogin_clear(s["id"])
                    except Exception:
                        pass
                    return {"ok": True}
            return {"error": "站点不存在"}

    @staticmethod
    def _wipe_session_files(site_id):
        """删掉该站点的会话文件与课程缓存。

        会话文件名走统一口径（session_path）：以前按 `s.get("session")` 拼，
        配置里没写 session 时删的是 None/空路径，用户点「清除登录」什么也没删掉。
        """
        from core.site_runner import DATA_DIR as _DATA, get_site
        try:
            from adapters.auth import session_path
            site = get_site(site_id)
            if site:
                p = session_path(_DATA, site)
                if os.path.exists(p):
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
        from core.site_runner import load_sites, save_sites, config_transaction
        with config_transaction():
            sites = load_sites(include_disabled=True)
            by_id = {s["id"]: s for s in sites}
            to_delete = set()

            def _wipe_session(sid):
                self._wipe_session_files(sid)

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
            # 被删掉/被禁用的站点，自动重登的冷却与失败原因也一并清掉，
            # 否则同名站点重新加回来时会带着旧冷却。
            for sid in list(to_delete):
                try:
                    from core.site_runner import _relogin_clear
                    _relogin_clear(sid)
                except Exception:
                    pass
            return {"ok": True}

    # ---------- 开机自启动 ----------
    def get_autostart(self):
        return get_autostart_status()

    def set_autostart(self, enabled):
        return set_autostart_status(bool(enabled))

    # ---------- 前端就绪 + 启动行为 ----------
    def main_ready(self):
        """主窗口前端已注册好全局函数。由 main.js 在 pywebviewready 时调用。"""
        _main_frontend_ready()
        return {"ok": True}

    def get_startup_behavior(self):
        return {"mode": get_startup_mode()}

    def set_startup_behavior(self, mode):
        return set_startup_mode(mode)


# ============================================================
# 主窗口关闭：任何方式关闭 → 退出程序
# ============================================================
def _shutdown_cleanup():
    r"""退出前的收尾：等抓取线程一小会儿，然后把它拉起的浏览器进程一起结束。

    为什么必须做：抓取与静默登录跑在**工作线程**里，用的是 Playwright。
    用户在抓取过程中点 X，主窗口一关、进程就进入退出流程，而 Playwright 的驱动
    （`_internal\playwright\driver\node.exe`）和它拉起的 `msedge.exe` **不会自己死** ——
    任务管理器里就会出现「程序已经关了，却还有一堆 node.exe / msedge.exe 在跑」，
    而且它们的可执行文件路径正是本程序的目录，看起来就像「exe 还在跑」。
    """
    # 1) 抓取还在跑的话，给它一点时间自己收尾（Playwright 正常关闭是干净的）
    try:
        if _fetch_lock.acquire(timeout=2.0):
            _fetch_lock.release()
    except Exception:
        pass

    # 2) 兜底：把自己名下的 node.exe（Playwright 驱动）连整棵子树一起结束。
    #    只按「本进程的直接子进程」筛选，**绝不能**用 /IM node.exe 这种全局匹配 ——
    #    那会把用户自己的 Node 开发进程一起杀掉。
    try:
        ps = (r"""
$p = %d
Get-CimInstance Win32_Process -Filter "ParentProcessId=$p" |
  Where-Object { $_.Name -match '^(node|msedge|chrome)\.exe$' } |
  ForEach-Object { taskkill /F /T /PID $_.ProcessId 2>$null | Out-Null }
""" % os.getpid())
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       timeout=15, creationflags=0x08000000)   # CREATE_NO_WINDOW：别闪黑框
    except Exception as e:
        print(f"[exit] 清理浏览器子进程失败（不影响退出）：{type(e).__name__}: {e}")


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
    """兜底：启动后长时间没拿到数据，就把窗口显示出来。

    静默档下这里**不能弹窗** —— 用户选了「每次启动都不出现窗口」，
    到点自动弹出来直接违背他的选择。静默档只标记首轮结束，保持隐藏。
    """
    global _first_load_done, _startup_shown
    for _ in range(600):
        time.sleep(0.5)
        if _quit_flag or _first_load_done:
            return
    if not _first_load_done and not _quit_flag:
        _first_load_done = True
        if _startup_mode == "visible":
            _startup_shown = True
            show_main_window()


# ============================================================
# 主函数
# ============================================================
def main():
    global _main_window, _alert_window, _panel_window, _screen_size, _startup_mode

    # 1) 先解析数据目录。必须在任何 data/ 读写之前。
    ok, err = _setup_runtime_paths()
    if not ok:
        _fatal_dialog("任务清单 - 无法启动", err)
        sys.exit(1)

    # 2) 首次启动生成配置；把残留明文账密加密；读出启动行为
    try:
        ensure_sites_yaml()
        upgrade_credentials()
        from core.site_runner import load_startup_mode
        _startup_mode = load_startup_mode()
    except Exception as e:
        print(f"[init] 启动初始化失败：{type(e).__name__}: {e}")

    api = Api()

    index_path = os.path.join(RES, "web", "index.html")
    alert_path = os.path.join(RES, "web", "alert.html")
    panel_path = os.path.join(RES, "web", "panel.html")

    # 3) 建窗口与启动 GUI。目标机缺少 WebView2 Runtime 等情况下，
    #    异常会在 webview.start() 抛出——必须给出中文、可操作的提示，
    #    否则用户只看到"程序没反应"。
    #
    # ★ 建窗时就必须给出**每个**窗口的初始坐标（下面三个 create_window 的 x= / y=）：
    #   winforms 后端的逻辑是
    #       if window.initial_x is not None and window.initial_y is not None:
    #           StartPosition = Manual;  Location = (initial_x, initial_y)
    #       else:
    #           StartPosition = CenterScreen          ← 不传 x/y 就是这个
    #   也就是说"不给坐标"的窗口**第一次显示会被系统摆到屏幕中央**，
    #   之后才被 move() 挪到右下 —— 用户在真机上看到的"先闪一下"就是这么来的。
    #
    #   ★ 主窗口同样中招，而且更明显：它是 hidden=False 创建的（非静默档），
    #     所以**创建那一刻就显示在屏幕中央**了。show_main_window() 里的
    #     restore→resize→move→show 全都在那之后 —— 顺序怎么调都没用，
    #     那一帧已经发生了。只有建窗时给坐标才能消掉。
    _screen_size = _primary_screen_size(*_screen_size)
    _float_x = max(0, _screen_size[0] - ALERT_W - ALERT_MARGIN)
    # 与 show_main_window() 共用同一套算法，避免"建窗放好了又被挪到别处"
    _main_w, _main_h, _main_x, _main_y = _main_geometry(*_screen_size)

    try:
        # 静默档必须用 hidden=True，不能用 minimized=True：
        # minimized 只是设 WindowState，pywebview 仍会 browser.Show()，
        # 窗口真实显示一瞬再缩进任务栏 → 任务栏图标闪一下，谈不上"静默"。
        # hidden 走的是 Opacity=0 → Show() → Hide()，肉眼看不到。
        # 注意 hidden 仍会触发 WinForms 的 Shown 事件，所以
        # evaluate_js 的 @_shown_call 门禁能通过，之后照常推数据。
        _main_window = webview.create_window(
            "任务清单", index_path, js_api=api,
            width=_main_w, height=_main_h,
            # ★ 主窗口也要给坐标：它在非静默档是 hidden=False，创建即显示，
            #   不给坐标就会被 CenterScreen 摆在屏幕中央（就是那一闪的右侧位置）
            x=_main_x, y=_main_y,
            hidden=(_startup_mode == "silent"),
        )
        _main_window.events.closing += _on_main_closing
        _main_window.events.minimized += _on_main_minimized
        _main_window.events.restored += _on_main_restored

        # ★ 浮窗**不设 on_top**：设了就是"永远置顶"，用户新开的浏览器会被它压住，
        #   而正常窗口应该是"后打开的盖住先打开的"。
        #   去掉之后浮窗回到普通窗口层级：别人可以盖住它，点一下它又能到前面。
        #   （下面用 bring_to_front 在"主面板显示/浮窗出现"时把浮窗提到 C 之前，
        #     保证浮窗始终在主面板之上，但不越过用户后来打开的其它程序。）
        _alert_window = webview.create_window(
            "警告", alert_path, js_api=api,
            frameless=True, hidden=True,
            width=ALERT_W,
            # 初始高度就按"满 4 行"给，与 layout_all 用同一公式 —— 否则第一帧尺寸
            # 和之后的不一致，窗口会先大一下再缩（和位置闪烁同一类问题）
            height=int(round(_calc_height(ALERT_MAX_ROWS, header=ALERT_HEADER_H, row=ALERT_ROW_H,
                                          max_rows=ALERT_MAX_ROWS, pad=ALERT_LIST_PAD))),
            transparent=True,
            # ★ 初始位置必须在建窗时给出，否则 WinForms 用 CenterScreen，
            #   第一次显示会先出现在屏幕中央（见上面的长注释）
            x=_float_x, y=ALERT_MARGIN,
        )
        _panel_window = webview.create_window(
            "提醒", panel_path, js_api=api,
            frameless=True, hidden=True,
            width=ALERT_W,
            height=int(round(_calc_height(PANEL_MAX_ROWS, header=PANEL_HEADER_H, row=PANEL_ROW_H,
                                          max_rows=PANEL_MAX_ROWS, pad=PANEL_LIST_PAD))),
            transparent=True,
            # 面板的 y 还要看红窗有没有显示，这里先按"只有面板"的那档给；
            # 有数据后 _place_float 会再修正一次（那时窗口已经不在屏幕中央了）
            x=_float_x, y=ALERT_MARGIN,
        )

        # 浮窗从任务栏恢复 → 清掉最小化标志并重排。
        # 之前只给主窗口注册了 restored，浮窗被拉回来后标志仍为真，
        # 于是数据注入和 show() 双双被跳过，窗口永久停在旧内容上。
        _alert_window.events.restored += lambda: _on_float_restored(True)
        _panel_window.events.restored += lambda: _on_float_restored(False)

        threading.Thread(target=_startup_show_fallback, daemon=True).start()

        def _after_start():
            global _screen_size
            try:
                first = next(iter(webview.screens))
                W, H = int(first.width), int(first.height)
            except Exception:
                W, H = 1920, 1080
            _screen_size = (W, H)
            time.sleep(0.3)
            layout_all()

        webview.start(_after_start)

        # 主窗口关闭后走到这里：把工作线程拉起的浏览器进程一起带走，
        # 否则任务管理器里会留下 node.exe / msedge.exe（见 _shutdown_cleanup）。
        _shutdown_cleanup()
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