# ============================================================================
# 浮窗定位
# ----------------------------------------------------------------------------
# ★ 为什么要有这个函数（修"开机时先闪一下左侧再跳到右下"）：
#   浮窗用 hidden=True 创建，但 Windows 上 pywebview 的 hidden 是
#   "Opacity=0 → Show() → Hide()"（见 controller 里 create_window 处的注释），
#   也就是说窗口**真的被显示过一次**。如果此时还没 move()，它会先按系统默认
#   位置（左上）画一帧，之后才被挪到右下 —— 那一闪就是这么来的。
#   所以在 show() 之前先把 resize/move 做好。
#
# ★ 为什么不自己缓存位置（我第一版就是这么写的，结果把「重置浮窗」弄坏了）：
#   缓存里留的是"我们上次设的目标位置"，而用户可能**手动拖走**过窗口。
#   再点「重置浮窗」时目标位置与缓存里的目标位置相同 → 判定为"没变" → 不 move，
#   窗口就永远回不到右上角了。改成读 Window.x / Window.y / width / height
#   这几个只读属性拿**真实位置**，与目标比较，缓存彻底不需要了。
#
# ★ 关于阻塞：pywebview 的 x / y 内部会 events.shown.wait(15)。但 winforms 后端
#   在 hidden 与 transparent 两条创建路径上都会调一次 Show()，而 Show() 会触发
#   on_shown → events.shown.set()，所以窗口创建后这两个属性是立刻可读的。
#
# ★ 单独成模块只为一件事：**可以被测试**。放在 controller.py 里就得 import webview
#   才能测一行纯逻辑。见 design-verify/liquid-glass/verify-float-place.js。
# ============================================================================


def place_float(win, w, h, x, y):
    """把浮窗摆到 (x, y) 且尺寸为 (w, h)。只在与**当前真实几何**不同时才动窗口。

    返回 (x, y)。win 为 None 时原样返回，方便调用点不做空判断。
    """
    if win is None:
        return (x, y)

    try:
        cur_w = getattr(win, 'width', None)
        cur_h = getattr(win, 'height', None)
        need_size = (cur_w is None) or (cur_h is None) or cur_w != w or cur_h != h
    except Exception:
        need_size = True

    try:
        need_pos = hasattr(win, 'move') and (win.x != x or win.y != y)
    except Exception:
        # ★ 读不到当前位置时**宁可移动**：否则一旦读失败，窗口就再也回不到右上角
        #   （这正是「重置浮窗」失效那一类问题）。多移动一次的代价只是闪烁风险。
        need_pos = hasattr(win, 'move')

    try:
        if need_size:
            win.resize(w, h)
        if need_pos:
            win.move(x, y)
    except Exception:
        # 摆位失败不该影响数据刷新；下次刷新还会再试
        pass

    return (x, y)
