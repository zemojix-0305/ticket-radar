# ticket-radar 设计规范 v18「纸感票根手账 · 纯拼贴开屏」

> 第十八轮在 v17 基础上做「去 AI 味、去土气」减法：删英文装饰小注、删 ✦ 符号、删蝴蝶结、开屏改纯拼贴零文字。
> 参考：北港茶小程序（暖白期刊纸 + 手绘细线 + 大量留白 + 胶带拼贴）+ 复古票根（打孔线/副券/条码/印章）。

## 0. 设计原则（taste-skill 规则，按本项目情境化）

- DESIGN_VARIANCE 8：不对称拼贴、微倾斜胶带、便签微旋转；
- MOTION 5：只做克制的入场与按压反馈，缓动 `cubic-bezier(0.22, 1, 0.36, 1)`，禁 `transition: all`；
- DENSITY 4：日常工具密度，留白透气；
- 单一强调色（茶绿）+ 语义色（朱砂=热卖/回流/售罄，琥珀=额度提醒）；
- 禁纯黑 #000 与纯白 #fff（墨色 #2A2620 / 纸白 #FDFBF4）；
- 形状系统全站统一：**纸 = 裁纸直角 2px（--r），功能 = 药丸 999px（--r-pill）**；
- 阴影一律暖墨调 `rgba(42,38,32,.07)`，禁纯黑投影；
- **全部文字系统默认字体：零 `font-family` 声明（硬规则）**；
- 标题类一律单行（white-space: nowrap），折行 = 排版事故；
- 拼贴素材透明度 0.06~0.12，只在留白处透出，不压文字。

### 0.1 去 AI 味硬规则（第十八轮新增，用户明确反馈后立）

- **禁英文装饰小注**：不写 `WATCHING NOW` / `FIND SOMETHING TO WATCH` / `ADMIT ONE` / `ON SALE` / `SESSIONS & TICKETS` 这类翻译腔双语排版。界面语言只有中文，状态词一律中文（在售/售罄/预售中/缺票登记）；
- **禁 ✦ 类符号装饰**：不当列表 dot、不当标题点缀、不当 sparkle 用；
- **禁蝴蝶结**：coquette 残留，最显俗的元素，已从 app.wxss 删除，任何页面不得新增；
- **禁带英文字的贴纸素材**：collage13（LOVE×3）、collage16（YOU ME）、collage8（英文句子）已全部替换为折纸星星/手绘线条类；
- **禁矫情文案**：不写「不错过每一次回流」「点个 Star 就是最大的鼓励」「我们会持续替你看着」这类嗲语气泡文案，功能说明一句话说清；
- **装饰做减法**：每个视觉焦点最多一种装饰（一片胶带 或 一张贴纸），不堆砌。

## 1. 设计令牌（定义在 app.wxss 的 page{}，直接用 var() 引用）

```
纸:  --paper #F7F2E7 页面底 | --paper-deep #EFE8D7 牛皮纸块 | --card #FDFBF4 卡片纸 | --card2 #F3EDDE 卡内嵌块
墨:  --ink #2A2620 主文字 | --ink2 #57503F 次文字 | --ink3 #6E6558 弱文字/占位
     --line #D9D0BC 纸边细线 | --line2 #C4B99F 打孔线/强分隔
茶绿(唯一强调色): --accent #445C38 | --accent-deep #36492C | --accent-weak #E9EDDF
朱砂(语义): --stamp #A23B2E | --stamp-weak #F5E4E0
琥珀(语义): --warn #7E6118 | --warn-weak #F4EBD4
形状: --r 2px(纸) | --r-sm 2px | --r-pill 999px(功能) | --gut 12px
其他: --on-brand #FDFBF4(绿底上的纸白字) | --disabled #DCD5C4 | --bar-idle #EAE3D2
```

旧令牌别名仍有效（--bg/--brand/--t1/--ok/--bad/--none 等全部指向新色板），**新代码一律用新名**。

对比度（纸面 #F7F2E7 上）：ink 13.5:1 / ink2 7.3:1 / ink3 5.1:1 / accent 6.6:1 / stamp 5.9:1 / warn 4.5:1，全部过 WCAG AA。

## 2. 公共组件（app.wxss 已定义，直接用 class）

| class | 用法 |
|---|---|
| `.card` / `.paper` | 纸卡片：card 底 + 1px --line 边 + 2px 直角 |
| `.tag` + `.tag-ok/.tag-none/.tag-bad/.tag-warn/.tag-brand` | 纸签：药丸 + 纸边线（平台名等中性标签用裸 `.tag`） |
| `.stamp` + `.stamp-green/.stamp-red/.stamp-ink` | 印章：双线框微斜，盖状态（在售=green，售罄=red，未开售/无票=ink） |
| `.perf` / `.perf-on-card` | 打孔线：虚线 + 两侧半圆缺口（卡片内用 on-card 变体） |
| `.barcode` | 条码：硬边条纹（可再用 width/height 覆盖） |
| `.tape` + `.tape-red/.tape-kraft/.tape-wide` | 和纸胶带：半透明微斜，父容器需 position:relative |
| `.lace` | 蕾丝点线软分隔 |
| `.btn` + `.btn-primary/.btn-ghost/.btn-line` | 按钮：44px 高，2px 直角 |
| `.empty` + `.empty-mark/.empty-title/.empty-sub` | 空状态（必须给下一步动作） |
| `.sheet` + `.sheet-grab/.sheet-title/.sheet-sub` | 底部纸弹层 |
| `.input` / `.input-pick` / `.field` / `.field-lbl` | 纸面表单 |
| `.sec` + `.sec-title` + `.sec-more` | 分区标题 |
| `.bg-collage` + `.bg-stk` + `.bg-faint/.bg-soft/.bg-visible` | 背景拼贴层（父级 .page 需 position:relative） |
| `.sk` | 骨架屏 |
| `.press` / `.press-hover` / `.press-card` / `.press-card-hover` | 按压反馈（可点元素必须带） |

**已删除**：`.bg-tint` 系列（Y2K 光晕）、`.bow`/`.bow-k`（蝴蝶结）、`.spark`/`.spark-lg`（✦ 符号）、`.en-note`（英文小注）——wxml/wxss 均不得再引用。

## 3. Vant Weapp（已 vendor 到 `miniprogram/vant/`，共 19 个组件目录）

vant 组件开了 `addGlobalClass: true`，app.wxss 里的 `page .van-*` 选择器已做纸感主题覆盖（tabs/toast/dialog/overlay/hairline）。**禁止修改 `miniprogram/vant/` 里任何文件。**

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
  color="#445C38" title-active-color="#2A2620" title-inactive-color="#6E6558"
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

已接入的页面：shows（平台 tabs）、monitor/detail/discussion/mine（toast/dialog）。

## 4. 各页面现状与改版要求

通用要求（所有页面）：
1. wxml 删掉 `bg-tint` 节点，保留 `bg-stk`；页面 wxss 里 bg-* 定位类保留可微调；
2. 删除旧页面 wxss 里已被 app.wxss 取代的重复定义时，注意只删确实重复的；
3. 所有 `wx.showToast` / `wx.showModal` 换成 vant Toast / Dialog（见上）；
4. page.json 的 `navigationBarBackgroundColor` 改 `#F7F2E7`；
5. **不得改动任何数据绑定字段名、事件 handler 名、js 业务逻辑**（Toast/Dialog 替换除外）；
6. 不得新增 `font-family`；不得引用 app.wxss 里不存在的 var()；
7. 遵守 0.1 节去 AI 味硬规则（无英文小注/无✦/无蝴蝶结/无英文字贴纸/无矫情文案）；
8. 图标/贴纸继续用 `/images/board2/` 与 `/images/board/` 现有素材，不新增图片文件；
9. hero 挂件贴纸每处最多 1 张。

### splash（开屏，第十八轮重做：纯拼贴零文字）
- **整个屏幕无任何文字**：不写品牌名/口号/编号/按钮字/提示语，图形自己说话；
- 结构：暖白纸底 → 顶部手撕条（纯图形 + 底边锯齿撕口）→ 打孔线 → 主视觉拼贴区 → 打孔线 → 底部 dock；
- 主视觉：大折纸星（origami11，视觉重心偏左上）+ 手绘星轨（cartoon10，右上）+ 小黄星（origami22，左下）+ 小红星（origami8，右中）+ 两片手剪胶带；不对称构图，留白 ≥ 60%；
- 底部 dock：条码（纯图形，无编号）+ 圆形纸片按钮（card 底 + 墨色细边 + 折纸星贴纸 + 斜压胶带）；
- 动效：撕条/打孔线/条码淡入；贴纸逐张「贴上」（外层定位+旋转，内层 scale 动画，全部 1.5s 内完成）；
- 交互不变：整屏 bindtap 进入 + 2.4s 自动进入 + 24h 内不再展示（splash.js 不动）。

### monitor（首页，tab 第 1 页）
- hero：标题「正在盯的票」+ 大数字 okCount/total + 加监控按钮（accent，2px 直角，去掉蓝色阴影）；hero 挂件贴纸仅 1 张（origami12）；
- quick-grid 四个快捷入口：纸卡片，线性图标近墨色；
- segs 筛选：药丸容器，选中 = accent 实心；
- credit 额度条：纸卡，空额度时 warn-weak 底；
- task-card：纸卡 + 顶部 4px 状态线（ok=accent / none=bar-idle / bad=stamp）+ 虚线 foot 分隔；
- sheet 加监控弹层用 app.wxss 的纸面样式；
- 分享画布 `#shareCanvas` 相关 js 一律不动。

### detail（演出详情）
- 大海报 + 名称 + 平台/城市/票价信息区（信息格用虚线分栏）；
- 场次列表：每行像票根一行，场次间用 `.perf-on-card` 打孔线分隔；
- 票档：名称 + 价格 + 状态（`.stamp`）；
- 底部「盯这场」：accent 大按钮；
- toast/dialog 接入（js 里现有的 wx.showToast/showModal 替换）。

### discussion（反馈/讨论区）
- 分类 tabs（全部/问题反馈/建议留言/求助留言）用 van-tabs，`onTab` 双签名兼容；
- 留言墙便签保留现有 note 图素材，便签上的文字/标签/头像meta 重新排版（纸感）；
- 头部「问题反馈」+ 茶绿小短线（board-line），无英文小注；挂件贴纸仅 1 张；
- 搜索栏、fab 发帖按钮（accent）、发帖弹层（.sheet 纸面）、放大查看便签全部纸感化；
- toast/dialog 接入；
- 底部「数据来自社区 · 每五分钟刷新一次」文案保留；
- 发帖可选便签素材 10 种（stickers 数组），全部不含英文字贴纸。

### history（余票历史）
- 列表改纸卡；空状态/失败态用 app.wxss 公共类；toast 接入。

### inbox（消息）
- 列表改纸卡；已读/未读用墨色层级区分（不引入新彩色）；toast 接入。

## 5. 自检清单（改完必须逐项过）

1. `grep -rn "font-family" miniprogram/miniprogram` 零命中；
2. 页面里所有 `var(--xxx)` 都能在 app.wxss 找到定义；
3. wxml 里没有残留 `bg-tint`；
4. `grep -rn 'class="[^"]*\bbow\b\|en-note\|spark-lg\|quota-bow\|search-pin' miniprogram/miniprogram` 零命中；
5. `grep -rn 'collage13\|collage16\|collage8.png' miniprogram/miniprogram` 零命中（带英文字的贴纸禁用）；
6. `grep -rn 'WATCHING NOW\|FIND SOMETHING\|ADMIT ONE\|ON SALE\|SESSIONS\|FEEDBACK' miniprogram/miniprogram` 零命中；
7. js 里没有残留 `wx.showToast` / `wx.showModal`（discussion/monitor/detail/history/inbox 各自检查）；
8. page.json 有正确的 usingComponents 与 #F7F2E7 导航色；
9. 事件 handler 名与数据字段名与原版一致（git diff 只看 wxml/wxss/json + js 的反馈替换）；
10. wxml 标签配对、wxss 大括号配对、页面 js `node --check` 全过。
