"""运行期路径解析与日志初始化。

为什么单独成一个模块
--------------------
原先 `controller.py` 和 `site_runner.py` 都在**模块导入期**就
`os.makedirs(DATA_DIR)`，而 frozen 时 `BASE` 是 exe 所在目录。
用户若把程序解压到 `Program Files`、`C:\\` 根等受保护位置，
导入期就会抛 `PermissionError`，程序连界面都出不来。

现在改为：导入期**只解析路径不建目录**，可写性探测与目录创建延迟到
`resolve()`（在 `main()` 里调用），失败则回退到 `%LOCALAPPDATA%\\HomeworkTasks`。

另外，窗口化打包（`console=False`）下 `sys.stdout` 是 `None`，所有 `print`
会被静默丢弃——出问题时完全查不到原因。所以这里同时负责把日志建起来：
文件日志 + stdout/stderr 重定向 + 三个 excepthook。
"""

import ctypes
import logging
import logging.handlers
import os
import sys
import threading

APP_DIR_NAME = "HomeworkTasks"

# 解析结果（由 resolve() 填充）
ROOT = None                 # 实际使用的数据根目录
DATA_DIR = None             # 会话/数据库/缓存
SITES_FILE = None           # sites.yaml
LOG_PATH = None
FALLBACK_NOTICE = None      # 回退时给用户看的中文说明；未回退则为 None
COPIED_NOTICE = None        # 迁移了哪些文件

_log_handler = None         # 保留引用，防止被 GC 后日志句柄失效
_log_stream = None
_lock = threading.Lock()


# ============================================================
# 路径解析
# ============================================================
def _preferred_root():
    """首选数据根：exe（或项目）所在目录 —— 老用户的配置就在这里。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _localappdata_root():
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or os.path.expanduser("~")
    return os.path.join(base, APP_DIR_NAME)


def _writable(path):
    """探测目录是否真的可写（真正写一个临时文件，而不是看权限位）。

    只检查 os.access 不可靠：受保护目录、只读卷、被同步工具锁定的目录
    都可能通过权限检查却在真正写入时失败。
    """
    probe = os.path.join(path, ".write_test.tmp")
    try:
        os.makedirs(path, exist_ok=True)
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except Exception:
        try:
            if os.path.exists(probe):
                os.remove(probe)
        except Exception:
            pass
        return False


def _migrate(old_root, new_root):
    """把旧根目录里的用户文件**复制**到新根目录（只复制，绝不删除）。

    返回迁移说明，没有可迁移文件时返回 None。
    """
    moved = []
    for name in ("sites.yaml",):
        src = os.path.join(old_root, name)
        dst = os.path.join(new_root, name)
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                import shutil
                shutil.copy2(src, dst)
                moved.append(name)
            except Exception:
                pass

    old_data = os.path.join(old_root, "data")
    new_data = os.path.join(new_root, "data")
    if os.path.isdir(old_data):
        try:
            n = 0
            os.makedirs(new_data, exist_ok=True)
            for name in os.listdir(old_data):
                src = os.path.join(old_data, name)
                dst = os.path.join(new_data, name)
                if os.path.isfile(src) and not os.path.exists(dst):
                    import shutil
                    shutil.copy2(src, dst)
                    n += 1
            if n:
                moved.append(f"data/（{n} 个文件）")
        except Exception:
            pass

    if not moved:
        return None
    return "、".join(moved)


def resolve(root=None):
    """确定数据根目录，创建所需目录，返回 ROOT。可重复调用。

    root 显式给出时直接采用（测试用）。
    """
    global ROOT, DATA_DIR, SITES_FILE, FALLBACK_NOTICE, COPIED_NOTICE

    with _lock:
        preferred = root or _preferred_root()

        if root is not None:
            chosen = preferred
        elif _writable(preferred):
            chosen = preferred
        else:
            fallback = _localappdata_root()
            if not _writable(fallback):
                # 两个都不可写：仍然停在首选位置，让上层报错，而不是假装成功
                chosen = preferred
            else:
                chosen = fallback
                FALLBACK_NOTICE = (
                    f"程序所在目录不可写，已改用 {chosen} 保存配置和登录状态。"
                )
                COPIED_NOTICE = _migrate(preferred, chosen)
                if COPIED_NOTICE:
                    FALLBACK_NOTICE += f"已把原有配置复制过去：{COPIED_NOTICE}。"
                else:
                    FALLBACK_NOTICE += "原目录没有可迁移的配置，请重新填写账号密码。"

        ROOT = chosen
        DATA_DIR = os.path.join(chosen, "data")
        SITES_FILE = os.path.join(chosen, "sites.yaml")
        return ROOT


def ensure_dirs():
    """创建数据目录。失败返回 False（由调用方决定如何提示）。"""
    try:
        if ROOT is None:
            resolve()
        os.makedirs(DATA_DIR, exist_ok=True)
        return True
    except Exception:
        return False


# ============================================================
# 日志
# ============================================================
def _wrap_stream(fh, original=None):
    """把文件句柄包成可 write 的对象。

    刻意不用 io.TextIOWrapper：它没有稳定的 fileno()，
    子进程（Playwright 启动的浏览器）继承 fd 时会踩坑。

    original 非空时**同时**写回原流：这样从控制台运行（源码模式、
    调试脚本）时仍能看到输出，而不是全部只进日志文件。
    """
    class _Tee:
        def write(self, s):
            try:
                if not s:
                    return 0
                fh.write(s)
                fh.flush()
                if original is not None:
                    try:
                        original.write(s)
                        original.flush()
                    except Exception:
                        pass
                return len(s)
            except Exception:
                return 0

        def flush(self):
            for f in (fh, original):
                if f is None:
                    continue
                try:
                    f.flush()
                except Exception:
                    pass

        def isatty(self):
            return False

        def fileno(self):
            return fh.fileno()

    return _Tee()


def setup_logging(level=logging.INFO):
    """建立日志：文件 + stdout/stderr 重定向 + 三个 excepthook。

    返回日志文件路径（失败返回 None）。可重复调用，只生效一次。
    """
    global LOG_PATH, _log_handler, _log_stream

    with _lock:
        if LOG_PATH is not None:
            return LOG_PATH

        if ROOT is None:
            resolve()

        for candidate in (os.path.join(ROOT, "logs"),
                          os.path.join(os.environ.get("TEMP", "."), APP_DIR_NAME, "logs")):
            try:
                os.makedirs(candidate, exist_ok=True)
                path = os.path.join(candidate, "app.log")
                fh = open(path, "a", encoding="utf-8", buffering=1)
                LOG_PATH = path
                _log_stream = fh
                break
            except Exception:
                continue

        if LOG_PATH is None:
            return None

        # 文件日志（带轮转）
        try:
            handler = logging.handlers.RotatingFileHandler(
                LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s [%(threadName)s] %(message)s"))
            root_logger = logging.getLogger()
            root_logger.setLevel(level)
            root_logger.addHandler(handler)
            _log_handler = handler
        except Exception:
            pass

        # 把 print 也收进日志文件：窗口化 exe 下 stdout 是 None，print 会全部丢失。
        # 从控制台运行时同时保留控制台输出，方便源码模式调试。
        try:
            orig_out = sys.stdout if sys.stdout is not None else None
            orig_err = sys.stderr if sys.stderr is not None else None
            sys.stdout = _wrap_stream(_log_stream, orig_out)
            sys.stderr = _wrap_stream(_log_stream, orig_err)
        except Exception:
            pass

        # 未捕获异常 —— 主线程
        def _excepthook(exc_type, exc, tb):
            try:
                logging.getLogger("excepthook").error(
                    "未捕获异常", exc_info=(exc_type, exc, tb))
            except Exception:
                pass
        sys.excepthook = _excepthook

        # 未捕获异常 —— 子线程。抓取跑在 ThreadPoolExecutor 的 worker 里，
        # 主线程钩子抓不到它们，必须单独设。
        try:
            def _thread_excepthook(args):
                try:
                    logging.getLogger("thread").error(
                        "线程 %s 未捕获异常", getattr(args.thread, "name", "?"),
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
                except Exception:
                    pass
            threading.excepthook = _thread_excepthook
        except Exception:
            pass

        # 致命错误（段错误等）也留痕。
        #
        # all_threads=False 是刻意的：实测 pywebview 在窗口创建期会抛一次
        # COM 异常 RPC_E_CANTCALLOUT_ININPUTSYNCCALL (0x8001010d)，
        # 位置在 webview\platforms\winforms.py 的 create()，**程序照常运行**。
        # 用 all_threads=True 会把这时所有线程的栈都打出来，把日志顶满，
        # 反而淹没真正的问题。已用「只含 pywebview 的最小程序」复现确认：
        # 这是 pywebview/WebView2 的既有现象，与本项目代码无关。
        # 所以日志里看到这条 fatal exception 属**正常**，不用排查。
        try:
            import faulthandler
            faulthandler.enable(file=_log_stream, all_threads=False)
        except Exception:
            pass

        logging.getLogger("app").info(
            "日志已启动 | ROOT=%s | LOG=%s | frozen=%s",
            ROOT, LOG_PATH, getattr(sys, "frozen", False))
        return LOG_PATH


def startup_messages():
    """给界面用的启动提示（目前只有「已回退到别的数据目录」这一种）。"""
    msgs = []
    if FALLBACK_NOTICE:
        msgs.append({
            "kind": "warn",
            "text": FALLBACK_NOTICE,
            "detail": f"数据目录：{ROOT}",
        })
    return msgs
