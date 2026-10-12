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
def _redirect_print_to_logging(level=logging.INFO):
    """把 print/stdout 的输出交给 logging，而不是自己另开一个文件句柄。

    为什么改成这样（踩过坑才改）：

    最初是「直接把 sys.stdout 包成一个写 app.log 的对象」，于是同一份
    日志文件被**两个彼此不知情的句柄**打开——RotatingFileHandler 一个、
    直写一个。由此带来两个问题：

      1. 轮转之后直写句柄仍指向被改名/删除的旧文件，日志会分裂；
      2. 一旦有人「读日志文件并打印」（排障脚本里很自然的写法），
         读与写落在同一个文件上会形成自我反馈，文件呈指数级膨胀。
         实际发生过：几分钟内涨到 194 GB，把 D 盘占满。

    改为经 logging 转发后只剩一个句柄，轮转正常，也不存在这条自反馈路径。
    """
    logger = logging.getLogger("console")

    class _LogStream:
        """只实现 logging 需要的接口；写入统一走 logger（同一把锁、同一个句柄）。"""

        def __init__(self, lvl):
            self._lvl = lvl
            self._lock = threading.Lock()
            self._busy = False

        def write(self, s):
            if not s:
                return 0
            if not s.strip():          # 纯空白不值得占一行日志
                return len(s)
            # 防递归：万一 logging 内部又往这里写，直接丢弃而不是无限套娃
            with self._lock:
                if self._busy:
                    return len(s)
                self._busy = True
            try:
                logger.log(self._lvl, s.rstrip())
            except Exception:
                pass
            finally:
                with self._lock:
                    self._busy = False
            return len(s)

        def flush(self):
            try:
                for h in logging.getLogger().handlers:
                    h.flush()
            except Exception:
                pass

        def isatty(self):
            return False

        def writable(self):
            return True

    sys.stdout = _LogStream(level)
    sys.stderr = _LogStream(logging.ERROR)


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
                # 只探测能否写，随即关闭：真正的写入统一交给 RotatingFileHandler，
                # 绝不让第二个句柄长期持有同一个文件（见 _redirect_print_to_logging）。
                with open(path, "a", encoding="utf-8"):
                    pass
                LOG_PATH = path
                break
            except Exception:
                continue

        if LOG_PATH is None:
            return None

        # 文件日志（带轮转）—— 全项目唯一的日志句柄
        try:
            handler = logging.handlers.RotatingFileHandler(
                LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s [%(threadName)s] %(message)s"))
            root_logger = logging.getLogger()
            root_logger.setLevel(level)
            root_logger.addHandler(handler)
            _log_handler = handler
            _log_stream = handler.stream
        except Exception:
            pass

        # 把 print 收进日志：窗口化 exe 下 stdout 是 None，print 否则全部丢失。
        try:
            _redirect_print_to_logging(level)
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
