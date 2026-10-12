// ticket-radar 小程序
//
// 数据全部来自云函数 radar（query / poll），前端不直接请求任何票务平台——
// 小程序发请求需要配域名白名单，而那些平台不可能给你加，所以只能走云函数中转。
App({
  onLaunch() {
    if (!wx.cloud) {
      wx.showModal({ title: '基础库过低', content: '请在微信里更新到最新版', showCancel: false });
      return;
    }
    // ↓↓↓ 换成你自己的云开发环境 ID（部署文档第 2 步会给）
    wx.cloud.init({ env: '你的环境ID', traceUser: true });
  },
});
