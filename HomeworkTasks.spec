# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配方（onedir）。

★ 这个文件**必须入库**
    README 的「打包」章节让用户执行 `pyinstaller HomeworkTasks.spec`。
    早期 `.gitignore` 里有 `*.spec`，配方没进仓库 —— clone 之后按 README 执行必然失败，
    发布流程不可复现，而 `datas` / `hiddenimports` / `console` 这些决定包能不能跑的
    设置只留在本机。v26.5 起本文件纳入版本控制。

★ 打进去的东西（只有运行必需的）
    web/                 三个页面 + 样式 + 脚本 + 字体（UI 的全部资源）
    sites.yaml.template  首次启动生成 sites.yaml 用的模板
    playwright 整包      collect_all 收进来的 driver 与 node 运行时，抓取必需

★ 刻意**不**打进去
    sites.yaml / sites.yaml.bak   真实配置（含 DPAPI 密文账密）
    data/                         会话文件、homework.db、课程缓存
    docs/、Examine/、PRODUCT.md   文档
    任何 *.log / *.db / *.bak     日志与备份
    icon.ico                      运行时没有任何代码读它；它只被下面的 icon= 用作
                                  EXE 图标，没必要在包里再放一份副本
"""
from PyInstaller.utils.hooks import collect_all

# ---------------------------------------------------------------- 资源
datas = [
    ("web", "web"),                  # 页面 + CSS + JS + 字体
    ("sites.yaml.template", "."),    # 首次启动生成 sites.yaml
]

# ---------------------------------------------------------------- 隐式依赖
# hiddenimports **不递归**，core/adapters 的子模块目前靠显式 import 被 modulegraph
# 捕获。一旦有人改成字符串动态加载（importlib.import_module），冻结包会静默缺模块，
# 所以这里把子模块逐个写死。
hiddenimports = [
    "adapters",
    "adapters.auth",
    "adapters.base",
    "adapters.learn",
    "adapters.smartestu",
    "adapters.thuoj",
    "adapters.tyche",
    "adapters.unipus",
    "adapters.yuketang",
    "core",
    "core.controller",
    "core.database",
    "core.float_place",
    "core.paths",
    "core.secrets",
    "core.site_runner",
    "core.task_logic",
]

# playwright：driver + node 运行时，缺了就没法抓取
_tmp = collect_all("playwright")
datas += _tmp[0]
binaries = list(_tmp[1])
hiddenimports += _tmp[2]

# 剔除只有开发期才用的类型声明：playwright 的 driver 里带 4 个 .d.ts
# （index/protocol/structs/types，实测 1.86 MB），只有编辑器与 TypeScript 使用者会读，
# Python 运行时不会碰它们。LICENSE / NOTICE / ThirdPartyNotices 一律保留（第三方归属）。
#
# 注意这里要过滤两次：PyInstaller 自带 hook-playwright，它会在 Analysis 阶段**再收一遍**
# playwright 的 driver 数据，所以只过滤下面这份 `datas` 是没用的 —— 关键是 Analysis 之后
# 过滤 `a.datas`（COLLECT 消费的就是它）。
datas = [d for d in datas if not str(d[0]).lower().endswith(".d.ts")]

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# hook-playwright 收进来的数据在 a.datas 里 —— 在这里剔除开发期类型声明
_before = len(a.datas)
a.datas = [d for d in a.datas if not str(d[0]).lower().endswith(".d.ts")]
_filtered = _before - len(a.datas)
if _filtered:
    print(f"[spec] 已剔除 {_filtered} 个 .d.ts 类型声明（开发期文件，运行时不需要）")

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HomeworkTasks",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX 保持关闭：本机没装 UPX 时 upx=True 只是空操作，可一旦构建机装了 UPX，
    # 它会把 webview/lib 下的 .NET 二进制也压一遍（upx_exclude 为空）。
    # 收益不确定，风险是加载失败/杀软误报 —— 要开就显式开，并显式排除那批二进制。
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["icon.ico"],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="HomeworkTasks",
)
