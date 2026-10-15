import os
from datetime import datetime
from playwright.sync_api import sync_playwright
from .base import Homework, LoginRequiredError, FetchFailedError, session_path

# 能力位：雨课堂只能微信扫码，没有「填账号密码 → 后台静默登录」这条路。
# 放在适配器模块上，是为了让「哪些平台不能自动登录」只有一处定义：
# 设置面板的按钮（core/controller.list_sites）、需要登录条目的原因
# （core/site_runner._need_login_entry）、后台重登调度（auto_relogin_site）都读它。
# 以前这三处各自硬编码 `type != "yuketang"` / `type == "yuketang"`。
SUPPORTS_SILENT_LOGIN = False


def _ts_to_str(ts):
    """毫秒时间戳 → "2026-09-29 23:59:00" """
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _fetch_courses(page):
    """调接口拿当前用户的所有课程"""
    js = """async () => {
        try {
            const r = await fetch('/v2/api/web/courses/list?identity=2', {credentials: 'include'});
            if (!r.ok) return {status: 'http_error', code: r.status};
            return await r.json();
        } catch(e) { return {status: 'fetch_error', message: e.toString()}; }
    }"""
    return page.evaluate(js)


def _fetch_activities(page, classroom_id):
    """拿一门课的全部活动（含作业、习题、试卷）"""
    js = """async (cid) => {
        const url = '/v2/api/web/logs/learn/' + cid
                    + '?actype=-1&page=0&offset=500&sort=-1';
        try {
            const r = await fetch(url, {credentials: 'include'});
            if (!r.ok) return {status: 'http_error', code: r.status};
            return await r.json();
        } catch(e) { return {status: 'fetch_error', message: e.toString()}; }
    }"""
    return page.evaluate(js, str(classroom_id))


def _extract_courses(data):
    """从课程列表接口返回里提取 (id, name)"""
    result = []
    if not isinstance(data, dict):
        return result
    for c in (data.get("data") or {}).get("list") or []:
        cid = c.get("classroom_id")
        name = (c.get("course") or {}).get("name") or c.get("name") or ""
        if cid:
            result.append({"id": cid, "name": name})
    return result


def _extract_tasks(data, site, course_name="", classroom_id=""):
    """从活动列表里提取作业 / 习题 / 试卷
    通过 type 区分：
        19 = 作业（截止时间在 content.score_d）
        5  = 考试（截止时间在 deadline）
        4  = 试卷（截止时间在 deadline）
        14 = 教学，跳过
    """
    result = []
    if not isinstance(data, dict):
        return result

    acts = (data.get("data") or {}).get("activities") or []
    for a in acts:
        t = a.get("type")
        title = a.get("title") or ""
        end = ""

        if t == 19:
            # 作业
            end = _ts_to_str((a.get("content") or {}).get("score_d"))
            title = "【作业】" + title
        elif t == 5:
            # 考试
            end = _ts_to_str(a.get("deadline"))
            title = "【考试】" + title
        elif t == 4:
            # 试卷
            end = _ts_to_str(a.get("deadline"))
            title = "【试卷】" + title
        else:
            continue

        # 缺 id 就直接跳过：以前会退化成 "yuketang:None"，
        # 于是多条任务共用一个 uid，「已完成」勾选互相串、done 表按 uid 互相覆盖。
        aid = a.get("id")
        if aid is None or not end or not title:
            continue

        # 链接优先用活动自带的 classroom_id，缺了就用调用方传进来的课程 id。
        # 以前只取活动字段，缺了会得到 .../studentLog//123 这种死链。
        cid = a.get("classroom_id") or classroom_id
        result.append(Homework(
            site=site["id"],
            site_name=site["name"],
            uid=f"{site['id']}:{aid}",
            title=title,
            course=course_name,
            start=_ts_to_str(a.get("create_time")),
            end=end,
            url=f"https://pro.yuketang.cn/v2/web/studentLog/{cid}/{aid}",
        ))
    return result


def fetch_homeworks(site, base_dir):
    """静默抓取：先拿课程列表，再逐门课拿活动。

    雨课堂要微信扫码，没有会话时直接报需要登录，不浪费一次浏览器启动。
    """
    from .auth import browser_session, wait_for_ready, guard_in_page_fetch

    session = session_path(base_dir, site)
    if not os.path.exists(session):
        raise LoginRequiredError(site["id"], site["name"])

    with browser_session() as browser:
        # 用共享的 browser_session()：它有 finally: browser.close()。
        # 以前这里是裸 sync_playwright()，goto/等页面超时就直接冒出去，
        # 每次刷新残留一个 msedge 进程。
        ctx = browser.new_context(storage_state=session)
        try:
            page = ctx.new_page()
            guard_in_page_fetch(page)   # 页面内 fetch 超时保护，见 auth.guard_in_page_fetch
            try:
                page.goto(site["base_url"], wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                raise FetchFailedError(site["id"], site["name"],
                                       f"打开站点失败：{str(e)[:120]}")

            # 不用 wait_for_load_state("networkidle")：它要求网络彻底安静 500ms，
            # 而这类 SPA 一直有轮询请求，实测会一路等到超时。
            wait_for_ready(page, lambda pg: "login" not in (pg.url or "").lower(),
                           timeout_ms=8000, what="雨课堂主页面")
            page.wait_for_timeout(1500)

            if "login" in (page.url or "").lower():
                raise LoginRequiredError(site["id"], site["name"])

            # 1) 拿课程列表
            data = _fetch_courses(page)
            if isinstance(data, dict) and data.get("status") in ("http_error", "fetch_error"):
                code = data.get("code")
                if code in (401, 403):
                    # 未登录时 SPA 有时不跳转、只让接口返回 401：
                    # 以前这会被 _extract_courses 变成空列表，用户永远看到
                    # 「雨课堂 完成 0 条」，等不到扫码提示。
                    raise LoginRequiredError(site["id"], site["name"])
                raise FetchFailedError(
                    site["id"], site["name"],
                    f"课程列表接口失败（{data.get('status')} "
                    f"{code or data.get('message') or ''}）".strip())

            courses = _extract_courses(data)
            if not courses:
                # 接口正常但一门课都没有：这是"没有课程"，不是"需要登录"
                return []

            # 2) 逐门课拿活动
            all_homeworks = []
            failed = []
            for c in courses:
                adata = _fetch_activities(page, c["id"])
                if isinstance(adata, dict) and adata.get("status") in ("http_error", "fetch_error"):
                    code = adata.get("code")
                    if code in (401, 403):
                        raise LoginRequiredError(site["id"], site["name"])
                    # 单门课接口失败不该拖垮整站
                    failed.append(c.get("name") or c["id"])
                    continue
                all_homeworks.extend(
                    _extract_tasks(adata, site, c.get("name", ""), c["id"]))
            if failed:
                print(f"[yuketang] {len(failed)} 门课的活动接口失败，已跳过："
                      + "、".join(failed[:3]))
            return all_homeworks
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def login(site, base_dir):
    """弹窗让用户扫码登录"""
    os.makedirs(base_dir, exist_ok=True)      # 运行期 data/ 被删时不再直接抛 FileNotFoundError
    session = session_path(base_dir, site)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="msedge")
        try:
            ctx = browser.new_context()
            page = ctx.new_page()
            page.goto(site["base_url"])

            logged_in = False
            # 「接口答对了但是空列表」的连续次数。不能一看到 200 + data 就判成功：
            # 未登录时这个接口有可能回 200 且 body 形如 {code,…,data:{list:[]}}，
            # 那样第一次轮询（3 秒）就会把**匿名会话**写进文件、覆盖原本可用的会话，
            # 用户看到"登录成功"但刷新后仍然要登录。
            empty_ok_polls = 0
            # 扫码可能要挺久，最多等 10 分钟
            for _ in range(200):
                page.wait_for_timeout(3000)
                try:
                    if "login" in (page.url or "").lower():
                        continue
                    data = _fetch_courses(page)
                    if not isinstance(data, dict) or data.get("status"):
                        continue
                    # 判据一：真的拿到了课程 —— 唯一无歧义的成功信号
                    if _extract_courses(data):
                        logged_in = True
                        break
                    # 判据二：接口以"正确的形状"回了空列表。这可能是真的没选课，
                    # 也可能是未登录的空壳，所以要求连续 20 次（≈60 秒）都是这样
                    # 才接受：正常扫码几十秒内就会出课程，而空壳会一直空。
                    d = data.get("data")
                    if isinstance(d, dict) and "list" in d:
                        empty_ok_polls += 1
                        if empty_ok_polls >= 20:
                            logged_in = True
                            break
                except Exception:
                    pass

            if logged_in:
                try:
                    ctx.storage_state(path=session)
                    logged_in = os.path.exists(session) and os.path.getsize(session) > 0
                except Exception as e:
                    print(f"[yuketang] 会话存盘失败：{type(e).__name__}: {e}")
                    logged_in = False
            return logged_in
        finally:
            try:
                browser.close()
            except Exception:
                pass
