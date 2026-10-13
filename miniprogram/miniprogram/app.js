// ticket-radar 小程序
//
// 数据来自云函数 radar（query / poll / find），前端不直接请求任何票务平台——
// 小程序发请求要配域名白名单，而那些平台不可能给你加，所以只能走云函数中转。
//
// 环境 ID 的取用顺序：本地 storage > 下面这个默认值。
// 做成 storage 优先是为了调试方便——在开发者工具里
//   wx.setStorageSync('radar_env', 'your-env-id')
// 存一次就能换环境，不用改代码重新编译。
App({
  onLaunch() {
    if (!wx.cloud) return;

    const ENV = wx.getStorageSync('radar_env') || '';
    if (!ENV) {
      // 不静默失败：没配环境时，后面每个页面都会报错，用户完全不知道原因。
      // 这里留一条日志，开发者工具的 Console 里能直接看到。
      console.warn(
        '[ticket-radar] 未配置云环境 ID：看板/场次/历史都读不到数据。\n' +
          '配置方式二选一：\n' +
          '  1) 把 app.js 里的 ENV 填成你的环境 ID\n' +
          "  2) 在 Console 跑 wx.setStorageSync('radar_env', '你的环境ID')\n" +
          '「我的」页的绑定和领额度不依赖云开发，不受影响。',
      );
      return;
    }

    try {
      wx.cloud.init({ env: ENV, traceUser: true });
    } catch (err) {
      console.warn('[ticket-radar] 云开发初始化失败：', err);
    }
  },
});
