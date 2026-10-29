# ticket-radar 交接文档（给接手 AI / Claude Code）

> 本文档自包含，目的是让另一个 AI 工具（Claude Code）无需翻聊天记录即可接手本项目。
> 生成时间：2026-10-28。仓库最新提交：`d8a4b4a`（取消开屏）。

---

## 0. 一句话定位

**ticket-radar** 是一个微信小程序 + 云端轮询服务的**余票回流监控提醒工具**（大麦 / 淘票票 / 纷玩岛 / 猫眼 / 摩天轮 / B站会员购 / 票星球 / 12306）。**只做监控提醒，不做、也不碰自动抢票**（合规红线，已刻进项目基因）。用户在小程序里加监控任务，服务定时轮询，回流时推送到手机。

---

## 1. 路径与仓库（先核对，别用错目录）

- **真实项目根**：`D:\Amyapp\ticket-radar`
  - ⚠️ 桌面上的 `ticket-radar` 文件夹是**空壳**，不要用。
- **双仓库**：
  - `origin` = GitHub `zemojix-0305/ticket-radar`
  - `gitee` = `zemojix/ticket-radar`
- 当前分支 `main`，工作区在 2026-10-28 干净。
- 小程序代码在 `miniprogram/miniprogram/`（双嵌套，devtools 的 `project.config.json` 指向内层 `miniprogram/`）。

---

## 2. 架构三条线（千万别搞混）

1. **小程序前端** → `miniprogram/miniprogram/utils/api.js`，默认打 `http://localhost:3000/api`（`API_BASE` 常量，可覆盖为 Vercel 域名）。
2. **本地/云端服务层** `server/api.js`：mock 掉云函数对象后复用云函数代码；查询类接口全无状态。
3. **真 7×24 引擎** `miniprogram/cloudfunctions/radar/index.js`（也是 `server/api.js` 实际 require 的同一份逻辑）：才是长期盯票的核心。
   - 小程序自身**不常驻轮询**（只有打开页面才查）；「关掉小程序也能收推送」靠服务层 + 微信订阅消息。

---

## 3. 硬约定（违反就会踩坑，务必遵守）

- **改完 `cloudfunctions/radar/index.js` 或 `server/api.js` 必须重启本地 Node 服务**，否则小程序看到的还是旧逻辑（历史上多次「点详情全是空白」就源于此）。
  - 启动：`server/` 下 `node api.js`（监听 `:3000`）。仓库根有 `start-server.bat` 双击即启。
- **服务端凭据优先级**：真实环境变量 > 仓库根 `.env` > `server/config.json`。`.env` 读取是后来补上的。
- **合规下限**：`TaskConfig.interval_seconds` **不得低于 60 秒**（压到秒级+账号池 = 自动刷票，明令禁止）。「快」= 回流后尽快通知，**不是**比谁手速快。
- 价位多选参数名统一 `price_labels`（数组）；旧单数 `price_label` 与 12306 的 `seat_types` 做兼容。
- 数字价位匹配必须**整体相等**（`399` 不能命中 `1399`）。
- **推送双推命令**（GitHub 走代理，Gitee 直连）：
  ```
  git -c credential.helper= -c credential.helper=manager push gitee main
  http_proxy=http://127.0.0.1:7897 https_proxy=http://127.0.0.1:7897 HTTP_PROXY=http://127.0.0.1:7897 HTTPS_PROXY=http://127.0.0.1:7897 git -c credential.helper= -c credential.helper=manager push origin main
  ```
  ⚠️ 四个代理变量（大小写都要）缺一就会 `CONNECT tunnel failed 502`。代理没开（Clash 未启动）时推送会失败，属正常，开代理再推即可。

---

## 4. 平台数据通路（已验证，匿名可用）

活体测试（`tools/verify.mjs`，69+ 过 / 0 败 / 4 警告）结论：

| 平台 | 状态 | 备注 |
|---|---|---|
| 猫眼 | ✅ | 票价档位匿名可得（`/tickets`），但 `hasInventory` 恒 false，只展示不勾选 |
| 摩天轮 | ✅ | 列表/场次/票档 |
| B站会员购 | ✅ | |
| 票星球 | ✅ | 含真实票档 |
| 12306 | ✅ | `rail12306` 平台；过期日期已本地拦截 |
| 大麦 | ✅ | 热卖榜/搜索/场次匿名（**关键词搜索易被 IP 限频**，非 bug） |
| 淘票票 | ✅ | 接大麦通道 |
| 纷玩岛 | ✅ | 贴分享链接匿名，逐票档需 token |

- **大麦匿名破局关键**：新 H5 站点 `m.damai.cn/shows/*` 走 `mtopH5Get`，带 `H5Request=true & forceAntiCreep=true & AntiCreep=true`（**不带 x-tap**）+ payload 补 `platform=282/comboChannel=4/dmChannel=damai@weixin_gzh` 即零 Cookie 通。旧 `mtop.damai.item.detail.getdetail` 已废弃。
- **凭证边界（重要）**：无需任何凭证即可用的 = 上面 8 家平台的列表/搜索/价格/场次。只剩「**逐票档实时库存**」需要登录态（大麦 subpage RGV587、猫眼 hasInventory、纷玩岛 get_performs 11004）。凭证只存用户手机 `wx.getStorageSync`，**不经任何第三方、不入库**。

---

## 5. 最近已完成的修复（避免重复劳动）

| 提交 | 内容 |
|---|---|
| `d8a4b4a` | **取消开屏动画**：`app.json` 移除 `pages/splash/splash`，`entryPagePath` 设为 `pages/monitor/monitor`；删除了 splash 文件夹 |
| `b7e5dde` | 新增 `start-server.bat`（双击起本地服务）；`App.onError` 全局兜错 + shows 页启动出错显示可见红卡片 |
| `129ab6e` | 去掉 `shows.js` 的 `if(searching) return` 硬守卫（请求挂死后会永久吞掉点击） |
| `3a5b899` | 「详情 ›」按钮直绑 `catchtap`（之前依赖冒泡，devtools 偶发不冒泡=点了没反应）；`openDetail` 空 id 改 Toast |
| `1f9cdf4` | `wx:key` 重复告警修复（12306 车次号不能当 key，`normalize` 注入自增 `_uk`） |
| `0ad801f` | detail 页 `onLoad` 解码保护（裸 `%` 抛 URIError 会炸整页）+ 30s 兜底闹钟 |
| `641c4ac` | 12306 过期日期（如选了过去的日子）本地拦截报人话，不再冒充「云 IP 风控」 |

---

## 6. 当前未决 / 用户最大痛点

### 6.1 「devtools 里所有按钮失灵（连本地双箭头都不动）」
- **诊断结论（已逐项核到底）**：代码本身 100% 正确（JS 绑定、`import` 链含 vant 源码、`bg-collage` 有 `pointer-events:none`、`project.config.json` 指向正确、后端 12306 实测秒回真实车次）。
- 连**纯本地**的双箭头（只 `setData`）都死，说明这一页 `Page()` 没被 devtools 注册成功（能显示 WXML 静态骨架但事件全失活）——**这是 devtools 编译/加载态的环境问题，不是代码 bug**。
- **接手者遇到此问题，按序排查**：
  1. 本地 `:3000` 服务是否在跑（双击 `start-server.bat` 并保持窗口开）；
  2. devtools 顶部有无红色「编译错误」横幅；
  3. `Ctrl+B` 重新编译；
  4. 「清缓存 → 清除全部缓存」再编译 / 关掉 devtools 重开项目；
  5. **千万别点「构建 npm」**——本项目 vant 是**源码组件**（`/vant/...` 绝对路径），不是 npm 包，点了反而坏；
  6. Console 清旧日志后看带文件名的红字（那才是加载失败的文件）。

### 6.2 真机/体验版永远连不上 localhost
- `utils/api.js` 的 `API_BASE` 默认 `http://localhost:3000`——真机上 localhost 是手机自己，必然连不上。
- **治本**：把 `server/`（零依赖，`server/vercel.json` + `start: node api.js` 已就绪）部署到 Vercel，拿到 `https://xxx.vercel.app`，填进 `API_BASE`，并在小程序后台「开发设置 → 服务器域名 → request 合法域名」加白。⚠️ 注意：根目录目前**没有** `vercel.json`/`package.json`，部署入口在 `server/` 子目录，需确认 Vercel 根目录配置。

### 6.3 还没做的功能（候选方向，均不碰自动抢票）
- 开售倒计时 + 到点强提醒（最实用）
- 价格趋势图
- 多平台同演出比价/余票聚合卡片
- 逐票档实时库存（需登录态，已有凭证通道）
- 服务端定时轮询云函数 timer / 开售倒计时提醒

---

## 7. 本地怎么跑（给接手者的启动清单）

1. 双击 `D:\Amyapp\ticket-radar\start-server.bat`（或在 `server/` 跑 `node api.js`），保持黑窗口开着 → 服务在 `:3000`。
2. 微信开发者工具打开 `D:\Amyapp\ticket-radar\miniprogram`（它会读内层 `miniprogram/`）。
3. 开发者工具「详情 → 本地设置」勾「不校验合法域名、TLS 版本以及 HTTPS 证书」。
4. `Ctrl+B` 编译。模拟器应直接进监控首页（开屏已取消）。
5. 验证：演出页选 12306，填 广州南 / 潮汕，日期选今天之后，点「查余票」→ 应出真实车次。

---

## 8. 关键文件速查

- `miniprogram/miniprogram/utils/api.js` —— 前端请求封装 + `API_BASE`
- `miniprogram/cloudfunctions/radar/index.js`（= `server/api.js` require 的同一份）—— 所有平台抓取逻辑
- `miniprogram/miniprogram/pages/shows/shows.js` / `.wxml` —— 演出/12306 查询主页面
- `miniprogram/miniprogram/pages/monitor/monitor.js` —— 监控任务列表（卡片可点进详情）
- `miniprogram/miniprogram/pages/detail/detail.js` —— 详情页（含 URL→id 归一化）
- `tools/verify.mjs` —— 活体回归测试（各平台出数断言）
- `start-server.bat` —— 一键起本地服务

---

## 9. 给接手者的一句话建议

用户是华农软工本科生，偏好**结论先行、能直接粘贴执行的命令、少废话**。本项目代码已较完整，**最大的坑不在写代码，而在：① devtools 运行态排查（见 6.1）；② 真机部署打通（见 6.2）**。优先把「真机能看到数据」这条打通，比加新功能更能解决用户当下的挫败感。
