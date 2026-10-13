# ticket-radar 设计规范 v19「白色灵动」

> 第十九轮回归白色灵动（第七轮 ins 高级风的精修版），废除全部手账元素。
> 改任何页面样式前先读这份。

## 0. 设计原则

- 淡灰白底 `#F7F7FA` + 纯白卡 `#FFFFFF`，分区靠**柔和阴影 + 大留白**，不靠描边；
- 近黑 `#1C1C1E` 是唯一主色（主 CTA / 选中态 / tabs 下划线 / 大数字）；
- 彩色只留给语义：绿=有票/在售，灰=无票/未开售，红=售罄/异常，琥珀=额度/公告提醒；
- **大圆角 22px（--r）是「灵动」的核心**，配柔和双阴影 `0 2px 8px / 0 10px 24px`；
- 克制线性图标，无插画、无手账元素；
- **全部文字系统默认字体：零 `font-family` 声明（硬规则）**；
- 标题类一律单行（white-space: nowrap），折行 = 排版事故；
- 拼贴只留在两处：开屏纯拼贴、页面背景层（bg-stk 透明度 0.06~0.12）。

### 0.1 去 AI 味硬规则（第十八轮立，第十九轮继续有效）

- 禁英文装饰小注（不写 WATCHING NOW / ADMIT ONE / ON SALE 这类翻译腔双语排版）；
- 禁 ✦ 类符号装饰；禁蝴蝶结；禁带英文字的贴纸素材；
- 禁矫情文案（「不错过每一次回流」这类嗲语气泡文案），功能说明一句话说清；
- 装饰做减法：每个视觉焦点最多一种装饰。

## 1. 设计令牌（定义在 app.wxss 的 page{}，直接用 var() 引用）

```
纸面: --paper #F7F7FA 页面底 | --paper-deep #F1F1F4 深一档底 | --card #FFFFFF 卡片 | --card2 #F7F7F8 卡内嵌块
墨:   --ink #1C1C1E 主文字 | --ink2 #5A5A60 次文字 | --ink3 #6C6C75 弱文字/占位
      --line #ECECEC 细线 | --line2 #E2E2E6 强分隔
主色: --accent #1C1C1E 近黑 | --accent-deep #000000 | --accent-weak #F1F1F4
语义: --stamp #C43C2C 红 | --stamp-weak #FCEBE9
      --warn  #8D6308 琥珀 | --warn-weak #FFF7E0
      (ok/none 走别名：--ok #237A45 / --ok-weak #E3F5EA / --none #6B6B73 / --none-weak #F1F1F4)
形状: --r 22px(卡片/按钮) | --r-sm 16px(卡内嵌块) | --r-pill 999px(功能) | --gut 12px
其他: --on-brand #FFFFFF | --disabled #E2E2E6 | --bar-idle #E5E5EA
阴影: --shadow 0 2px 8px rgba(28,28,30,.05), 0 10px 24px rgba(28,28,30,.04)
      --shadow-brand 0 4px 14px rgba(28,28,30,.18)
```

旧令牌别名（--bg/--brand/--t1/--ok/--bad/--none/--warn 等）全部指向白色灵动色板，页面里两种名字都能用；**新代码用新名**。

对比度：`python tools/check_palette.py`，19 项 WCAG AA 全过（改色板必须重跑）。
琥珀从 #9B6E0A 加深到 #8D6308 才在 #F7F7FA 上过 4.5（4.24 → 5.01）。

## 2. 公共组件（app.wxss 已定义，直接用 class）

| class | 用法 |
|---|---|
| `.card` / `.paper` | 纯白卡：大圆角 + 柔和阴影，无描边 |
| `.tag` + `.tag-ok/.tag-none/.tag-bad/.tag-warn/.tag-brand` | 状态标签：实心弱色药丸 |
| `.btn` + `.btn-primary/.btn-ghost/.btn-line` | 按钮：药丸形，主按钮近黑实心 |
| `.empty` + `.empty-mark/.empty-title/.empty-sub` | 空状态（必须给下一步动作） |
| `.sheet` + `.sheet-grab/.sheet-title/.sheet-sub` | 底部弹层：白面 + 20px 顶部圆角 |
| `.input` / `.input-pick` / `.field` / `.field-lbl` | 表单：浅灰底圆角输入框 |
| `.sec` + `.sec-title` + `.sec-more` | 分区标题 |
| `.group` / `.g-row` | inset 分组列表（设置类页面） |
| `.sk` | 骨架屏（呼吸脉冲） |
| `.bg-collage` + `.bg-stk` + `.bg-faint/.bg-soft/.bg-visible` | 背景拼贴层（父级 .page 需 position:relative） |
| `.press` / `.press-hover` / `.press-card` / `.press-card-hover` | 按压反馈（可点元素必须带） |
| `.num` / `.t1` / `.t2` / `.t3` / `.title-strong` | 文字层级 |

**已废除**（第 19 轮，wxml/wxss 均不得引用）：`.tape`（胶带）、`.stamp`（印章）、
`.perf`（打孔线）、`.barcode`（条码）、`.lace`（蕾丝线）、`.stitch-tag`（绣字）、
`.bow`（蝴蝶结）、`.spark`（✦）、`.en-note`（英文小注）、`.bg-tint`（Y2K 光晕）。
唯一例外：开屏页 splash.wxss 内有 `.tape/.perf/.barcode` 的**局部定义**（拼贴自足），
只许在 splash 里用。

## 3. Vant Weapp（已 vendor 到 `miniprogram/vant/`，共 19 个组件目录）

vant 组件开了 `addGlobalClass: true`，app.wxss 里的 `page .van-*` 选择器已做白色灵动主题覆盖（tabs/toast/dialog/overlay/hairline）。**禁止修改 `miniprogram/vant/` 里任何文件。**

接入模式（每个页面按需复制）：

```json
// page.json
"usingComponents": {
  "van-tabs": "/vant/tabs/index",
  "van-tab": "/vant/tab/index",
  "van-toast": "/vant/toast/index",
  "van-dialog": "/vant/dialog/index"
}
```
```xml
<!-- wxml 末尾放反馈节点 -->
<van-toast id="van-toast" />
<van-dialog id="van-dialog" />
```
```js
// js 顶部
import Toast from '/vant/toast/toast';
import Dialog from '/vant/dialog/dialog';
// 用法：Toast('文字') / Toast({ type: 'success', message: '文字' })
// Dialog.confirm({ title, message, confirmButtonText }).then(() => {}).catch(() => {})
// Dialog.alert({ title, message })
```

van-tabs 切换（内容放在 tabs 外面，tabs 只当切换条用）：

```xml
<van-tabs active="{{currentTab}}" bind:change="onTab" swipeable animated
  color="#1C1C1E" title-active-color="#1C1C1E" title-inactive-color="#6C6C75"
  line-width="26px" line-height="3px">
  <van-tab wx:for="{{tabs}}" wx:key="key" title="{{item.title}}"></van-tab>
</van-tabs>
```

注意：`bind:change` 的事件是 `e.detail.index`（不是 dataset）。改造旧 handler 时做双签名兼容：

```js
onTab(e) {
  const idx = e.detail && typeof e.detail.index === 'number'
    ? e.detail.index
    : Number(e.currentTarget.dataset.tab);
  ...
}
```

已接入的页面：shows（平台 tabs）、discussion（分类 tabs）、monitor/detail/discussion/mine（toast/dialog）。

## 4. 海报链路（第 19 轮修）

数据流：平台接口 → adapter 掏字段 → **`normalizePosters` 出口统一** → 小程序 `<image>`。

- 出口规范化（radar/index.js 末尾 `exports.main`）：协议相对 `//host/…` 补 `https:`、`http://` 升 `https:`、空值/非字符串转 `''`；递归覆盖 `poster/imgUrl/verticalPic/cover` 四个字段名的所有层级；
- 实测各平台海报字段：猫眼 `posterUrl`（p0.pipi.cn）、票星球 `posterUrl`（cdn.ticketmars.com）、B站 `cover`（//i2.hdslb.com，协议相对）、大麦 `posterUrl`；
- **小程序后台必须配 downloadFile 合法域名**（清单见 miniprogram/README.md 第 4.5 节），不配则全部裂图——这是用户侧配置，代码替代不了。

## 5. 各页面现状与改版要求

通用要求（所有页面）：
1. 不得引用已废除的手账组件（见第 2 节）；
2. 删除旧页面 wxss 里已被 app.wxss 取代的重复定义时，注意只删确实重复的；
3. 所有 `wx.showToast` / `wx.showModal` 用 vant Toast / Dialog（见上）；
4. page.json 的 `navigationBarBackgroundColor` 用 `#F7F7FA`；
5. **不得改动任何数据绑定字段名、事件 handler 名、js 业务逻辑**（Toast/Dialog 替换与海报规范化除外）；
6. 不得新增 `font-family`；不得引用 app.wxss 里不存在的 var()；
7. 遵守 0.1 节去 AI 味硬规则；
8. hero 挂件贴纸每处最多 1 张。

### splash（开屏：纯拼贴零文字）
- 整个屏幕无任何文字；图形自己说话；
- 结构：淡灰白底 → 手撕条（纯图形 + 锯齿撕口）→ 打孔线（局部定义）→ 主视觉拼贴区 → 打孔线 → 底部 dock；
- 主视觉：大折纸星（origami11，视觉重心偏左上）+ 手绘星轨（cartoon10，右上）+ 小黄星（origami22，左下）+ 小红星（origami8，右中）+ 两片手剪胶带；不对称构图，留白 ≥ 60%；
- 底部 dock：条码（局部定义，纯图形）+ 圆形纸片按钮（card 底 + 墨色细边 + 折纸星贴纸 + 斜压胶带）；
- 贴纸物理感：`.stk-in` 加 `filter: drop-shadow(0 3rpx 6rpx rgba(28,28,30,.10))`；
- 动效：撕条/打孔线/条码淡入；贴纸逐张「贴上」（外层定位+旋转，内层 scale 动画），全部 1.5s 内完成；
- 交互不变：整屏 bindtap 进入 + 2.4s 自动进入 + 24h 内不再展示（splash.js 不动）。

### monitor（首页，tab 第 1 页）
- hero：标题「正在盯的票」+ 大数字 okCount/total + 加监控按钮（accent 实心药丸）；hero 挂件贴纸仅 1 张；
- quick-grid 四个快捷入口：白卡片，线性图标近墨色；
- segs 筛选：药丸容器，选中 = accent 实心；
- credit 额度条：白卡，空额度时 warn-weak 底；
- task-card：白卡 + 顶部 4px 状态线（ok=accent / none=bar-idle / bad=stamp）+ 虚线 foot 分隔；
- sheet 加监控弹层用 app.wxss 的纸面样式；
- 分享画布 `#shareCanvas` 相关 js 一律不动。

### detail（演出详情）
- 大海报 + 名称 + 平台/城市/票价信息区；
- 场次列表：白卡，场次间距分隔（打孔线已废）；
- 票档：名称 + 价格 + 状态（`.tag`）；
- 底部「盯这场」：accent 大按钮；
- toast/dialog 接入。

### discussion（反馈/讨论区）
- 分类 tabs 用 van-tabs，`onTab` 双签名兼容；
- 留言墙便签保留现有 note 图素材（便签是功能不是装饰）；
- 头部「问题反馈」+ 茶绿小短线；挂件贴纸仅 1 张；
- 搜索栏、fab 发帖按钮（accent）、发帖弹层（.sheet）、放大查看便签；
- toast/dialog 接入；
- 发帖可选便签素材 10 种（stickers 数组），全部不含英文字贴纸。

### history（余票历史） / inbox（消息）
- 列表白卡；空状态/失败态用 app.wxss 公共类；toast 接入。

## 6. 自检清单（改完必须逐项过）

1. `grep -rn "font-family" miniprogram/miniprogram` 零命中；
2. 页面里所有 `var(--xxx)` 都能在 app.wxss 找到定义；
3. `grep -rn 'class="[^"]*\bbow\b\|en-note\|spark-lg\|quota-bow\|search-pin' miniprogram/miniprogram` 零命中；
4. 手账组件页面残留零命中（stamp/perf/lace/stitch-tag/stitch-wrap，splash 局部定义除外）；
5. `grep -rn 'collage13\|collage16\|collage8.png' miniprogram/miniprogram` 零命中（带英文字的贴纸禁用）；
6. `grep -rn 'WATCHING NOW\|FIND SOMETHING\|ADMIT ONE\|ON SALE\|SESSIONS\|FEEDBACK' miniprogram/miniprogram` 零命中；
7. js 里没有残留 `wx.showToast` / `wx.showModal`；
8. `python tools/check_palette.py` 全过；
9. page.json 有正确的 usingComponents 与 #F7F7FA 导航色；
10. 事件 handler 名与数据字段名与原版一致；
11. wxml 标签配对、wxss 大括号配对、页面 js `node --check` 全过。
