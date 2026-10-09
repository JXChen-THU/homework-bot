"""Pure task date parsing and floating-list classification."""
from datetime import datetime


def parse_time(value):
    if not value:
        return None
    value = value.strip().replace("T", " ").replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def compute_urgent(items, now=None):
    now = now or datetime.now()
    result = []
    for item in items:
        if item.get("done"):
            continue
        due = parse_time(item.get("end"))
        if due is None:
            continue
        hours_left = (due - now).total_seconds() / 3600
        if hours_left <= 24:
            result.append({"uid": item["uid"], "title": item["title"], "hours_left": hours_left})
    return sorted(result, key=lambda item: item["hours_left"])


def compute_normal(items, urgent, now=None):
    now = now or datetime.now()
    urgent_uids = {item["uid"] for item in urgent}
    result = []
    for item in items:
        if item.get("done") or item["uid"] in urgent_uids:
            continue
        due_text = item.get("end") or ""
        due = parse_time(due_text)
        result.append({"uid": item["uid"], "title": item["title"], "end": due_text,
                       "hours_left": (due - now).total_seconds() / 3600 if due else None})
    return sorted(result, key=lambda item: item["end"] or "9999")
