# ticket-radar 微信小程序

Python 那套跑在你自己电脑上；这套跑在微信云开发上，手机随时能看。

支持 **12306 铁路 / 猫眼 / 摩天轮** 三个平台（都是匿名可用的公开接口）。
不支持大麦——它要登录态，Cookie 存云上等于把凭据放到第三方环境，不安全也不必要。

---

## 部署（第一次做，跟着走）

### 1. 注册小程序

去 <https://mp.weixin.qq.com> 注册，主体类型选**个人**。

- 不用营业执照
- 不用认证（认证是 30 元/年解锁高级接口，不是上架门票）
- 类目随便选个「工具」类的就行，这个小程序不做交易

### 2. 开通云开发

在开发者工具里：**云开发 → 开通**，选按量付费或免费额度。

记下**环境 ID**，下一步要填。

### 3. 导入项目

下载 [微信开发者工具](https://developers.weixin.qq.com/miniprogram/dev/devtools/download.html)，
选「小程序 → 导入项目」，目录选本仓库的 **`miniprogram/`** 这一层（不是仓库根目录）。

- AppID 填你刚注册的
- 云开发环境 ID 填上一步那个

### 4. 改两处占位符

| 文件 | 改成 |
|---|---|
| `miniprogram/app.js` | `env: '你的环境ID'` → 你自己的 |
| `cloudfunctions/radar/` 里的 `index.js` | 不用改 |

### 4.5 **配 downloadFile 合法域名**（海报图片必须，不漏这步海报全是裂图）

小程序 `<image>` 加载外链海报，域名必须在小程序后台
「开发 → 开发管理 → 开发设置 → 服务器域名 → downloadFile 合法域名」里登记
（不用勾选校验，仅列表登记即可）。当前代码实测用到的图片域名：

```
p0.pipi.cn
p1.pipi.cn
i0.hdslb.com
i1.hdslb.com
i2.hdslb.com
cdn.ticketmars.com
img.alicdn.com
gw.alicdn.com
alipic.oss-cn-beijing.aliyuncs.com
```

> 域名清单来自 2026-10 对猫眼/B站/票星球/大麦接口的实测返回值。
> 云函数出口有 `normalizePosters` 统一把协议相对（`//host/…`）和 `http://`
> 升成 `https://`，所以这里只需登记 https 域名。

### 5. **先验证云 IP 能不能抓到数据**（重要，别跳）

在开发者工具的云开发控制台 → 云函数 → 新建/上传 `radar`，
然后在**云开发控制台的调试器**里调用：

```json
{ "action": "probe" }
```

看返回：

- `ok: true` → 你的云 IP 没被风控，继续下一步
- `ok: false` 或 `html_page: true` → **腾讯云的 IP 被票务平台风控了**。
  这不是代码问题（同样的代码在你电脑上跑得通，见下），是这条路走不通。
  回到主项目用 `radar run` + ntfy 推送吧。

> 本地先自测：`node cloudfunctions/radar/smoke.js`
> 会用真实接口验证三个平台的解析逻辑全对。**代码对不对和 IP 能不能过是两件事，
> 得分开验证。**

### 6. 加任务

两种方式，任选：

**A. 小程序里点「＋ 加任务」**（推荐）

**B. 云开发控制台 → 数据库 → `radar_tasks` 集合，手加一条**

```json
{
  "_id": "maoyan-509862",
  "adapter": "maoyan",
  "name": "汪苏泷·阜阳斑马音乐节",
  "params": { "performance_id": "509862" },
  "enabled": true
}
```

12306 的 params 长这样（`from`/`to` 填**站码**，在 `station_name.js` 里能查到）：

```json
{ "from": "IZQ", "to": "CWQ", "date": "2026-10-10", "seat_types": ["二等座"] }
```

### 7. 让它自动轮询

云开发控制台 → 云函数 `radar` → **触发器** → 新建：

- 触发器类型：**定时触发器**
- 周期：**每 10 分钟**（cron 表达式 `0 */10 * * * *`）

**别设太密。** 12306 实测 1 分钟内连抓两次就被 302。各平台都有最小间隔，
密了只会撞限流，不会更新得更快。

### 8. 上传发布

开发者工具 → 上传 → 到小程序后台「版本管理」→ 提交审核 → 发布。

---

## 它能做什么 / 不能做什么

| | |
|---|---|
| 看余票状态、车次/场次列表 | ✅ |
| 加任务、删任务 | ✅ |
| **有票时推送手机** | ❌ **做不到** |
| 抢票、下单 | ❌ 不做 |

**关于推送**：小程序后台运行限制极严，定时任务只在用户打开小程序时才可能执行，
指望它常驻发通知是不现实的。**要推送就用主项目的 ntfy**（那个是真能用的，
你已经在用了）。

小程序这边的定位是「随时打开看一眼」，不是「替你盯着」。

---

## 目录

```
miniprogram/
├── cloudfunctions/radar/
│   ├── index.js      云函数：probe / poll / query 三个动作
│   └── smoke.js      离线冒烟测试（node smoke.js，用真接口）
└── miniprogram/
    ├── app.js        填环境 ID
    ├── app.json
    └── pages/index/  看板页
```

**云函数是 Node.js，跟主项目的 Python 是两套代码。** 解析逻辑是从
`radar/adapters/` 翻成 JS 的，**改那边记得同步这里**。

三个数据表（都在云开发控制台里）：

| 集合 | 用途 |
|---|---|
| `radar_tasks` | 你配的任务（小程序写的） |
| `radar_state` | 每轮抓的结果（云函数写的） |
| 云存储 `radar_config_stations` | 12306 站名表缓存，一天取一次 |

## 为什么数据要走云函数

小程序不能直接请求票务平台：微信要求请求域名先 ICP 备案、再配进白名单，
而那些平台不可能给一个陌生小程序开白名单。云函数运行在腾讯服务器上，
不受这个限制——**这也是为什么必须有云函数，前端自己抓不了**。

---

## 设计系统：白色灵动（第 19 轮）

完整规范见仓库根目录 **`DESIGN.md`**（改任何页面样式前先读它）。要点：

- **语言**：白色灵动。淡灰白底 `#F7F7FA` + 纯白卡 `#FFFFFF` + 近黑主色 `#1C1C1E`
  （主 CTA/选中态/tabs 下划线），分区靠柔和阴影与大留白，不靠描边；
  彩色只留给语义：绿 `#237A45`（有票/在售）、灰 `#6B6B73`（无票）、
  红 `#C43C2C`（售罄/异常）、琥珀 `#8D6308`（额度提醒）。
- **形状规则**：卡片/按钮大圆角 22px，功能件药丸 999px，全站统一。
- **字体**：全部系统默认，零自定义字体声明（硬规则）。
- **手账元素已全废**（第 19 轮）：胶带/印章/打孔线/条码/蝴蝶结/英文小注/✦ 符号，
  app.wxss 不再定义；拼贴只留在开屏与页面背景层（`bg-stk`）。
- **公共组件**都在 `miniprogram/app.wxss`：`.card` `.tag` `.btn` `.sheet` `.field`
  `.empty` `.bg-collage` 等，直接用 class，不要各写各的。
- **对比度**：`python tools/check_palette.py`，19 项 WCAG AA 全过（改色板必须重跑）。

### Vant Weapp 组件库

`miniprogram/miniprogram/vant/` 是从 [@vant/weapp](https://github.com/youzan/vant-weapp) v1.11.7
**手工 vendor 的 dist**（共 19 个组件目录，约 380KB），不走 npm 构建，导入即用。
**不要改 `miniprogram/miniprogram/vant/` 里的文件**；要加组件就从官方 dist 拷对应目录进来。

已接入：`van-tabs`/`van-tab`（shows 平台切换、discussion 分类切换）、
`van-toast`/`van-dialog`（全站反馈，替代原生 showToast/showModal）。

```js
// js 顶部
import Toast from '/vant/toast/toast';
import Dialog from '/vant/dialog/dialog';
Toast('文字');                                  // 或 Toast({ type: 'success', message: '文字' })
Dialog.confirm({ title, message }).then(() => {}).catch(() => {});
```

```json
// page.json
"usingComponents": { "van-toast": "/vant/toast/index", "van-dialog": "/vant/dialog/index" }
```

```xml
<!-- wxml 末尾 -->
<van-toast id="van-toast" />
<van-dialog id="van-dialog" />
```

白灵动主题靠 vant 的 `addGlobalClass` 机制由 `app.wxss` 里的 `page .van-*` 选择器覆盖，
新增组件若要改样式，加在 app.wxss 的「Vant Weapp 白色灵动主题覆盖」区。
