import os
from dataclasses import dataclass, asdict


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
    （最多连续 3 次，见 `site_runner.RELOGIN_MAX_CONSECUTIVE`）。
    """
    def __init__(self, site_id, site_name, detail="", timeout=False):
        self.site_id = site_id
        self.site_name = site_name
        self.detail = detail
        self.timeout = timeout
        super().__init__(f"{site_name} 抓取失败" + (f"：{detail}" if detail else ""))


def session_path(base_dir, site):
    """会话文件路径的**唯一口径**：配置里没写 session 时按站点 id 推断。

    放在 base 而不是 auth：auth 依赖 playwright，而 6 个适配器都要拼这个路径，
    没必要为了一个纯拼路径的函数把 playwright 引进来。
    `adapters.auth.session_path` 只是转发到这里，保证全仓一套口径 ——
    以前有 7 个调用点直接 `site["session"]`，用户手改配置删掉该键时
    会出现"登录写 A 文件、抓取读 B 键"的断链（KeyError，永不自愈）。
    """
    return os.path.join(base_dir, site.get("session") or f"{site.get('id')}_session.json")
