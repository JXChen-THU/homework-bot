import os
from dataclasses import dataclass, asdict


@dataclass
class Homework:
    site: str
    site_name: str
    uid: str
    title: str
    course: str
    start: str
    end: str
    url: str

    def to_dict(self):
        return asdict(self)


class LoginRequiredError(Exception):
    def __init__(self, site_id, site_name):
        self.site_id = site_id
        self.site_name = site_name
        super().__init__(f"{site_name} 需要登录")


def session_path(site, base_dir):
    return os.path.join(base_dir, site["session"])