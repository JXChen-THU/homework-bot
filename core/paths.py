"""运行期路径解析。

为什么单独成一个模块
--------------------
原先 `controller.py` 和 `site_runner.py` 都在**模块导入期**就
`os.makedirs(DATA_DIR)`，而 frozen 时 `BASE` 是 exe 所在目录。
用户若把程序解压到 `Program Files`、`C:\\` 根等受保护位置，
导入期就会抛 `PermissionError`，程序连界面都出不来。

现在改为：导入期**只解析路径不建目录**，可写性探测与目录创建延迟到
`resolve()`（在 `main()` 里调用），失败则回退到 `%LOCALAPPDATA%\\HomeworkTasks`。

本模块**不写任何日志文件**，只做路径解析。
"""

import ctypes
import os
import sys
import threading

APP_DIR_NAME = "HomeworkTasks"

# 解析结果（由 resolve() 填充）
ROOT = None                 # 实际使用的数据根目录
DATA_DIR = None             # 会话/数据库/缓存
SITES_FILE = None           # sites.yaml
FALLBACK_NOTICE = None      # 回退时给用户看的中文说明；未回退则为 None
COPIED_NOTICE = None        # 迁移了哪些文件

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
