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
});
