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