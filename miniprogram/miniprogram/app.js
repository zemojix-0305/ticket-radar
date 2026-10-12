// ticket-radar 小程序
//
// 数据来自云函数 radar（query / poll），前端不直接请求任何票务平台——
// 小程序发请求需要配域名白名单，而那些平台不可能给你加，所以只能走云函数中转。
//
// 云开发是**可选**的：不填环境 ID 也能用「我的」页面拿登录 code、订阅通知，
// 所以这里做兜底，避免没开通云开发时整小程序直接白屏。
App({
  onLaunch() {
    if (!wx.cloud) {
      return;
    }
    const ENV = ''; // ← 开通云开发后把环境 ID 填这里；留空则不初始化
    if (!ENV) {
      return;
    }
    try {
      wx.cloud.init({ env: ENV, traceUser: true });
    } catch (err) {
      console.warn('云开发初始化失败，看板功能将不可用', err);
    }
  },
});
