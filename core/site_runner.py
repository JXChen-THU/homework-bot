# ============================================================
# 多平台调度器
# 职责：读 sites.yaml，遍历每个站点，调用对应的抓取器
# ============================================================

import os
import sys
import socket
import threading
import time
import yaml
from concurrent.futures import ThreadPoolExecutor, as_completed
from adapters import REGISTRY
from adapters.base import LoginRequiredError


# 打包后 __file__ 指向 _internal 里面，
# 用户数据（sites.yaml、data/）要放在 exe 旁边，和 app.py 保持一致
if getattr(sys, 'frozen', False):
    BASE = os.path.dirname(sys.executable)
else:
    BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATA_DIR = os.path.join(BASE, "data")
os.makedirs(DATA_DIR, exist_ok=True)
SITES_FILE = os.path.join(BASE, "sites.yaml")


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


def load_sites(include_disabled=False):
    """读取 sites.yaml，返回站点列表。
    include_disabled=False 时过滤掉 enabled=False 的站点。
    文件不存在返回空列表。"""
    if not os.path.exists(SITES_FILE):
        return []
    with open(SITES_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    sites = data.get("sites", [])
    if include_disabled:
        return sites
    # 只保留启用的（默认 enabled=True）
    return [s for s in sites if s.get("enabled", True)]


def save_sites(sites):
    """把站点列表写回 sites.yaml"""
    with open(SITES_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            {"sites": sites}, f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )


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
        return [], [{
            "site_name": "网络",
            "message": "等待网络连接超时（120 秒），请检查网络后点「刷新任务」重试",
        }], []

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


def _fetch_one_site(site):
    """抓取单个站点（含会话失效后的后台重登），返回 (homeworks, errors, need_login)。

    这个函数会被并行调用，所以内部不能依赖任何跨站点的可变全局状态。
    """
    homeworks, errors, need_login = [], [], []
    adapter = REGISTRY.get(site.get("type"))
    if not adapter:
        errors.append({
            "site_name": site.get("name", site.get("id")),
            "message": f"未知类型 {site.get('type')}",
        })
        return homeworks, errors, need_login

    try:
        items = adapter.fetch_homeworks(site, DATA_DIR)
        homeworks.extend(items)
        return homeworks, errors, need_login
    except LoginRequiredError:
        pass
    except Exception as e:
        errors.append({
            "site_name": site.get("name", site.get("id")),
            "message": f"{type(e).__name__}: {e}",
        })
        return homeworks, errors, need_login

    # ---------- 会话失效：尝试后台静默重登 ----------
    if auto_relogin_site(site):
        try:
            items = adapter.fetch_homeworks(site, DATA_DIR)
            homeworks.extend(items)
            return homeworks, errors, need_login
        except LoginRequiredError:
            pass
        except Exception as e:
            errors.append({
                "site_name": site.get("name", site.get("id")),
                "message": f"{type(e).__name__}: {e}",
            })
            return homeworks, errors, need_login

    # 静默登录不可用 / 失败 → 交给前端提示（reason 让前端能给出更准确的话术）
    need_login.append(_need_login_entry(site))
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


def _relogin_blocked(site_id):
    with _relogin_cooldown_lock:
        until = _relogin_cooldown.get(site_id)
    return bool(until and time.time() < until)


def _relogin_mark_failed(site_id, reason):
    secs = _RELOGIN_COOLDOWN_S.get(reason, _RELOGIN_COOLDOWN_DEFAULT)
    with _relogin_cooldown_lock:
        _relogin_cooldown[site_id] = time.time() + secs


def _relogin_clear(site_id):
    with _relogin_cooldown_lock:
        _relogin_cooldown.pop(site_id, None)


def auto_relogin_site(site):
    """尝试后台重登，分三层，都不需要用户看见任何窗口：

      1. 无头浏览器静默登录（一般站点的正常路径）
      2. 屏幕外真实窗口登录（清华 SSO 会挑战无头、放行真实窗口）
      3. 都失败 → 返回 False，由上层提示用户（例如需要输入验证码）

    返回 True 表示会话已重新保存好，可以马上重试抓取。
    """
    try:
        from adapters.auth import has_credentials, silent_login
    except Exception:
        return False

    if not has_credentials(site):
        return False

    sid = site.get("id")
    name = site.get("name", sid)
    if _relogin_blocked(sid):
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
    """
    site = get_site(site_id)
    if not site:
        return False
    adapter = REGISTRY.get(site.get("type"))
    if not adapter:
        return False

    # 1) 先试静默
    if auto_relogin_site(site):
        return True

    # 2) 退回可见窗口，人工完成（会尽量先把账号密码填好，用户只需过验证码）
    return adapter.login(site, DATA_DIR)
