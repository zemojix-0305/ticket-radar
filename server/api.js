/**
 * ticket-radar 免费服务层（Vercel / 任意 Node 托管）
 * ---------------------------------------------------------------------------
 * 背景：微信云开发 2026 年起新环境必须购买套餐（19.9 元/月），本项目迁移到
 * 零成本架构——查询类接口全部无状态化，跑在 Vercel 免费额度上；有状态的
 * 监控任务 / 讨论帖 / 历史记录全部存小程序本地 storage（个人自用场景）。
 *
 * 复用小程序的云函数代码（miniprogram/cloudfunctions/radar/index.js）：
 * 云函数运行时全局注入 cloud 对象，这里在 require 之前 mock 掉它——
 *   database()      所有读写降级为空操作（无状态模式不落库）
 *   getWXContext()  没有 openid，返回空串
 *   openapi         订阅消息推送在本地模式跳过
 *
 * 接口：POST /api  body = { action, ... }
 *   action: find / detail / poll / probe
 *     find    搜演出（keyword/platform/limit）
 *     detail  演出详情（platform/id）
 *     poll    无状态轮询：客户端传 tasks 数组 + prevStates 对象，
 *             服务端只查平台、算回流、返回结果，不落任何库
 *     probe   测各平台从本机 IP 能不能抓到数据
 *
 * 本地跑：node api.js  （默认 3000 端口，PORT 环境变量可改）
 * Vercel 部署：见 server/vercel.json，零依赖。
 */

'use strict';

const http = require('http');
const path = require('path');

// ---------------------------------------------------------------------------
// mock 云运行时全局对象（必须在 require index.js 之前挂到 global）
// ---------------------------------------------------------------------------
const emptyCollection = () => ({
  where: () => ({ limit: () => ({ get: async () => ({ data: [] }) }) }),
  limit: () => ({ get: async () => ({ data: [] }) }),  // query action 直接调 .limit()
  doc: () => ({
    get: async () => ({ data: null }),      // 不存在 → null（首次无基线）
    set: async () => ({ _id: 'noop' }),     // 无状态模式：不落库
    update: async () => ({ stats: { updated: 0 } }),
  }),
  add: async () => ({ _id: 'noop' }),
  get: async () => ({ data: [] }),
});

global.cloud = {
  database: () => ({ collection: emptyCollection }),
  getWXContext: () => ({ OPENID: '', APPID: '' }),
  openapi: {
    subscribeMessage: {
      // 本地/无状态模式没有 openid 可推，调用方已跳过；真被调到就静默失败
      send: async () => {
        const e = new Error('subscribeMessage unavailable in stateless mode');
        e.errMsg = e.message;
        throw e;
      },
    },
  },
};

// 云函数主体（内部有 normalizePosters 出口层 + 全部平台 adapter）
const radar = require(path.join(__dirname, '..', 'miniprogram', 'cloudfunctions', 'radar', 'index.js'));

// ---------------------------------------------------------------------------
// HTTP 处理
// ---------------------------------------------------------------------------
function setCors(res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
}

async function apiHandler(req, res) {
  setCors(res);
  if (req.method === 'OPTIONS') {
    res.statusCode = 204;
    res.end();
    return;
  }

  let event = {};
  try {
    if (req.method === 'GET') {
      // GET /api?action=find&platform=maoyan&keyword=xxx
      const u = new URL(req.url, 'http://localhost');
      u.searchParams.forEach((v, k) => {
        event[k] = v;
      });
      event.limit = Number(event.limit) || 20;
    } else {
      const chunks = [];
      for await (const c of req) chunks.push(c);
      const raw = Buffer.concat(chunks).toString('utf8');
      event = raw ? JSON.parse(raw) : {};
    }
  } catch (e) {
    res.statusCode = 400;
    res.setHeader('Content-Type', 'application/json; charset=utf-8');
    res.end(JSON.stringify({ ok: false, error: 'bad request: ' + String(e.message || e) }));
    return;
  }

  try {
    const out = await radar.main(event);
    res.statusCode = 200;
    res.setHeader('Content-Type', 'application/json; charset=utf-8');
    res.end(JSON.stringify(out));
  } catch (e) {
    res.statusCode = 200; // 业务错误也走 200，小程序端统一按 ok 字段判断
    res.setHeader('Content-Type', 'application/json; charset=utf-8');
    res.end(JSON.stringify({ ok: false, error: String((e && e.message) || e) }));
  }
}

// ---------------------------------------------------------------------------
// 本地开发服务器 / Vercel serverless 双模式导出
// ---------------------------------------------------------------------------
if (require.main === module) {
  const port = Number(process.env.PORT) || 3000;
  const server = http.createServer(apiHandler);
  server.listen(port, () => {
    console.log('[ticket-radar] stateless api listening on http://127.0.0.1:' + port + '/api');
    console.log('[ticket-radar] try: curl -X POST http://127.0.0.1:' + port + "/api -H 'Content-Type: application/json' -d '{\"action\":\"find\",\"platform\":\"maoyan\",\"keyword\":\"\",\"limit\":3}'");
  });
}

module.exports = apiHandler;
