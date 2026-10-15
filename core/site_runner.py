# ============================================================
# 多平台调度器
# 职责：读 sites.yaml，遍历每个站点，调用对应的抓取器
# ============================================================

import collections
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

    注意：这个等待只在**进程首轮抓取**时执行一次（由 fetch_all 控制），
    手动点「刷新任务」不会再等 120 秒 —— 文档承诺的也只是"启动时"。
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


# 首轮网络等待只做一次
_network_waited = False
_network_lock = threading.Lock()


# ============================================================
# 抓取进度播报
#
# 这是给界面看的「正在发生什么」，不是日志。只允许 3 类行：
#   1. 检查网络（等待前先播一行，结果出来再播一行）
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
# 开场慢放：表按行号索引（第 0 行 = 本轮第 1 行），0.0 是显式的
# 「第 1 行不等待、立即出」；第 2、3 行分别等 0.28 / 0.22 秒，之后回到
# PROGRESS_MIN_INTERVAL（间隔逐行收敛，不是突然切换）。
# 之前的做法是「区域为空时前 3 行立即输出」（PROGRESS_BURST = 3），
# 但那三行是同一帧里一起冒出来的，看不出先后 —— 像一次闪烁，而不是「一条条出来」。
#
# 改这个之前先读代价：开场的行是在 worker 真正联网之前播报的
# （_fetch_one_site_locked 开头），而这里的 sleep 在 _progress_lock 内，
# 所以慢放会顺带把前几个并发站点各推迟一次。默认并发 3、整轮抓取 8 秒量级，
# 慢放总预算压在 PROGRESS_OPENING_BUDGET 以内（0.5 秒），可以接受。
# 想要更多慢放又不拖抓取，就得把播报挪到独立线程（worker 只入队）。
PROGRESS_OPENING_DELAYS = (0.0, 0.28, 0.22)
PROGRESS_OPENING_BUDGET = 0.5  # 开场几行的累计等待上限（秒），超了就是拖抓取
_progress_opening_spent = 0.0  # 本轮已花在"开场慢放"上的总等待


def set_progress_sink(sink):
    """注册进度接收端；传 None 取消。返回值不参与流程控制。"""
    global _progress_sink, _progress_count, _progress_last, _progress_opening_spent
    with _progress_lock:
        _progress_sink = sink
        _progress_count = 0
        _progress_last = 0.0
        _progress_opening_spent = 0.0


def _progress_interval(count):
    """第 count 行（从 0 数）之前要等多久；0 表示不等待。

    表是按行号索引的，所以 PROGRESS_OPENING_DELAYS[0] 是「第 1 行等多久」
    （写 0.0：立即出，不能拖首屏）；表走完就落回 PROGRESS_MIN_INTERVAL。
    注意不要靠 _progress_last 的 0.0 初值来让第 1 行免费 —— 那是隐式行为，
    表里显式写 0.0 才经得起以后改时钟基准。

    PROGRESS_OPENING_BUDGET 是**硬上限**：累计慢放时间一到就不再等。
    以前这个常量只是个承诺、没有任何代码执行它 —— 那样加长慢放表就会
    悄悄变成"拖慢抓取"，而注释还写着预算受控。
    """
    if _progress_opening_spent >= PROGRESS_OPENING_BUDGET:
        return PROGRESS_MIN_INTERVAL
    if count < len(PROGRESS_OPENING_DELAYS):
        return PROGRESS_OPENING_DELAYS[count]
    return PROGRESS_MIN_INTERVAL


def _emit_progress(line):
    """播报一行。任何异常都被吞掉：进度是辅助信息，不能拖垮抓取。"""
    global _progress_count, _progress_last, _progress_opening_spent
    if not line:
        return
    with _progress_lock:
        sink = _progress_sink
        if sink is None:
            return
        count = _progress_count
        _progress_count += 1
        interval = _progress_interval(count)
        if interval > 0:
            elapsed = time.time() - _progress_last
            if elapsed < interval:
                wait = interval - elapsed
                time.sleep(wait)
                if count < len(PROGRESS_OPENING_DELAYS):
                    _progress_opening_spent += wait
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

# 跨「读—改—写」的事务锁。
# 为什么 _sites_lock 不够：它只锁住单次读或单次写，而
# `save_site_credentials` / `apply_sites_changes` / `save_startup_mode`
# 都是「load_sites() → 改内存 → save_sites()」两段式。pywebview 给每次
# js_api 调用起一个独立线程（webview/util.py 里是 Thread(target=_call)），
# 两次调用真的会重叠，后完成者用旧快照整体覆盖，前一次改动无声消失。
# 锁序固定为 _config_rw_lock → _sites_lock，绝不反向持有。
_config_rw_lock = threading.RLock()


class ConfigTransaction:
    """把「读—改—写」整段串行化的上下文管理器。

    用法：
        with config_transaction():
            sites = load_sites(include_disabled=True)
            ...
            save_sites(sites)
    嵌套安全（RLock）。
    """

    def __enter__(self):
        _config_rw_lock.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        _config_rw_lock.release()
        return False


def config_transaction():
    return ConfigTransaction()


# 最近一次 sites.yaml 读取失败的原因（给人看的中文），成功读取时为 None。
_last_config_error = None

# 最近一次"明文升级为密文"失败的原因；成功或无需升级时为 None。
_last_upgrade_error = None


def upgrade_error():
    """最近一次明文加密升级失败的原因（供界面提示）。"""
    return _last_upgrade_error


# 最近一次"某个字段加密失败"的原因（DPAPI 不可用/令牌异常时会保留明文）
def _note_upgrade_error(msg):
    global _last_upgrade_error
    _last_upgrade_error = msg


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
    文件不存在返回空列表。

    「文件不存在」与「文件读不出来」都会返回空列表（调用方拿不到差异），
    因此读失败时把原因记到 _last_config_error，由 fetch_all 转成界面横幅 ——
    否则用户看到的是"所有任务都已完成"这种和"配置坏了"无法区分的状态。
    """
    global _last_config_error
    with _sites_lock:
        if not os.path.exists(SITES_FILE):
            _last_config_error = None
            return []
        try:
            with open(SITES_FILE, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            _last_config_error = None
        except Exception as e:
            # 文件损坏时退回备份，尽量不让用户「站点全没了」
            bak = SITES_FILE + ".bak"
            reason = f"{type(e).__name__}: {e}"
            if os.path.exists(bak):
                try:
                    with open(bak, encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}
                    print(f"[config] sites.yaml 损坏，已回退到备份 {bak}")
                    _last_config_error = None
                except Exception as e2:
                    _last_config_error = (
                        f"{SITES_FILE} 与备份都无法解析（{reason}；备份："
                        f"{type(e2).__name__}: {e2}）"
                    )
                    return []
            else:
                _last_config_error = f"{SITES_FILE} 无法解析（{reason}），且没有可用的备份"
                return []
    sites = data.get("sites", [])
    if not isinstance(sites, list):
        with _sites_lock:
            _last_config_error = f"{SITES_FILE} 的 sites: 不是一个列表（实际是 {type(sites).__name__}）"
        return []
    sites = [s for s in sites if isinstance(s, dict) and s.get("id")]
    if include_disabled:
        return sites
    # 只保留启用的（默认 enabled=True）
    return [s for s in sites if s.get("enabled", True)]


def config_error():
    """最近一次配置读取失败的原因；没有失败时返回 None。"""
    with _sites_lock:
        return _last_config_error


def save_sites(sites):
    """把站点列表写回 sites.yaml（原子替换 + 备份）。返回 (ok, 错误信息)。"""
    return _write_config_sites(sites)


class ConfigUnreadable(Exception):
    """sites.yaml 存在但读不出来，且备份也不可用。"""


def _read_config(strict=False):
    """读整个 sites.yaml，返回 dict。

    文件**不存在**时返回 `{}`（首次启动的正常情况）。
    文件存在但读不出来时：
      - `strict=True` → 抛 `ConfigUnreadable`。**写路径必须用这个**：
        拿一个空 dict 去写回，会把 `sites:` 数组连同里面所有 DPAPI 密文
        凭据一次性抹掉，而界面仍显示"保存成功"。
      - `strict=False` → 返回 `{}`（只读路径用，例如读启动行为，退回默认值即可）。
    """
    with _sites_lock:
        if not os.path.exists(SITES_FILE):
            return {}
        try:
            with open(SITES_FILE, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            bak = SITES_FILE + ".bak"
            if os.path.exists(bak):
                try:
                    with open(bak, encoding="utf-8") as f:
                        data = yaml.safe_load(f)
                    if isinstance(data, dict):
                        print(f"[config] sites.yaml 读取失败，已回退到备份 {bak}")
                        return data
                except Exception:
                    pass
            if strict:
                raise ConfigUnreadable(f"{SITES_FILE} 读取失败：{type(e).__name__}: {e}")
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

    读失败时**拒绝写入**并返回错误（见 `_read_config` 的说明）。
    """
    try:
        config = _read_config(strict=True)
    except ConfigUnreadable as e:
        return False, f"拒绝写入（读不到现有配置，写了会丢数据）：{e}"
    config["sites"] = sites
    return _write_config(config)


STARTUP_MODE_KEY = "startup_mode"
# 出厂默认：**首次启动直接显示界面**。
# 老默认值是 silent，后果是「第一次运行 exe 什么都不出现」——
# 用户根本不知道程序已经在跑，只能反复双击。静默是一个应当由用户
# 主动做出的选择，不能替一个还没做过选择的用户预先选上。
# 老用户（配置里没有这个键）由 _legacy_startup_mode 继续按 silent 处理，
# 升级不会改变他们已有的行为。
STARTUP_MODE_DEFAULT = "visible"


def _legacy_startup_mode():
    """sites.yaml 里没有 startup_mode 时的兜底值。

    分两种人：
      - 老用户：data/ 里已经有会话文件，说明程序在他机器上正常用过。
        沿用老默认 silent，升级不改变既有行为。
      - 全新的机器：一个会话文件都没有，他从未做过「静默/显示」这个选择。
        用 visible —— 否则首次运行什么都不出现（就是"等了很久没反应"）。
    """
    try:
        if data_file_present():
            return "silent"
    except Exception:
        pass
    return STARTUP_MODE_DEFAULT


def load_startup_mode():
    """启动行为：'silent'（静默）或 'visible'（直接显示界面）。

    sites.yaml 里没有这个键时走 _legacy_startup_mode()，不要报错。
    """
    mode = _read_config().get(STARTUP_MODE_KEY)
    if mode in ("silent", "visible"):
        return mode
    return _legacy_startup_mode()


def save_startup_mode(mode):
    """写回启动行为。返回 (ok, 错误信息)。"""
    if mode not in ("silent", "visible"):
        return False, f"未知的启动行为：{mode}"
    try:
        config = _read_config(strict=True)
    except ConfigUnreadable as e:
        return False, f"拒绝写入（读不到现有配置，写了会丢数据）：{e}"
    config[STARTUP_MODE_KEY] = mode
    return _write_config(config)


# ============================================================
# 启动闸门：一个会话文件都没有的机器上，不要跑那一轮注定失败的抓取
#
# 为什么要有这一层（实测的"exe 等太久"的根因）：
#   首次运行（或换了一台干净的机器）时，data/ 里没有任何 *_session.json、
#   sites.yaml 里的账号密码也还是空的。fetch_all() 却照样会：
#     1) wait_for_network(timeout=120) —— 没网/网络慢时白等最多 120 秒；
#     2) 为每个启用站点拉起一个 Playwright 浏览器去"抓作业"，
#        每个站点都要等登录页超时才放弃（数秒到数十秒）。
#   两件事的结果必然都是"需要登录"，用户看到的就是"双击 exe 之后很久
#   什么都不发生"。
#
# 判定口径（与用户确认过的 A 口径）：
#   「既没有任何可用的会话文件，也没有任何站点填了可用账号密码」
#   → 本轮不发任何网络请求、不启动浏览器，直接让界面出来，
#     并告诉用户去「设置 → 填账号密码」。
#   只要还有任意一个站点存在会话文件、或填了能解密出来的账号密码
#   （下次可以静默登录），就照原来的流程正常抓取。
# ============================================================
def data_file_present():
    """data/ 下是否有会话文件（登录状态）。

    只看存在性、严格不看大小：0 字节的会话文件同样是"用户已经做过一次
    登录"的痕迹，按有数据处理 —— 让正常流程去抓、去自愈（删坏文件重登），
    比在这里替他决定"你没有数据"更安全。
    """
    try:
        with os.scandir(DATA_DIR) as it:
            for entry in it:
                name = entry.name
                if name.endswith("_session.json") and entry.is_file():
                    return True
    except Exception:
        # 目录不存在（首次运行）或读不了 → 当作没有数据文件
        pass
    try:
        # 配置里可以把会话文件名写成别的（site["session"]），按实际名字兜一遍
        for site in load_sites():
            name = site.get("session")
            if not name:
                continue
            try:
                if os.path.isfile(os.path.join(DATA_DIR, name)):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _enabled_sites():
    """启用中的站点；配置读不出来时返回空表（由上层按配置错误处理）。"""
    try:
        return load_sites()
    except Exception:
        return []


def _site_has_usable_session(site):
    """该站点是否已有一份会话文件（含配置里自定义的文件名）。"""
    try:
        return os.path.isfile(_session_file(site))
    except Exception:
        return False


def _site_has_credentials(site):
    """该站点是否填了能解密出来的账号密码（下次可静默登录）。

    返回值是**三态**：True / False / None（判断不出来，例如 adapters.auth
    导入失败）。None 必须被当成"可能有"：`has_credentials` 一旦整体抛异常，
    所有站点都会看起来"没填账密"，闸门就会短路掉一轮本该执行的抓取。
    宁可少省一次时间，也不能把用户的抓取吞掉。
    """
    try:
        from adapters.auth import has_credentials
        return bool(has_credentials(site))
    except Exception as e:
        print(f"[startup] 判断 {site.get('id')} 是否已填账密失败：{type(e).__name__}: {e}")
        return None


def _site_credentials_broken(site):
    """账号密码填了但解不开（换了电脑 / Windows 账户）→ 要提示重填。"""
    try:
        from adapters.auth import credentials_broken
        return bool(credentials_broken(site))
    except Exception:
        return False


def startup_gate():
    """启动闸门判定。

    返回：
        {
          "skip": bool,        # True = 本轮不发请求、立即把界面显示出来
          "sites": [ {site_id, site_name, reason, has_credentials,
                      credentials_broken}, ... ],   # 需要登录的站点（skip 时才有）
        }

    任何一步出问题都返回 {"skip": False}：这是"少做一次网络请求"的优化，
    绝不能因为它自己出错就拦掉正常的抓取。配置损坏（config_error）时同样不拦，
    交给 fetch_all() 报"配置坏了"，否则用户会看到"没有任务"的假象。
    """
    result = {"skip": False, "sites": []}
    try:
        try:
            if config_error():
                return result
        except Exception:
            pass

        sites = _enabled_sites()
        if not sites:
            # 没有启用中的站点（含"配置读不出来"的情况）：本来也抓不到东西，
            # 没有什么可跳过的 —— 交给 fetch_all() 去报"配置坏了"
            return result

        pending = []
        for site in sites:
            if _site_has_usable_session(site):
                # 有登录状态 → 按正常流程抓
                return result
            has_cred = _site_has_credentials(site)
            if has_cred is None:
                # 判断不出来就当作可能有：见 _site_has_credentials 的说明
                print(f"[startup] {site.get('id')} 的账密状态无法判断，按正常流程抓取")
                return result
            if has_cred:
                # 填了下次能静默登录的账密 → 按正常流程抓
                return result
            pending.append(site)

        for site in pending:
            stype = site.get("type")
            broken = _site_credentials_broken(site)
            if not supports_silent_login(stype):
                # 只能扫码/人工登录的平台（雨课堂）：给扫码话术
                reason = "qr_code" if stype == "yuketang" else "no_adapter_support"
            elif broken:
                # 填过但解不开：必须引导重填，否则用户会一直卡在"需要登录"
                reason = "bad_credentials"
            else:
                reason = "no_credentials"
            result["sites"].append({
                "site_id": site.get("id"),
                "site_name": site.get("name", site.get("id")),
                "reason": reason,
                "has_credentials": False,
                "credentials_broken": broken,
            })

        result["skip"] = True
        return result
    except Exception as e:
        print(f"[startup] 启动闸门判定失败，按正常流程继续：{type(e).__name__}: {e}")
        return {"skip": False, "sites": []}



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
            # 加密失败时这个字段会随整文件被写回磁盘 —— 也就是明文留在磁盘上。
            # 必须记下来让界面能提示，不能只 print（窗口化 exe 下 print 不可见）。
            _note_upgrade_error(f"{site.get('id')}.{key} 加密失败：{type(e).__name__}: {e}（该字段仍是明文）")
            print(f"[secrets] {_last_upgrade_error}")
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
        # save_sites 只返回 (ok, err)，**从不抛异常** —— 以前这里用 try/except
        # 接住"写盘失败"，结果是 except 永远不会触发，写盘失败也照样打印
        # "已加密存储"，实际磁盘上还是明文。必须显式判返回值。
        global _last_upgrade_error
        ok, err = save_sites(sites)
        if not ok:
            _last_upgrade_error = f"写回 sites.yaml 失败：{err}（明文可能仍在磁盘上）"
            print(f"[secrets] {_last_upgrade_error}")
            return 0
        _last_upgrade_error = None
        print(f"[secrets] 已把 {total} 个明文账号/密码升级为加密存储")
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
    # 先等网络就绪（开机自启动时可能比系统联网更快）。
    # 只在**本轮进程的第一次**抓取时等：文档承诺的是"启动时最多等待网络
    # 120 秒"，而 fetch_all 每次刷新都会被调用；断网时点「刷新任务」也白等
    # 120 秒、还被 _fetch_lock 串行化，用户会以为刷新按钮卡死。
    global _network_waited
    with _network_lock:
        need_wait = not _network_waited
        _network_waited = True
    if need_wait:
        # 等待期间必须先播一行，否则进度区会空白最长 120 秒（注释却宣称含这一行）
        _emit_progress("检查网络…")
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
    cfg_err = config_error()
    if cfg_err:
        # 不能静默返回空：界面会把"配置坏了"显示成"所有任务都已完成，太棒了！"
        return [], [{
            "site_name": "配置",
            "message": (
                f"{cfg_err}。本次没有可抓取的站点。"
                "请修好该文件，或先把它改名/删除，程序会按 sites.yaml.template 重新生成一份。"
            ),
        }], []
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

    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="fetch") as pool:
        futures = {pool.submit(_fetch_one_site, s): s for s in sites}
        for fut in as_completed(futures):
            site = futures[fut]
            try:
                hw, er, nl = fut.result()
            except Exception as e:
                # _fetch_one_site 内部已经兜了异常，这里只是最后一道保险
                # 防呆：站点线程整个崩掉时也必须留下一条能看懂的记录，
                # 不能让这个站点从结果里凭空消失（用户会以为它"没有作业"）。
                crash = {
                    "site_name": site.get("name", site.get("id")),
                    "message": "抓取线程异常：" + _error_message(e),
                }
                lines = diagnostics(tail=3)
                if lines:
                    crash["detail"] = " / ".join(lines)
                hw, er, nl = [], [crash], []
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


def _discard_session(site, base_dir=None, reason="会话文件损坏"):
    """删除会话文件，让它回到「未登录」状态从而触发重登。

    `reason` 只进日志：调用它的有两种情况 —— 会话文件读不出合法 JSON（损坏），
    以及"抓取超时、怀疑会话已被静默作废"（见 `_should_relogin_after_timeout`）。
    把原因交给调用方，日志才不会在超时路径上说"文件损坏"这种假话。
    """
    path = _session_file(site, base_dir)
    try:
        os.remove(path)
    except FileNotFoundError:
        # 防呆：同一轮里可能已经删过一次（损坏自愈 → 重登前再丢一次），
        # 文件不存在是正常情况。以前会打一条"删除会话失败 FileNotFoundError"，
        # 而这条日志会进诊断摘要、上送给用户，看着像出了故障。
        return False
    except Exception as e:
        print(f"[fetch] {site.get('name')}: 删除会话失败 {type(e).__name__}: {e}")
        return False
    print(f"[fetch] {site.get('name', site.get('id'))}: {reason}，已删除会话文件以便重新登录")
    return True


def _fetch_one_site(site):
    """抓取单个站点（含会话失效后的后台重登），返回 (homeworks, errors, need_login)。

    这个函数会被并行调用，所以内部不能依赖任何跨站点的可变全局状态。
    整段（抓取 + 重登）在同一站点的锁内执行，避免刷新与手动登录同时操作一个站点。
    """
    with _site_lock(site.get("id")):
        return _fetch_one_site_locked(site)


def _relogin_ready(site):
    """这个站点能不能自动重登（平台支持账密登录，且有能在这台机器解开的账密）。

    它决定两件事：值不值得发起重登、以及"要不要先清掉登录记录"。
      - 平台不支持账密自动登录（雨课堂只能扫码）→ 不能；
      - 账密是空的、或密文在这台机器上解不开（换了电脑 / 换了 Windows 账户）→ 不能。
    """
    if not supports_silent_login(site.get("type")):
        return False
    try:
        from adapters.auth import has_credentials, credentials_broken
        if credentials_broken(site):
            return False
        return bool(has_credentials(site))
    except Exception as e:
        print(f"[fetch] {_site_label(site)}: 判断能否自动重登失败，按不能处理"
              f"（{type(e).__name__}: {e}）")
        return False


def _error_message(exc):
    """把异常压成给用户看的一句话：**绝不允许只有类名、后面空着**。

    防呆：`FetchFailedError(site, name)` 这类不带 detail 的异常，`str(e)` 可能是空串，
    界面上就会出现"网络学堂：FetchFailedError:"这种等于没说的提示。
    """
    detail = str(exc).strip()
    if not detail:
        detail = ("没有更多细节（可点「刷新任务」重试；"
                  "仍不行就在「设置」里清除该站点登录后重新登录）")
    if detail == type(exc).__name__:
        detail = detail + "（异常自身的说明就是类名）"
    return f"{type(exc).__name__}: {detail}"


def _verdict_reporter(site):
    """把登录判定结果立刻播报到界面的进度区（关浏览器之前）。

    实测：判定出"密码错"只用 2.1 秒，而整段要 8.6 秒 —— 多出来的 6.5 秒是
    Playwright 退出浏览器的固定开销。用户不该为了等这个收尾才看到答案。
    """
    label = _site_label(site)
    texts = {
        "bad_credentials": "账号或密码可能不对，请重新填写（正在收尾）",
        "school_select_failed": "选校没成功，请重新选择学校（正在收尾）",
        "no_credentials": "还没有填账号密码",
        "mfa": "需要一次验证码，请点「登录」处理（正在收尾）",
        "no_adapter_support": "该平台不支持账密自动登录",
        "session_save_unusable": "登录成功但会话存不下来（正在收尾）",
    }
    def cb(reason):
        _emit_progress(f"[{label}] {texts.get(reason, '登录没有成功（正在收尾）')}")
    return cb


def _discard_reason(exc):
    """丢弃旧会话时写进日志的原因（超时 / 需要登录 / 其它失败措辞不同）。"""
    if exc is None:
        return "会话已失效（平台要求登录）"
    if getattr(exc, "timeout", False):
        return "抓取超时（会话可能已被静默作废）"
    return f"抓取失败（{type(exc).__name__}），先丢弃旧会话以便真正重登"


# 一次抓取里最多重登几次。
# 用户要求（最新）："系统报错的时候尝试重新登录一遍" —— **只试一遍**，
# 不再连续重试三次；同时"把之前的转三遍取消"。道理是：重登是真实登录
# （每遍 10~60 秒，清华 SSO 还可能弹窗），有答案就该立刻给用户，不要让他干等。
RELOGIN_MAX_CONSECUTIVE = 1

# 这些重登失败原因是"用户自己能修"的：收到就直接给对应话术，不要让原始抓取错误
# （超时 / 500 / 页面改版）盖在前面。用户要求："我专门改错密码，你要在收到密码错误的
# 时候就直接提示出来，不要重复登录。"
_CREDENTIAL_FAILURE_REASONS = {
    "bad_credentials",         # 密码错（平台明确回"用户名或密码不正确"）
    "school_select_failed",    # 选校没成功：服务端回的是凭据错误，要修的是选校
    "no_credentials",          # 账号密码是空的
    "session_save_unusable",   # 登录成功但会话存不下来
}


def _fetch_one_site_locked(site):
    """抓一个站点。

    失败处理（用户要求）：**任何报错都尝试后台静默重登，最多连续 3 次**；
    重登前会先丢弃该站点那份"刚刚抓取失败"的登录记录 —— 否则重登只会复用它、
    白转三轮（见下面失败分支的注释）。密码明确错误时**第一遍就停下并提示**，不重跑。
    重登由 `auto_relogin_site()` 执行，它自己还会拦掉三种情况：平台不支持账密登录、
    没有可用账密、以及刚失败过的冷却期。

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

    # ★ 既没有登录状态、又没有可用账密 → 这一轮抓取注定只会得到"需要登录"，
    #   那就别起浏览器、别发请求，直接把话说出来。用户要求：没填账号密码要和
    #   填错一样立即爆出（配合启动闸门，首次运行也不会空转）。
    if not _site_has_usable_session(site) and not _relogin_ready(site):
        need_login.append(_need_login_entry(site))
        _emit_progress(_progress_result_line(site, homeworks, errors, need_login))
        return homeworks, errors, need_login

    relogins = 0            # 本轮已经重登的次数
    failed_reason = ""      # 最后一次重登失败的 reason（密码错这类"用户能修"的直接提示）
    last_error = None       # 最后一次"抓取本身失败"的原因；None 表示该提示"需要登录"
    while True:
        try:
            items = adapter.fetch_homeworks(site, DATA_DIR)
            homeworks.extend(items)
            _emit_progress(_progress_result_line(site, homeworks, errors, need_login))
            return homeworks, errors, need_login
        except LoginRequiredError:
            # 平台明确说"要登录"：重登失败时提示"需要登录"，而不是报"抓取失败"
            last_error = None
        except Exception as e:
            if _session_is_broken(site):
                # 会话文件损坏时不要把错误当终局：删掉坏文件，走重登自愈。
                # 自愈不能以"丢失原因"为代价：选择器失效、接口 500、Playwright 崩
                # 都会走到这里，如果不留痕，用户只看到"需要登录"，线上排障零线索。
                _discard_session(site)
                print(f"[fetch] {_site_label(site)}: 会话损坏，吞掉异常 "
                      f"{type(e).__name__}: {e}（已删除会话文件并转入重登）")
                last_error = None
            else:
                # 记录原因：重登最终失败时，用户看到的应该是"抓取失败"的真相，
                # 而不是被笼统地告知"需要登录"。
                last_error = e

        # ---------- 报错了：尝试重登，最多连续 RELOGIN_MAX_CONSECUTIVE 次 ----------
        if relogins >= RELOGIN_MAX_CONSECUTIVE:
            print(f"[fetch] {_site_label(site)}: 已重登 {relogins} 次仍失败，不再重试")
            break
        if _relogin_blocked(site.get("id")):
            print(f"[fetch] {_site_label(site)}: 上次登录失败还在冷却，跳过自动重登")
            failed_reason = _last_failure_reason(site.get("id")) or ""
            break
        if not _relogin_ready(site):
            # 登不回来（没填账密 / 平台不支持账密登录 / 账密在这台机器解不开）：
            # 既不重登、也不丢会话 —— 丢了只会把用户踢成"需要登录"。
            break
        # ★ 重登前必须先丢掉这份"刚刚判失败"的会话（对 LoginRequiredError 这条最常见的
        #   路径尤其关键）。否则 silent_login 的第一步会拿它去试（auth.py 的"复用会话检查"），
        #   只要首页还能打开就回 (True, "session_valid")：并没有真正重登、却白开一次窗口，
        #   接着重试抓取再次失败 —— "报错 → 重登 → 再报错"正好空转 3 轮，全程几十秒，
        #   密码错也永远判不出来（用户实测"重登三遍才跳账号密码错误"就是这条）。
        #   丢掉它，这一轮才是一次**真实登录**：密码错会在第一遍就暴露。
        _discard_session(site, reason=_discard_reason(last_error))
        if not auto_relogin_site(site):
            # 记下这次重登为什么失败：密码错这类"用户能修"的问题必须**直接**说出来，
            # 不能被原始的抓取错误（超时 / 500）盖在前面。
            failed_reason = _last_failure_reason(site.get("id")) or ""
            break
        relogins += 1

    if last_error is not None and failed_reason not in _CREDENTIAL_FAILURE_REASONS:
        entry = {
            "site_name": site.get("name", site.get("id")),
            "message": _error_message(last_error),
        }
        # 防呆：报错时也带上最近几行诊断 —— 冻结包没有控制台，
        # 用户报障时唯一能给出来的线索就是这几行。
        lines = diagnostics(tail=3)
        if lines:
            entry["detail"] = " / ".join(lines)
        errors.append(entry)
    else:
        # 两种情况都交给前端提示（reason 让它给出更准确的话术）：
        #   1) last_error 为空 —— 本来就是"需要登录"；
        #   2) 重登失败在"用户能修"的原因上（密码错 / 选校失败 / 没账密 / 会话存不下来）。
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


# ============================================================
# 静默登录诊断（窗口化 exe 下 print 会被丢弃）
#
# 冻结包是 console=False，`sys.stdout` 为 None，silent_login 的
# `[silent-login]` 诊断一个字都留不下来 —— 用户报"登录失败"时无从查起。
# 这里用**有界内存环形缓冲**保留最近若干行，失败时把摘要附在
# `need_login[].detail` 上送给界面。
#
# 刻意不写日志文件：本仓库历史上有过一次"读写同一日志文件导致自反馈、
# 文件涨到 194GB"的事故，当时的结论就是"不落盘日志"。
# ============================================================
_DIAG_MAX = 40
_diag_lines = collections.deque(maxlen=_DIAG_MAX)
_diag_lock = threading.Lock()


def _diag(msg):
    """记录一行诊断：既打印（源码模式可见），也留在环形缓冲里（冻结包可见）。"""
    text = str(msg)
    with _diag_lock:
        _diag_lines.append(text)
    print(text)


def diagnostics(tail=6):
    """最近若干行诊断，给界面显示用。"""
    with _diag_lock:
        items = list(_diag_lines)
    return items[-tail:] if tail else items


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


def supports_silent_login(site_type):
    """该平台是否支持「填账号密码 → 后台静默登录」。

    能力位定义在适配器模块上（`SUPPORTS_SILENT_LOGIN = False`，目前只有雨课堂）。
    做成单一出处，是因为这个事实原先被硬编码在三处：设置面板的按钮
    （`controller.list_sites`）、需要登录条目的话术（`_need_login_entry`）、
    后台重登调度（`_auto_relogin_locked`）——三处只要有一处漏改，
    用户就会看到一个"承诺能自动登录、但永远失败"的按钮，或者白等两次必然失败的登录。
    """
    try:
        from adapters import REGISTRY
        adapter = REGISTRY.get(site_type)
        if adapter is None:
            return False
        return bool(getattr(adapter, "SUPPORTS_SILENT_LOGIN", True))
    except Exception:
        # 拿不到适配器时按"支持"处理，不改变既有行为
        return True


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

    # 平台本身就没有「账密自动登录」这条路（雨课堂只能扫码）：直接返回，
    # 不要白跑两次 headless 尝试（各 30~120 秒），否则用户点了「刷新任务」
    # 会先卡上几分钟，而横幅文案明明写着"无法自动登录"。
    if not supports_silent_login(site.get("type")):
        print(f"[relogin] {site.get('name', site.get('id'))}: 该平台不支持静默登录，跳过自动重登")
        _remember_failure(site.get("id"), "no_adapter_support")
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
    headless_tried = False

    # ---- 第 1 层：无头 ----
    if skip_headless:
        print(f"[relogin] {name}: 该站点无头必定被要求二次验证，直接改用屏幕外窗口…")
    else:
        headless_tried = True
        print(f"[relogin] {name}: 会话失效，尝试后台静默重登…")
        try:
            ok, reason = silent_login(site, DATA_DIR, log=_diag,
                                      on_verdict=_verdict_reporter(site))
        except Exception as e:
            print(f"[relogin] {name}: 无头登录异常 {type(e).__name__}: {e}")
            ok, reason = False, "exception"

        if ok:
            _relogin_clear(sid)
            print(f"[relogin] {name}: 成功（无头）")
            return True

        # 密码明确是错的：屏幕外窗口不可能把它变对，只会再花 10~60 秒、还可能闪一下窗口。
        # 用户要求："我专门改错密码，你要在收到密码错误的时候就直接提示出来，不要重复登录。"
        if reason == "bad_credentials":
            _relogin_mark_failed(sid, reason)
            _remember_failure(sid, reason)
            print(f"[relogin] {name}: 无头层已明确是密码错误，不再用屏幕外窗口重试（直接提示用户）")
            return False

    # ---- 第 2 层：屏幕外真实窗口 ----
    print(f"[relogin] {name}: 改用屏幕外窗口重试…")
    try:
        ok2, reason2 = silent_login(site, DATA_DIR, offscreen=True, log=_diag,
                                    on_verdict=_verdict_reporter(site))
    except Exception as e:
        print(f"[relogin] {name}: 屏幕外登录异常 {type(e).__name__}: {e}")
        ok2, reason2 = False, "exception"
    if ok2:
        _relogin_clear(sid)
        print(f"[relogin] {name}: 成功（屏幕外窗口）")
        return True

    # 不能让弱 reason 覆盖强的：
    # 无头层已经明确得到 bad_credentials（密码真错），而屏幕外层因为慢/白屏
    # 给出 timeout 时，写回 timeout 会把冷却从 1800 秒缩到 300 秒、话术退化成
    # "去确认验证码" —— 于是用户每 5 分钟再撞一次 SSO，而且被引向错误的动作。
    # 只有无头层真的跑过才谈"保留更强 reason"：skip_headless 站点上的
    # 初始值 "mfa" 只是占位，不是观测结果。
    reason = _stronger_reason(reason, reason2) if headless_tried else reason2

    _relogin_mark_failed(sid, reason)
    _remember_failure(sid, reason)
    print(f"[relogin] {name}: 失败({reason})，需要人工处理")
    return False


# reason 的"强度"：越大越能说明真实原因，失败落库时优先保留
_REASON_STRENGTH = {
    "bad_credentials": 6,
    "session_save_unusable": 5,
    "no_adapter_support": 4,
    "no_credentials": 4,
    "login_page_error": 4,
    "school_select_failed": 4,
    "no_login_form": 3,
    "mfa": 3,
    "timeout": 2,
    "exception": 1,
}


def _stronger_reason(a, b):
    """两个失败原因里取更能说明问题的那一个。"""
    return a if _REASON_STRENGTH.get(a, 0) >= _REASON_STRENGTH.get(b, 0) else b


# 后端 reason → 前端话术键。前端 LOGIN_HINTS 按这里的取值取话术。
_REASON_TO_FRONTEND = {
    "bad_credentials": "bad_credentials",
    "session_save_unusable": "session_unstable",
    "mfa": "mfa",
    "timeout": "timeout",
    "exception": "exception",
    "login_page_error": "login_page_error",
    "no_login_form": "login_page_error",
    "no_adapter_support": "no_adapter_support",
    "no_credentials": "no_credentials",
    # 选校没成功被服务端回了"凭据错误"：真正要修的是选校，不能引导用户去改密码
    "school_select_failed": "manual_required",
}


def _need_login_entry(site):
    """构造给前端的「需要登录」条目，带上是哪种原因。"""
    try:
        from adapters.auth import has_credentials
        has_cred = has_credentials(site)
    except Exception:
        has_cred = False

    detail = None
    # 顺序有讲究：只能扫码的平台（雨课堂）不管有没有填账号密码都是扫码，
    # 所以要放在最前面判断（否则填了账密时会给出误导性的「自动重登」话术）。
    if not supports_silent_login(site.get("type")):
        reason = "qr_code" if site.get("type") == "yuketang" else "no_adapter_support"
    elif not has_cred:
        reason = "no_credentials"
    else:
        # 有账密但自动登录失败：用真实失败原因，给前端更准确的话术。
        # 例如密码错了要说「账号密码可能不对」，而不是让用户去点验证码；
        # 基础设施故障（Playwright 崩、缺 msedge）也不能被说成"去确认验证码"。
        last = _last_failure_reason(site.get("id")) or ""
        reason = _REASON_TO_FRONTEND.get(last, "manual_required")
        if reason in ("exception", "login_page_error"):
            # 这类失败用户自己修不了，必须给可排查的线索（冻结包里 print 不可见）
            lines = diagnostics(tail=4)
            if lines:
                detail = " / ".join(lines)

    entry = {
        "site_id": site["id"],
        "site_name": site["name"],
        "reason": reason,
        "has_credentials": has_cred,
    }
    if detail:
        entry["detail"] = detail
    return entry


def login_site(site_id):
    """触发某站点的登录流程，返回 `(ok, reason)`。

    先试一次无头静默登录；不行再弹可见窗口让用户完成
    （验证码、二次认证、扫码等都需要人工）。
    全程持有该站点的锁，避免与后台刷新同时操作同一站点。

    为什么返回 reason：以前只返回布尔，`Api.login()` 就只回 `{"ok": ok}`，
    前端明明备好了五套失败话术却永远用不上，用户只能看到"登录未完成，
    点击重试"，分不清"密码错"和"还差一次验证码"。
    """
    site = get_site(site_id)
    if not site:
        return False, "no_such_site"
    adapter = REGISTRY.get(site.get("type"))
    if not adapter:
        _remember_failure(site_id, "no_adapter_support")
        return False, "no_adapter_support"

    with _site_lock(site_id):
        # 1) 先试静默
        if auto_relogin_site(site):
            return True, "ok"

        reason = _last_failure_reason(site_id) or "manual_required"

        # 2) 退回可见窗口，人工完成（会尽量先把账号密码填好，用户只需过验证码）
        #    注意：这里故意绕过冷却——冷却只针对**自动**重登，
        #    用户手动点「登录」本来就应该立刻执行。
        try:
            ok = adapter.login(site, DATA_DIR)
        except Exception as e:
            print(f"[login] {site.get('name', site_id)}: 人工登录异常 {type(e).__name__}: {e}")
            return False, "exception"

        if not ok:
            return False, reason

        # 人工登录"成功"也要复核会话文件真的落盘了：
        # 磁盘满 / 目录只读时以前仍然返回成功，用户看到横幅消失，
        # 下次刷新立刻又"需要登录"，形成"登录成功但抓不到作业"的循环。
        try:
            sess = auth_session_path(DATA_DIR, site)
            if not os.path.exists(sess) or os.path.getsize(sess) == 0:
                _remember_failure(site_id, "session_save_unusable")
                print(f"[login] {site.get('name', site_id)}: 登录流程结束但会话文件不可用：{sess}")
                return False, "session_save_unusable"
        except Exception as e:
            print(f"[login] 复核会话文件失败：{type(e).__name__}: {e}")

        _relogin_clear(site_id)
        return True, "ok"


def auth_session_path(base_dir, site):
    """会话文件路径的统一入口（转发到 adapters.auth.session_path）。

    site_runner 与 6 个适配器必须用同一套口径，否则"配置里没写 session"
    时会出现"登录写 A、抓取读 B"的断链。
    """
    try:
        from adapters.auth import session_path
        return session_path(base_dir, site)
    except Exception:
        name = site.get("session") or f"{site.get('id')}_session.json"
        return os.path.join(base_dir, name)
