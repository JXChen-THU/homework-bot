import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone


@dataclass
class Homework:
    """一条作业的数据结构"""
    site: str          # 站点 ID，如 "thuoj_ai"
    site_name: str     # 站点显示名，如 "清华 OJ"
    uid: str           # 全局唯一 ID，如 "thuoj_ai:82"
    title: str         # 作业标题
    course: str        # 课程名
    start: str         # 开始时间 "2026-09-15 00:00:00"
    end: str           # 截止时间 "2026-09-25 23:59:00"
    url: str           # 原网页链接

    def to_dict(self):
        """转成字典，方便发给前端 JS"""
        return asdict(self)


class LoginRequiredError(Exception):
    """需要登录时抛出的异常"""
    def __init__(self, site_id, site_name):
        self.site_id = site_id
        self.site_name = site_name
        super().__init__(f"{site_name} 需要登录")


class FetchFailedError(Exception):
    """已登录、但**抓取本身**失败（网络抖动、接口 5xx、页面结构变了）。

    必须与 LoginRequiredError 分开：把接口 500 报成"需要登录"会触发
    一次毫无意义的静默重登（每站 10~60 秒），并且给用户错误的横幅
    —— 会话明明有效，用户却被叫去重新登录。

    `timeout=True` 表示这次失败是"等满了 DATA_READY_MS 仍没内容、又没有任何
    失效信号"。它比 5xx / 结构变化多一层含义：**会话可能已经被静默作废**，
    所以 `site_runner` 会在重登前先清掉该站点的登录记录。

    注意 `timeout` 只管"要不要清登录记录"：**任何**抓取失败都会触发一次静默重登
    （只重登一遍，见 `site_runner.RELOGIN_MAX_CONSECUTIVE`）。

    `credentials=True` 是更强的一种：**凭据本身就没通过**（例如 Tyche 的 Basic Auth
    被服务器回了 401）。这种失败重登一百次也没用，所以 `site_runner` 会跳过重登、
    当场按"账号或密码不对"提示用户。
    """
    def __init__(self, site_id, site_name, detail="", timeout=False, credentials=False):
        self.site_id = site_id
        self.site_name = site_name
        self.detail = detail
        self.timeout = timeout
        # ★ 必须真的挂到实例上：site_runner 用 getattr(e, "credentials", False) 判断
        #   "这轮不用重登"。只改签名不赋值的话，标记永远是 False，补丁形同不存在。
        self.credentials = credentials
        super().__init__(f"{site_name} 抓取失败" + (f"：{detail}" if detail else ""))


# Chromium 的网络层错误码。这些**不是**站点故障，而是"这台机器连不上对方"，
# 最典型的场景是校园站点（Tyche 就是）没连校园网/VPN。命中时要给用户一句能照做的话，
# 而不是把 "net::ERR_CONNECTION_TIMED_OUT" 这种原文甩出去。
NET_ERROR_CODES = (
    "ERR_CONNECTION_TIMED_OUT", "ERR_TIMED_OUT", "ERR_CONNECTION_REFUSED",
    "ERR_NAME_NOT_RESOLVED", "ERR_ADDRESS_UNREACHABLE", "ERR_INTERNET_DISCONNECTED",
    "ERR_CONNECTION_RESET", "ERR_CONNECTION_CLOSED", "ERR_CONNECTION_ABORTED",
    "ERR_EMPTY_RESPONSE", "ERR_NETWORK_CHANGED", "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED", "ERR_SSL_PROTOCOL_ERROR",
)


def network_error_code(text):
    """从异常文本里认出 Chromium 网络错误码；没命中返回 None。"""
    s = str(text or "")
    for code in NET_ERROR_CODES:
        if code in s:
            return code
    return None


def network_error_message(site_name, code=None):
    """网络层失败时给用户看的话：指明站点、说明需要校园网/VPN、给出下一步动作。"""
    tail = f"（{code}）" if code else ""
    return (f"无法连接到 {site_name}{tail}：该站点需要校园网/VPN。"
            f"请连接校园 VPN 后点「刷新任务」重试。")


def utc_iso_to_local(value):
    """把接口返回的 **不带时区后缀的 UTC** ISO 时间转成本机时间文本。

    Tyche 的 `ShowGroup` 接口就是这种形态：`"2026-10-18T15:59:00"` 表示的是
    UTC 的 15:59，也就是北京时间的**当日 23:59**（作业周的起止因此是
    周一 00:00 ~ 周日 23:59）。原实现只把 `T` 换成空格就透传，
    `task_logic.parse_time` 会把它当成本机时间 → **所有截止时间早 8 小时**：
    红窗提前 8 小时报警，倒计时与"还剩多久"全错。

    与 `smartestu._iso_to_local` 的差别只有一处：那边靠 `Z` 后缀认出 UTC，
    而 Tyche 不带后缀，所以这里**没有时区信息时一律按 UTC 解释**
    （naive 的 `datetime.astimezone()` 会假定它是本机时间，正是这个 bug 的来源）。

    带 `Z` / `+08:00` 等显式时区的输入照常解析，不会被二次偏移；
    解析不了就原样返回（`T` 换空格、去掉 `Z`），交给 `parse_time` 兜底。
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        iso = text[:-1] + "+00:00" if text.endswith("Z") else text
        dt = datetime.fromisoformat(iso)
    except Exception:
        return text.replace("T", " ").replace("Z", "").strip()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def session_path(base_dir, site):
    """会话文件路径的**唯一口径**：配置里没写 session 时按站点 id 推断。

    放在 base 而不是 auth：auth 依赖 playwright，而 6 个适配器都要拼这个路径，
    没必要为了一个纯拼路径的函数把 playwright 引进来。
    `adapters.auth.session_path` 只是转发到这里，保证全仓一套口径 ——
    以前有 7 个调用点直接 `site["session"]`，用户手改配置删掉该键时
    会出现"登录写 A 文件、抓取读 B 键"的断链（KeyError，永不自愈）。
    """
    return os.path.join(base_dir, site.get("session") or f"{site.get('id')}_session.json")
