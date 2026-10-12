## Direction contract

THESIS: 主窗口是一张摊开的借阅凭条：任务在左，「到期」在右由一枚戳印说话。它拒绝的类别默认是「白色卡片 + 蓝色强调 + 图标徽标」的待办列表。

OWN-WORLD: 墨蓝黑 `#1b2430` 的顶栏与 1px 分隔线；米白卡纸底 `#efe9dd`、凭条面 `#fffdf7`；正文墨色 `#1b2430`、次要 `#6d6659`；朱红 `#a4262c` 只用于到期戳与作废，绝不做装饰。等宽数字一律 Consolas + `tabular-nums`。圆角只有 2px 一档。字号固定阶 11/12/13/15/19/22，不用流体字号。

STORY: 用户一眼读到「最近到期在什么时候、哪几条已经过期」，然后勾选完成或点标题用系统浏览器打开原网页。完成不是消失，而是被盖上一枚「已还」印。

FIRST VIEWPORT: 1000×720。顶部 56px 墨蓝纸条：左「作业清单」+ 最近到期，右侧四个按钮（「添加任务」为主）；其下 46px 凭条头：未完成/已完成与项数；主体 574px 是凭条面，每行 64px —— 24px 方框、15px 标题、下方课程/站点的 1px 描边小牌与截止时间，行尾 1px 虚线剪切线，右侧 138px 戳位（等宽数字 21px；紧急与过期加 1px 朱红描边并旋转 -1.4°）；底部 44px 凭条尾：共 N 项 / 未完成 / 数据目录。

FORM: 派生清单第 6 位「借阅凭条与到期日戳」（第 1 位周课表格作为 IMPECCABLE'S PICK 卡呈现）。seed key 165a6530。四名 declined 挑战者各捐一条纪律：等宽数字列为主节奏（ikeda-datamatics）、完成靠盖印作废而非删除（jet-age-ticket-wallet）、固定底层色语言 + 单一当前层（orienteering-map）、每行自带固定附属信息块（character-goods-catalog）。

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
