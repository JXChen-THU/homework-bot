"""Pure task date parsing and floating-list classification."""
import re
from datetime import datetime

# 红窗（紧急浮窗）只保留"最近过期"的任务：更早的历史遗留降到白窗，
# 否则一条几个月前就该交的作业会永久占据红窗前几行，把真正 24 小时内
# 到期的任务挤下去。注意是**降级到白窗**，不是从界面删除。
URGENT_PAST_GRACE_HOURS = 72.0
URGENT_HORIZON_HOURS = 24.0

_LOOSE_RE = re.compile(
    r"^(\d{4})-(\d{1,2})-(\d{1,2})"
    r"(?:[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?"
)


def _from_timestamp(num):
    """数字当成时间戳：13 位及以上按毫秒，其余按秒。"""
    seconds = float(num) / 1000.0 if abs(num) >= 1e11 else float(num)
    return datetime.fromtimestamp(seconds)


def _to_naive_local(dt):
    """带时区的 datetime 先换算成本机时区再去掉 tzinfo。

    必须做这一步：`compute_*` 里的 `now` 是 naive 的，naive 与 aware 相减
    会直接抛 TypeError —— 那会把"没有倒计时"升级成"整轮渲染崩掉"。
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _loose_parse(text):
    """兜底：容忍非零填充的月/日/时（`2026-1-2 3:4`）。"""
    m = _LOOSE_RE.match(text)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh = int(m.group(4) or 0)
    mi = int(m.group(5) or 0)
    ss = int(m.group(6) or 0)
    try:
        return datetime(y, mo, d, hh, mi, ss)
    except ValueError:
        return None


def parse_time(value):
    """把各来源的截止时间解析成本机 naive datetime；解析不了返回 None。

    真实见过的输入形态（各适配器直接把接口字符串透传过来）：
        `2026-01-02 03:04:05`、`2026-01-02 03:04`、`2026-01-02`
        `2026-01-02T03:04:05`、`2026-01-02T03:04:05.123`
        `2026-01-02T03:04:05+08:00`、`...Z`、`2026/1/2`、`2026-1-2 3:4`
        毫秒/秒时间戳（数字，或 9 位以上的纯数字字符串）
    非字符串且非数字的输入（None、list、dict…）一律返回 None 而不是抛异常：
    渲染路径上任何一条脏数据都不该让整轮任务列表失败。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return _from_timestamp(value)
        except Exception:
            return None
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    # 纯数字字符串：9 位以上当时间戳，避免把 "2026" 这种当时间戳
    if text.isdigit() and len(text) >= 9:
        try:
            return _from_timestamp(int(text))
        except Exception:
            pass

    text = text.replace("/", "-").strip()

    # 1) 先试严格格式：覆盖绝大多数，且不受本地化影响
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    # 2) 宽松 ISO：处理小数秒、时区偏移、Z、非零填充之外的写法
    dt = None
    try:
        dt = datetime.fromisoformat(text.replace(" ", "T", 1))
    except ValueError:
        dt = None

    # 3) 最后兜底：手工容忍非零填充
    if dt is None:
        dt = _loose_parse(text)

    if dt is None:
        return None
    return _to_naive_local(dt)


def compute_urgent(items, now=None):
    """紧急列表：已过期（在宽限期内）到 24 小时内到期，按剩余时间升序。"""
    now = now or datetime.now()
    result = []
    for item in items:
        if item.get("done"):
            continue
        due = parse_time(item.get("end"))
        if due is None:
            continue
        hours_left = (due - now).total_seconds() / 3600
        if -URGENT_PAST_GRACE_HOURS <= hours_left <= URGENT_HORIZON_HOURS:
            result.append({"uid": item["uid"], "title": item["title"], "hours_left": hours_left})
    # 次级键用 uid：截止时间相同的任务每次刷新都保持同一顺序，列表不会自己跳
    return sorted(result, key=lambda item: (item["hours_left"], item["uid"]))


def compute_normal(items, urgent, now=None):
    """普通列表：所有不在紧急列表里的未完成任务，按截止时间升序。"""
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
    return sorted(result, key=lambda item: (item["end"] or "9999", item["uid"]))
