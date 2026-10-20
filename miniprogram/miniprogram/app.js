// ticket-radar 小程序
//
// 第二十轮：迁移到无状态 HTTP 架构，删除云开发依赖。
// 数据请求通过 utils/api.js 统一走 HTTP，后端可以是：
//   - 本地 dev server（localhost:3000）
//   - Vercel 部署的免费服务（设置 API_BASE 环境变量）
//
// 本地调试：npm run start 在 server/ 目录启动，或 node api.js
// 生产环境：在 vercel.json 旁放 .env.production，写入 API_BASE=https://xxx.vercel.app

App({
  onLaunch() {
    // 云开发已废弃，不再初始化
    console.log('[ticket-radar] launched, using stateless HTTP API');
  },
  // 全局兜住页面运行时错误，避免「整页无声死掉」时用户毫无头绪。
  // 注意：模块加载失败（import 抛错导致 Page 不注册）这一层兜不住，
  // 那种情况 devtools Console 会直接红字点名文件，属于编译/环境问题。
  onError(err) {
    console.error('[ticket-radar] 运行时错误:', err);
  },
});
