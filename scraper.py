import os
import sys
import yaml
from adapters import REGISTRY
from adapters.base import LoginRequiredError

if getattr(sys, 'frozen', False):
    BASE = os.path.dirname(sys.executable)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))

SITES_FILE = os.path.join(BASE, "sites.yaml")

def load_sites(include_disabled=False):
    if not os.path.exists(SITES_FILE):
        return []
    with open(SITES_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    sites = data.get("sites", [])
    if include_disabled:
        return sites
    return [s for s in sites if s.get("enabled", True)]


def save_sites(sites):
    with open(SITES_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            {"sites": sites}, f,
            allow_unicode=True, sort_keys=False, default_flow_style=False,
        )


def get_site(site_id):
    for s in load_sites(include_disabled=True):
        if s["id"] == site_id:
            return s
    return None


def fetch_all():
    homeworks = []
    errors = []
    need_login = []
    for site in load_sites():
        adapter = REGISTRY.get(site.get("type"))
        if not adapter:
            errors.append({
                "site_name": site.get("name", site.get("id")),
                "message": f"未知类型 {site.get('type')}",
            })
            continue
        try:
            items = adapter.fetch_homeworks(site, BASE)
            homeworks.extend(items)
        except LoginRequiredError:
            need_login.append({"site_id": site["id"], "site_name": site["name"]})
        except Exception as e:
            errors.append({
                "site_name": site.get("name", site.get("id")),
                "message": f"{type(e).__name__}: {e}",
            })
    return homeworks, errors, need_login


def login_site(site_id):
    site = get_site(site_id)
    if not site:
        return False
    adapter = REGISTRY.get(site.get("type"))
    if not adapter:
        return False
    return adapter.login(site, BASE)