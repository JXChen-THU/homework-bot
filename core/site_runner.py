# ============================================================
# 多平台调度器
# 职责：读 sites.yaml，遍历每个站点，调用对应的抓取器
# ============================================================

import json
import os
import socket
import sys
import threading
import time
import yaml
from concurrent.futures import ThreadPoolExecutor, as_completed
from adapters import REGISTRY
from adapters.base import LoginRequiredError
from core import paths as _paths


# ============================================================
# 数据根解析
# 导入期**只解析路径、不建目录**：frozen 时 BASE 是 exe 所在目录，
# 若用户把程序放在 Program Files 等受保护位置，原先在这里
# os.makedirs() 会直接抛 PermissionError，程序连界面都起不来。
# 现在真正的目录创建与「不可写则回退 %LOCALAPPDATA%」由 core.paths 负责，
# 在 main() 里调用 apply_paths() 同步过来。
# ============================================================
def _preferred_root():
    """首选数据根：exe（或项目）所在目录 —— 老用户的配置就在这里。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BASE = _preferred_root()
DATA_DIR = os.path.join(BASE, "data")
SITES_FILE = os.path.join(BASE, "sites.yaml")


def apply_paths():
    """把 core.paths 解析出的最终路径同步到本模块。

    保留模块级常量是为了兼容既有调用方与测试的 monkeypatch 习惯；
    解析一旦完成就以这里为准。
    """
    global BASE, DATA_DIR, SITES_FILE
    BASE = _paths.ROOT or BASE
    DATA_DIR = _paths.DATA_DIR or DATA_DIR
    SITES_FILE = _paths.SITES_FILE or SITES_FILE
    return BASE



# ============================================================
# 网络检测
# 用来检测网络是否可用的目标，依次尝试，任意一个成功即视为已联网。
# 选用几个稳定的目标，避免某个站点临时不可达导致误判。
# ============================================================
_NET_CHECK_TARGETS = [
    ("www.tsinghua.edu.cn", 443),
    ("www.baidu.com", 443),
    ("223.5.5.5", 53),        # 阿里公共 DNS，作为兜底
]


def wait_for_network(timeout=120, interval=2):
    """等待网络就绪。
    返回 True 表示已联网；False 表示超过 timeout 秒仍没网。

    开机自启动时，程序可能比系统联网更快，所以先在这里等一下，
    避免一启动就抓取，全部报连接错误。
    """
    deadline = time.time() + timeout
    while True:
        for host, port in _NET_CHECK_TARGETS:
            try:
                with socket.create_connection((host, port), timeout=2):
                    return True
            except Exception:
                continue
        if time.time() >= deadline:
            return False
        time.sleep(interval)


# ============================================================
# 抓取进度播报
#
# 这是给界面看的「正在发生什么」，不是日志。只允许 3 类行：
#   1. 检查网络（含结果）
#   2. [站点名] 开始抓取
#   3. [站点名] 结果
# 适配器内部的调试输出（[silent-login]、[relogin]、英文错误码、
# 选择器字符串）一律不许透出来 —— 这里只按站点的最终结果播报。
#
# 发送端是并发抓取的 worker 线程，所以 sink 必须线程安全；
# controller 侧的 sink 会走 evaluate_js（自带线程封送），并在这里限速。
# ============================================================
_progress_lock = threading.Lock()
_progress_sink = None
_progress_count = 0
_progress_last = 0.0

PROGRESS_MIN_INTERVAL = 0.15   # 相邻两行最小间隔（秒）
PROGRESS_BURST = 3             # 区域为空时前 N 行立即输出，不拖首屏


def set_progress_sink(sink):
    """注册进度接收端；传 None 取消。返回值不参与流程控制。"""
    global _progress_sink, _progress_count, _progress_last
    with _progress_lock:
        _progress_sink = sink
        _progress_count = 0
        _progress_last = 0.0


def _emit_progress(line):
    """播报一行。任何异常都被吞掉：进度是辅助信息，不能拖垮抓取。"""
    global _progress_count, _progress_last
    if not line:
        return
    with _progress_lock:
        sink = _progress_sink
        if sink is None:
            return
        count = _progress_count
        _progress_count += 1
        if count >= PROGRESS_BURST:
            elapsed = time.time() - _progress_last
            if elapsed < PROGRESS_MIN_INTERVAL:
                time.sleep(PROGRESS_MIN_INTERVAL - elapsed)
        _progress_last = time.time()
    try:
        sink(line)
    except Exception as e:
        print(f"[progress] 播报失败：{type(e).__name__}: {e}")


def _site_label(site):
    return site.get("name") or site.get("id") or "未知站点"


def _progress_result_line(site, homeworks, errors, need_login):
    """把一次抓取的结果压成一行，只允许这几种说法。"""
    name = _site_label(site)
    if need_login:
        return f"[{name}] 需要登录"
    if errors:
        return f"[{name}] 抓取失败"
    return f"[{name}] 完成 {len(homeworks)} 条"



# ============================================================
# 原子写 + 读写互斥
# sites.yaml 原先用 open(...,"w") 直写，CPython 会先 truncate，
# 而 fetch_all 已经并行、controller 又可能随时重写配置，
# 读方会读到半截 YAML。会话文件同理。
# ============================================================
_sites_lock = threading.RLock()


def atomic_write_text(path, text, backup=False):
    """写临时文件后 os.replace 原子替换。返回 (ok, 错误信息)。

    backup=True 时会把现有文件先备份成 <path>.bak。
    """
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        if backup and os.path.exists(path):
            try:
                import shutil
                shutil.copy2(path, path + ".bak")
            except Exception:
                pass
        os.replace(tmp, path)
        return True, None
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False, f"{type(e).__name__}: {e}"


def load_sites(include_disabled=False):
    """读取 sites.yaml，返回站点列表。
    include_disabled=False 时过滤掉 enabled=False 的站点。
    文件不存在返回空列表。"""
    with _sites_lock:
        if not os.path.exists(SITES_FILE):
            return []
        try:
            with open(SITES_FILE, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            # 文件损坏时退回备份，尽量不让用户「站点全没了」
            bak = SITES_FILE + ".bak"
            if os.path.exists(bak):
                try:
                    with open(bak, encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}
                    print(f"[config] sites.yaml 损坏，已回退到备份 {bak}")
                except Exception:
                    return []
            else:
                return []
    sites = data.get("sites", [])
    if include_disabled:
        return sites
    # 只保留启用的（默认 enabled=True）
    return [s for s in sites if s.get("enabled", True)]


def save_sites(sites):
    """把站点列表写回 sites.yaml（原子替换 + 备份）。返回 (ok, 错误信息)。"""
    return _write_config_sites(sites)


def _read_config():
    """读整个 sites.yaml。返回 dict（失败或不存在返回空 dict）。"""
    with _sites_lock:
        if not os.path.exists(SITES_FILE):
            return {}
        try:
            with open(SITES_FILE, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception:
            bak = SITES_FILE + ".bak"
            if os.path.exists(bak):
                try:
                    with open(bak, encoding="utf-8") as f:
                        return yaml.safe_load(f) or {}
                except Exception:
                    return {}
            return {}


def _write_config(config):
    """原子写回整个 sites.yaml。返回 (ok, 错误信息)。"""
    text = yaml.safe_dump(
        config,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    with _sites_lock:
        ok, err = atomic_write_text(SITES_FILE, text, backup=True)
    if not ok:
        print(f"[config] 写入 sites.yaml 失败：{err}")
    return ok, err


def _write_config_sites(sites):
    """只替换站点数组，**保留其它顶层键**。

    这点很重要：`startup_mode` 这类设置就存在同一个文件的顶层。
    早期实现直接写 `{"sites": sites}`，会把它们整块抹掉 ——
    用户点一次「应用」，启动行为设置就悄悄回到默认值。
    """
    config = _read_config()
    config["sites"] = sites
    return _write_config(config)


STARTUP_MODE_KEY = "startup_mode"
STARTUP_MODE_DEFAULT = "silent"


def load_startup_mode():
    """启动行为：'silent'（静默，默认）或 'visible'（直接显示界面）。

    老用户的 sites.yaml 里没有这个键 —— 用默认值兜底，不要报错。
    """
    mode = _read_config().get(STARTUP_MODE_KEY, STARTUP_MODE_DEFAULT)
    return mode if mode in ("silent", "visible") else STARTUP_MODE_DEFAULT


def save_startup_mode(mode):
    """写回启动行为。返回 (ok, 错误信息)。"""
    if mode not in ("silent", "visible"):
        return False, f"未知的启动行为：{mode}"
    config = _read_config()
    config[STARTUP_MODE_KEY] = mode
    return _write_config(config)



# 需要加密存储的字段（账号和密码都加密）
ENCRYPTED_FIELDS = ("username", "password", "basic_user", "basic_pass")


def encrypt_site_credentials(site):
    """把单个站点的账号密码字段加密（已经是密文的跳过）。返回改动数量。"""
    try:
        from core.secrets import encrypt, is_encrypted
    except Exception:
        return 0

    changed = 0
    for key in ENCRYPTED_FIELDS:
        val = site.get(key)
        if not val or not isinstance(val, str) or is_encrypted(val):
            continue
        try:
            site[key] = encrypt(val)
            changed += 1
        except Exception as e:
            print(f"[secrets] {site.get('id')}.{key} 加密失败：{type(e).__name__}: {e}")
    return changed


def upgrade_plaintext_credentials():
    """把 sites.yaml 里还是明文的账号密码升级成密文。

    启动时调用一次。这样老的配置文件不需要用户做任何事就自动加密了。
    返回升级的字段数量；出错时返回 0，不影响程序正常启动。
    """
    try:
        from core.secrets import is_available
        if not is_available():
            return 0
    except Exception:
        return 0

    if not os.path.exists(SITES_FILE):
        return 0

    try:
        sites = load_sites(include_disabled=True)
    except Exception as e:
        print(f"[secrets] 读取 sites.yaml 失败，跳过加密升级：{type(e).__name__}: {e}")
        return 0

    total = 0
    for s in sites:
        total += encrypt_site_credentials(s)

    if total:
        try:
            save_sites(sites)
            print(f"[secrets] 已把 {total} 个明文账号/密码升级为加密存储")
        except Exception as e:
            print(f"[secrets] 写回 sites.yaml 失败：{type(e).__name__}: {e}")
            return 0
    return total


def get_site(site_id):
    """按 id 查一个站点（含被禁用的）"""
    for s in load_sites(include_disabled=True):
        if s["id"] == site_id:
            return s
    return None


def fetch_all():
    """遍历所有启用的站点，收集作业。
    返回 (homeworks, errors, need_login)：
        homeworks  - Homework 对象列表
        errors     - 抓取失败的 [{site_name, message}, ...]
        need_login - 需要登录的 [{site_id, site_name, reason}, ...]

    会话过期时：如果 sites.yaml 里填了账号密码，先尝试**后台静默重登**；
    只有静默登录失败（例如清华 SSO 要求二次验证）才把站点报告给前端。
    """
    # 先等网络就绪（开机自启动时可能比系统联网更快）
    if not wait_for_network(timeout=120):
        _emit_progress("检查网络… 失败")
        return [], [{
            "site_name": "网络",
            "message": "等待网络连接超时（120 秒），请检查网络后点「刷新任务」重试",
        }], []

    _emit_progress("检查网络… 已连接")
    # 各站点并行抓取：每个站点用独立 context，互不共享状态，因此可以同时跑。
    # 实测（本机 6 个站点）：并发 1 → 23.1s，并发 2 → 9.8s，
    # 并发 3 → 8.1s，并发 4/6 → 8.7~8.8s（不再变快，瓶颈是最慢的那个站）。
    # 取 3 而不是更高：冷启动时多个站点会同时发起登录，
    # 对清华 SSO 这类服务，3 个并发比 4 个更稳妥，而代价不到 1 秒。
    sites = load_sites()
    try:
        workers = int(os.environ.get("HOMEWORK_BOT_WORKERS", "3"))
    except Exception:
        workers = 3
    workers = max(1, min(workers, len(sites) or 1))

    # 共享浏览器：默认关闭。
    # 关于「共享浏览器」：已实测否决，不再提供开关。
    # 启动一个 Edge 只要 0.24 秒（新建 context 0.19 秒），6 个站点各自启动
    # 也就 1.4 秒，并行下更少——共享能省的不到 1 秒；而 Playwright 同步 API
    # 是线程绑定的，跨线程复用同一 browser 会直接崩
    # （TargetClosedError / greenlet cannot switch to a different thread）。
    # 结论：不值得做。各站点继续各自启动浏览器，用独立 context 隔离。

    homeworks = []
    errors = []
    need_login = []

    if workers <= 1 or len(sites) <= 1:
        for site in sites:
            hw, er, nl = _fetch_one_site(site)
            homeworks.extend(hw)
            errors.extend(er)
            need_login.extend(nl)
        return homeworks, errors, need_login

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="fetch") as pool:
        futures = {pool.submit(_fetch_one_site, s): s for s in sites}
        for fut in as_completed(futures):
            site = futures[fut]
            try:
                hw, er, nl = fut.result()
            except Exception as e:
                # _fetch_one_site 内部已经兜了异常，这里只是最后一道保险
                hw, er, nl = [], [{
                    "site_name": site.get("name", site.get("id")),
                    "message": f"{type(e).__name__}: {e}",
                }], []
            # 注意：这三行必须在 except 之外，否则正常完成的站点结果会被丢掉
            homeworks.extend(hw)
            errors.extend(er)
            need_login.extend(nl)

    return homeworks, errors, need_login


# ============================================================
# 每站点互斥锁
# 同一站点可能被两条路径同时操作：刷新（线程池）与手动点「登录」（js_api 线程）。
# 两者都会跑完整的登录流程（≥10 秒），若并发就会：同一站点拉起两个浏览器、
# 会话文件互相覆盖。
#
# 必须用 RLock：login_site() 会调用 auto_relogin_site()，
# 而 auto_relogin_site() 自身也要取同一把锁；普通 Lock 会自死锁。
# ============================================================
_site_locks = {}
_site_locks_guard = threading.Lock()


def _site_lock(site_id):
    with _site_locks_guard:
        lock = _site_locks.get(site_id)
        if lock is None:
            lock = threading.RLock()
            _site_locks[site_id] = lock
        return lock


def _session_file(site, base_dir=None):
    """该站点会话文件的完整路径；配置里没写 session 时按 id 推断。"""
    name = site.get("session") or f"{site.get('id')}_session.json"
    return os.path.join(base_dir or DATA_DIR, name)


def _session_is_broken(site, base_dir=None):
    """会话文件是否存在但已损坏（读不出合法 JSON）。

    这是「会话文件被写坏」的判据。之所以重要：损坏的会话会让
    Playwright 的 new_context(storage_state=...) 抛通用异常，
    而通用异常会被归入 errors 并**跳过重登分支**，
    于是站点永远无法自愈——用户只能手动删文件。
    """
    path = _session_file(site, base_dir)
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            json.load(f)
        return False
    except Exception:
        return True


def _discard_session(site, base_dir=None):
    """删除损坏的会话文件，让它回到「未登录」状态从而触发重登。"""
    path = _session_file(site, base_dir)
    try:
        os.remove(path)
        print(f"[fetch] {site.get('name', site.get('id'))}: 会话文件损坏，已删除以便重新登录")
        return True
    except Exception as e:
        print(f"[fetch] {site.get('name')}: 删除损坏会话失败 {type(e).__name__}: {e}")
        return False


def _fetch_one_site(site):
    """抓取单个站点（含会话失效后的后台重登），返回 (homeworks, errors, need_login)。

    这个函数会被并行调用，所以内部不能依赖任何跨站点的可变全局状态。
    整段（抓取 + 重登）在同一站点的锁内执行，避免刷新与手动登录同时操作一个站点。
    """
    with _site_lock(site.get("id")):
        return _fetch_one_site_locked(site)


def _fetch_one_site_locked(site):
    """抓一个站点。

    每个 return 之前都播报结果 —— 这是唯一需要维护的进度挂钩点。
    """
    homeworks, errors, need_login = [], [], []
    _emit_progress(f"[{_site_label(site)}] 开始抓取")

    adapter = REGISTRY.get(site.get("type"))
    if not adapter:
        errors.append({
            "site_name": site.get("name", site.get("id")),
            "message": f"未知类型 {site.get('type')}",
        })
        _emit_progress(_progress_result_line(site, homeworks, errors, need_login))
        return homeworks, errors, need_login

    try:
        items = adapter.fetch_homeworks(site, DATA_DIR)
        homeworks.extend(items)
        _emit_progress(_progress_result_line(site, homeworks, errors, need_login))
        return homeworks, errors, need_login
    except LoginRequiredError:
        pass
    except Exception as e:
        # 会话文件损坏时不要把错误当终局：删掉坏文件，走重登分支自愈
        if _session_is_broken(site):
            _discard_session(site)
        else:
            errors.append({
                "site_name": site.get("name", site.get("id")),
                "message": f"{type(e).__name__}: {e}",
            })
            _emit_progress(_progress_result_line(site, homeworks, errors, need_login))
            return homeworks, errors, need_login

    # ---------- 会话失效：尝试后台静默重登 ----------
    if auto_relogin_site(site):
        try:
            items = adapter.fetch_homeworks(site, DATA_DIR)
            homeworks.extend(items)
            _emit_progress(_progress_result_line(site, homeworks, errors, need_login))
            return homeworks, errors, need_login
        except LoginRequiredError:
            pass
        except Exception as e:
            errors.append({
                "site_name": site.get("name", site.get("id")),
                "message": f"{type(e).__name__}: {e}",
            })
            _emit_progress(_progress_result_line(site, homeworks, errors, need_login))
            return homeworks, errors, need_login

    # 静默登录不可用 / 失败 → 交给前端提示（reason 让前端能给出更准确的话术）
    need_login.append(_need_login_entry(site))
    _emit_progress(_progress_result_line(site, homeworks, errors, need_login))
    return homeworks, errors, need_login


# ============================================================
# 静默重登的失败冷却
# 清华 SSO 要求二次验证时，静默登录必定失败。如果每次刷新都试一遍，
# 既浪费时间（每站约 10 秒），也会反复去撞 SSO。失败后先冷却一段时间。
# 表只放在内存里，重启程序即清空。
# ============================================================
_relogin_cooldown = {}          # site_id -> 下次允许尝试的时间戳
_relogin_cooldown_lock = threading.Lock()
_RELOGIN_COOLDOWN_S = {
    "mfa": 600,                 # 需要人工过验证码：10 分钟内不再自动尝试
    "bad_credentials": 1800,    # 账号密码不对：30 分钟内不再自动尝试
}
_RELOGIN_COOLDOWN_DEFAULT = 300

# 记录每个站点最近一次后台重登失败的原因，供 _need_login_entry 给出准确话术。
# 例如密码错了要提示「账号密码可能不对」，而不是让用户去点验证码。
_last_failure = {}
_last_failure_lock = threading.Lock()


def _remember_failure(site_id, reason):
    with _last_failure_lock:
        _last_failure[site_id] = reason


def _last_failure_reason(site_id):
    with _last_failure_lock:
        return _last_failure.get(site_id)


def _relogin_blocked(site_id):
    with _relogin_cooldown_lock:
        until = _relogin_cooldown.get(site_id)
    return bool(until and time.time() < until)


def _relogin_mark_failed(site_id, reason):
    secs = _RELOGIN_COOLDOWN_S.get(reason, _RELOGIN_COOLDOWN_DEFAULT)
    with _relogin_cooldown_lock:
        _relogin_cooldown[site_id] = time.time() + secs


def _relogin_clear(site_id):
    """成功重登后清掉冷却，同时清掉上次失败原因。

    失败原因也必须清：否则一次历史上的 bad_credentials 会一直留在表里，
    把后续的提示话术带偏（例如明明是无头被要求验证码，却提示用户去改密码）。
    """
    with _relogin_cooldown_lock:
        _relogin_cooldown.pop(site_id, None)
    with _last_failure_lock:
        _last_failure.pop(site_id, None)


def auto_relogin_site(site):
    """尝试后台重登，分三层，都不需要用户看见任何窗口：

      1. 无头浏览器静默登录（一般站点的正常路径）
      2. 屏幕外真实窗口登录（清华 SSO 会挑战无头、放行真实窗口）
      3. 都失败 → 返回 False，由上层提示用户（例如需要输入验证码）

    返回 True 表示会话已重新保存好，可以马上重试抓取。
    全程持有该站点的可重入锁，避免与手动登录并发。
    """
    with _site_lock(site.get("id")):
        return _auto_relogin_locked(site)


def _auto_relogin_locked(site):
    try:
        from adapters.auth import has_credentials, silent_login
    except Exception:
        return False

    if not has_credentials(site):
        return False

    sid = site.get("id")
    name = site.get("name", sid)
    if _relogin_blocked(sid):
        # 此处故意保留 _last_failure 里的上次失败原因：
        # 冷却期内上层仍要靠它给出准确话术（例如提示「账号密码可能不对」）。
        print(f"[relogin] {name}: 刚失败过，冷却中，跳过自动重登")
        return False

    # 有些站点（清华 SSO）对无头浏览器**必定**要求二次验证，而真实窗口会被放行。
    # 这类站点直接跳过无头那一档，省掉一次注定失败的登录和多余的 SSO 交互。
    skip_headless = False
    try:
        from adapters import REGISTRY
        adapter = REGISTRY.get(site.get("type"))
        skip_headless = bool((getattr(adapter, "LOGIN", {}) or {}).get("skip_headless"))
    except Exception:
        pass

    reason = "mfa"
    ok = False

    # ---- 第 1 层：无头 ----
    if skip_headless:
        print(f"[relogin] {name}: 该站点无头必定被要求二次验证，直接改用屏幕外窗口…")
    else:
        print(f"[relogin] {name}: 会话失效，尝试后台静默重登…")
        try:
            ok, reason = silent_login(site, DATA_DIR)
        except Exception as e:
            print(f"[relogin] {name}: 无头登录异常 {type(e).__name__}: {e}")
            ok, reason = False, "exception"

        if ok:
            _relogin_clear(sid)
            print(f"[relogin] {name}: 成功（无头）")
            return True

    # ---- 第 2 层：屏幕外真实窗口 ----
    print(f"[relogin] {name}: 改用屏幕外窗口重试…")
    try:
        ok2, reason2 = silent_login(site, DATA_DIR, offscreen=True)
    except Exception as e:
        print(f"[relogin] {name}: 屏幕外登录异常 {type(e).__name__}: {e}")
        ok2, reason2 = False, "exception"
    if ok2:
        _relogin_clear(sid)
        print(f"[relogin] {name}: 成功（屏幕外窗口）")
        return True
    reason = reason2

    _relogin_mark_failed(sid, reason)
    _remember_failure(sid, reason)
    print(f"[relogin] {name}: 失败({reason})，需要人工处理")
    return False


def _need_login_entry(site):
    """构造给前端的「需要登录」条目，带上是哪种原因。"""
    try:
        from adapters.auth import has_credentials
        has_cred = has_credentials(site)
    except Exception:
        has_cred = False

    # 顺序有讲究：雨课堂只支持扫码，不管有没有填账号密码都是扫码，
    # 所以要放在最前面判断（否则填了账密时会给出误导性的「自动重登」话术）。
    if site.get("type") == "yuketang":
        reason = "qr_code"
    elif not has_cred:
        reason = "no_credentials"
    else:
        # 有账密但自动登录失败：用真实失败原因，给前端更准确的话术。
        # 例如密码错了要说「账号密码可能不对」，而不是让用户去点验证码。
        last = _last_failure_reason(site.get("id"))
        if last == "bad_credentials":
            reason = "bad_credentials"
        elif last == "session_save_unusable":
            reason = "session_unstable"
        else:
            reason = "manual_required"

    return {
        "site_id": site["id"],
        "site_name": site["name"],
        "reason": reason,
        "has_credentials": has_cred,
    }


def login_site(site_id):
    """触发某站点的登录流程，返回 True/False。

    先试一次无头静默登录；不行再弹可见窗口让用户完成
    （验证码、二次认证、扫码等都需要人工）。
    全程持有该站点的锁，避免与后台刷新同时操作同一站点。
    """
    site = get_site(site_id)
    if not site:
        return False
    adapter = REGISTRY.get(site.get("type"))
    if not adapter:
        return False

    with _site_lock(site_id):
        # 1) 先试静默
        if auto_relogin_site(site):
            return True

        # 2) 退回可见窗口，人工完成（会尽量先把账号密码填好，用户只需过验证码）
        #    注意：这里故意绕过冷却——冷却只针对**自动**重登，
        #    用户手动点「登录」本来就应该立刻执行。
        return adapter.login(site, DATA_DIR)
