// ticket-radar 小程序
//
// 第二十轮：迁移到无状态 HTTP 架构，删除云开发依赖。
// 数据请求通过 utils/api.js 统一走 HTTP，后端可以是：
//   - 本地 dev server（localhost:3000）
//   - Vercel 部署的免费服务（设置 API_BASE 环境变量）
//
// 本地调试：npm run start 在 server/ 目录启动，或 node api.js
// 生产环境：在 vercel.json 旁放 .env.production，写入 API_BASE=https://xxx.vercel.app

const api = require('./utils/api');
const push = require('./utils/push');
const tasks = require('./utils/tasks');

App({
  onLaunch() {
    // 云开发已废弃，不再初始化
    console.log('[ticket-radar] launched, using stateless HTTP API');
    this.syncEverything();
  },

  // 每次回到前台都做三件事，用户完全无感：
  //   1. 静默续一条订阅授权（勾了「总是保持以上选择」就不再弹窗）——
  //      微信一次性订阅是一次授权换一条推送，靠这个慢慢攒额度；
  //   2. 把本地任务列表同步给服务端——服务端才知道要盯什么；
  //   3. 用 wx.login 的 code 换 openid 存到服务端——不然发不出去。
  // 缺任何一件，手机上就收不到消息，而用户看到的界面还显示「推送已开启」。
  onShow() {
    this.syncEverything();
  },

  syncEverything() {
    const list = tasks.loadTasks();
    // 任务同步：即使这次连不上服务端也不影响本地功能
    api.pushSync(list, push.isOn());

    // openid 绑定：不弹窗、不需要用户操作，绑一次就够了。
    // 不管推送开没开都绑——否则用户以后点「恢复推送」时才发现发不出去。
    if (!wx.getStorageSync('radar_openid_ok')) {
      api
        .bindOpenId()
        .then((res) => {
          const r = (res && res.result) || {};
          if (r.ok) wx.setStorageSync('radar_openid_ok', true);
        })
        .catch(() => { /* 服务端没开就下次再说 */ });
    }

    // 静默续期放在最后：它会弹窗（首次）或静默返回，不该挡住上面两件事
    push.renewAfterAdd();
  },

  // 全局兜住页面运行时错误，避免「整页无声死掉」时用户毫无头绪。
  // 注意：模块加载失败（import 抛错导致 Page 不注册）这一层兜不住，
  // 那种情况 devtools Console 会直接红字点名文件，属于编译/环境问题。
  onError(err) {
    console.error('[ticket-radar] 运行时错误:', err);
  },
});
