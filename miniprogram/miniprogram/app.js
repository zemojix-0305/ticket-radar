// ticket-radar 小程序
//
// 第二十五轮：回到云开发。
// 上一轮为了让后端能白嫖 Vercel，把数据层改成了「无状态 HTTP」，代价是
// 真机必须能访问到一个公网地址——而这个项目没有公网地址，于是真机上
// 查票、盯票、社区全是坏的，只有开发者工具里看着正常。
//
// 现在的分工：
//   - 云函数 radar：所有查询（查票/详情/比价）+ 社区帖子 + 定时盯票 + 推送
//   - 云数据库：任务、基线、帖子、订阅额度
//   - 小程序：只负责界面，不常驻、不后台跑
//
// server/api.js 那套本地 HTTP 服务还在，但它只用于本地调试（config.js 里
// USE_CLOUD=false 时才会走），线上不再依赖它。

const api = require('./utils/api');
const push = require('./utils/push');
const tasks = require('./utils/tasks');
const cfg = require('./config');

App({
  globalData: {
    cloudReady: false,
  },

  onLaunch() {
    this.initCloud();
    this.syncEverything();
  },

  /** 初始化云开发。失败不抛错：界面还能打开，只是查票会失败并给出提示。 */
  initCloud() {
    if (!cfg.USE_CLOUD) return;
    if (!wx.cloud) {
      console.error(
        '[ticket-radar] 当前基础库不支持云开发。请在小程序开发者工具「详情 → 本地设置」里' +
          '把「调试基础库」调到 2.2.3 及以上，或直接选 latest。'
      );
      return;
    }
    try {
      const opt = { traceUser: true };
      // 显式指定环境更稳；留空时用账号下的默认环境。
      // 有多套环境（比如还建了测试环境）时一定要在 config.js 里填 CLOUD_ENV，
      // 否则可能连到另外那套上去，表现是「数据莫名其妙没了」。
      if (cfg.CLOUD_ENV) opt.env = cfg.CLOUD_ENV;
      wx.cloud.init(opt);
      this.globalData.cloudReady = true;
    } catch (e) {
      console.error('[ticket-radar] wx.cloud.init 失败：', e);
    }
  },

  // 每次回到前台做两件事，用户完全无感：
  //   1. 把本地任务列表同步给云函数——云端定时器才知道要盯什么；
  //   2. 绑定 openid（云端用 getWXContext 直接拿，这里只是确认能拿到）。
  // 订阅授权不放在这里：wx.requestSubscribeMessage 必须由用户点击触发，
  // 在 onShow 里调用一定失败（can only be invoked by user TAP gesture）。
  // 所以授权只在用户点「开始盯」「恢复推送」时请求，见 utils/push.js。
  onShow() {
    this.syncEverything();
  },

  syncEverything() {
    const list = tasks.loadTasks();
    // 任务同步：即使这次连不上云端也不影响本地功能
    api.pushSync(list, push.isOn());

    // 把上一版存在手机本地的帖子搬上云（只跑一次，失败静默）
    try {
      require('./utils/posts')
        .migrateLocalPosts()
        .catch(() => {});
    } catch (e) {
      /* ignore */
    }

    // openid 确认：云函数每次调用都能从上下文拿到，这里只做一次能力自检，
    // 拿不到就在「我的」页提示用户重新进入，而不是让他以为推送开了。
    if (!wx.getStorageSync('radar_openid_ok')) {
      api
        .bindOpenId()
        .then((res) => {
          const r = (res && res.result) || {};
          if (r.ok) wx.setStorageSync('radar_openid_ok', true);
        })
        .catch(() => {
          /* 云端没配好就下次再说 */
        });
    }
  },

  // 全局兜住页面运行时错误，避免「整页无声死掉」时用户毫无头绪。
  // 注意：模块加载失败（import 抛错导致 Page 不注册）这一层兜不住，
  // 那种情况 devtools Console 会直接红字点名文件，属于编译/环境问题。
  onError(err) {
    console.error('[ticket-radar] 运行时错误:', err);
  },
});
