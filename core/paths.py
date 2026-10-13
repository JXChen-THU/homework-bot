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


def _usable_root(path):
    """数据根与它下面的 data/ 都要真的可写。

    只探测根目录不够：根可写、但**已经存在**的 `data/` 子目录不可写时，
    以前会误判为"可写"并停在这里，随后所有会话/数据库写入都会失败。
    """
    return _writable(path) and _writable(os.path.join(path, "data"))


def _looks_empty_config(path):
    """该 sites.yaml 是否只是空壳（一个可用站点都没有）。

    用来避免目标目录里"上次运行自动生成/用户清空"的空配置**遮蔽**旧目录里的真实配置。
    读不出来时一律返回 False —— 宁可不动，也不覆盖用户文件。
    """
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        sites = data.get("sites") or []
        return not [s for s in sites if isinstance(s, dict) and s.get("id")]
    except Exception:
        return False


def _migrate(old_root, new_root):
    """把旧根目录里的用户文件**复制**到新根目录（只复制，绝不删除）。

    返回 (copied, skipped, failed) 三个列表，元素是给人看的中文短句。
    以前这里把"目标已存在"和"复制失败"都当成功静默跳过，然后统一告诉用户
    「原目录没有可迁移的配置」—— 用户在旧目录里的配置其实还在，只是被遮蔽了。
    """
    import shutil

    copied = []
    skipped = []
    failed = []

    # ---- sites.yaml ----
    src = os.path.join(old_root, "sites.yaml")
    dst = os.path.join(new_root, "sites.yaml")
    if os.path.isfile(src):
        if not os.path.exists(dst):
            try:
                shutil.copy2(src, dst)
                copied.append("sites.yaml")
            except Exception as e:
                failed.append(f"sites.yaml（{type(e).__name__}）")
        elif _looks_empty_config(dst) and not _looks_empty_config(src):
            try:
                shutil.copy2(dst, dst + ".bak")
                shutil.copy2(src, dst)
                copied.append("sites.yaml（覆盖了目标目录里的空配置，旧文件存为 sites.yaml.bak）")
            except Exception as e:
                failed.append(f"sites.yaml（{type(e).__name__}）")
        else:
            skipped.append("sites.yaml")

    # ---- data/ ----
    old_data = os.path.join(old_root, "data")
    new_data = os.path.join(new_root, "data")
    if os.path.isdir(old_data):
        copied_n = skipped_n = 0
        try:
            os.makedirs(new_data, exist_ok=True)
            for name in sorted(os.listdir(old_data)):
                s = os.path.join(old_data, name)
                d = os.path.join(new_data, name)
                if not os.path.isfile(s):
                    continue
                if os.path.exists(d):
                    skipped_n += 1
                    continue
                try:
                    shutil.copy2(s, d)
                    copied_n += 1
                except Exception:
                    failed.append(f"data/{name}")
        except Exception as e:
            failed.append(f"data/（{type(e).__name__}）")
        if copied_n:
            copied.append(f"data/（{copied_n} 个文件）")
        if skipped_n:
            skipped.append(f"data/（{skipped_n} 个文件已存在）")

    return copied, skipped, failed


def resolve(root=None):
    """确定数据根目录，创建所需目录，返回 ROOT。可重复调用。

    root 显式给出时直接采用（测试用）。
    """
    global ROOT, DATA_DIR, SITES_FILE, FALLBACK_NOTICE, COPIED_NOTICE

    with _lock:
        # 每次解析都从干净状态开始：否则同一进程里第二次 resolve() 会
        # 把上一次的回退提示继续挂在 startup_messages() 里，报一个已不成立的"已改用 …"。
        FALLBACK_NOTICE = None
        COPIED_NOTICE = None

        preferred = root or _preferred_root()

        if root is not None:
            chosen = preferred
        elif _usable_root(preferred):
            chosen = preferred
        else:
            fallback = _localappdata_root()
            if not _usable_root(fallback):
                # 两个都不可写：仍然停在首选位置，让上层报错，而不是假装成功
                chosen = preferred
            else:
                chosen = fallback
                copied, skipped, failed = _migrate(preferred, chosen)
                COPIED_NOTICE = "、".join(copied) if copied else None
                parts = [f"程序所在目录不可写，已改用 {chosen} 保存配置和登录状态。"]
                if copied:
                    parts.append("已从原目录复制：" + "、".join(copied) + "。")
                if skipped:
                    parts.append("目标目录已有同名文件、未覆盖：" + "、".join(skipped) + "。")
                if failed:
                    parts.append("复制失败：" + "、".join(failed) + "。")
                if not copied and not skipped and not failed:
                    parts.append("原目录没有可迁移的配置，请重新填写账号密码。")
                FALLBACK_NOTICE = "".join(parts)

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
