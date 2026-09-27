# ============================================================
# 多平台调度器
# 职责：读 sites.yaml，遍历每个站点，调用对应的抓取器
# ============================================================

import os
import sys
import socket
import time
import yaml
from adapters import REGISTRY
from adapters.base import LoginRequiredError


# 打包后 __file__ 指向 _internal 里面，
# 用户数据（sites.yaml、data/）要放在 exe 旁边，和 app.py 保持一致
if getattr(sys, 'frozen', False):
    BASE = os.path.dirname(sys.executable)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))

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
        need_login - 需要登录的 [{site_id, site_name}, ...]
    """
    # 先等网络就绪（开机自启动时可能比系统联网更快）
    if not wait_for_network(timeout=120):
        return [], [{
            "site_name": "网络",
            "message": "等待网络连接超时（120 秒），请检查网络后点「刷新任务」重试",
        }], []

    homeworks = []
    errors = []
    need_login = []

    for site in load_sites():
        # 从注册表里找对应的抓取器
        adapter = REGISTRY.get(site.get("type"))
        if not adapter:
            errors.append({
                "site_name": site.get("name", site.get("id")),
                "message": f"未知类型 {site.get('type')}",
            })
            continue

        try:
            items = adapter.fetch_homeworks(site, DATA_DIR)
            homeworks.extend(items)
        except LoginRequiredError:
            # 会话失效
            need_login.append({
                "site_id": site["id"],
                "site_name": site["name"],
            })
        except Exception as e:
            # 其他错误
            errors.append({
                "site_name": site.get("name", site.get("id")),
                "message": f"{type(e).__name__}: {e}",
            })

    return homeworks, errors, need_login


def login_site(site_id):
    """触发某站点的登录流程，返回 True/False"""
    site = get_site(site_id)
    if not site:
        return False
    adapter = REGISTRY.get(site.get("type"))
    if not adapter:
        return False
    return adapter.login(site, DATA_DIR)