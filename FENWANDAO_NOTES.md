# 纷玩岛接入方案

## 现状
纷玩岛 API (`api.livelab.com.cn`) 需要有效的 `authorization: Bearer <token>` 才能访问搜索和票档接口。从外部 IP（包括云服务器）访问一律返回 `11004 没有登录`。

## 已验证可行的方案

### 方案 A：微信小程序跳转登录（推荐）
1. 用户点击「跳转纷玩岛登录」
2. `wx.navigateToMiniProgram` 打开纷玩岛小程序
3. 用户在纷玩岛内登录
4. 用户返回后，在纷玩岛「我的」页长按复制 authorization token
5. 粘贴到 ticket-radar 的凭证输入框
6. 点「测试连通」验证

### 方案 B：WebView 内嵌登录（技术可行但复杂）
1. 用 `<web-view>` 组件打开 `https://m.livelab.com.cn/hppreview/pages/user/login`
2. 用户在 WebView 内完成登录
3. 监听页面 redirect，从 URL 中提取 token
4. ⚠️ 需要配置业务域名白名单，且纷玩岛 H5 可能不支持自定义 callback

### 方案 C：电脑抓包（最可靠）
1. 电脑安装 Charles/Fiddler
2. 手机连接同一 WiFi，配置代理
3. 打开纷玩岛小程序，随便点一个演出
4. 抓包工具里找到 `api.livelab.com.cn` 的请求
5. 复制 `authorization` 头的值
6. 粘贴到 ticket-radar

## 为什么不能像其他平台一样完全匿名
- 猫眼/摩天轮/B站/票星球：公开 JSON API，无签名
- 大麦：mtop 网关有匿名 CMS 通道（H5Request=true）
- 纷玩岛：**只有 App/小程序入口**，网页版是下载落地页，API 需要小程序会话上下文

## 实现状态
- ✅ 已添加「跳转纷玩岛登录」按钮（调用 wx.navigateToMiniProgram）
- ✅ 已添加「测试连通」按钮（用保存的 token 验证）
- ✅ 已添加手动粘贴兜底
- ✅ shows 页纷玩岛 tab 显示粘贴模式入口
