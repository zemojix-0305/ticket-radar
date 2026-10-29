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
const fs = require('fs');

// ---------------------------------------------------------------------------
// 读取仓库根目录的 .env
// ---------------------------------------------------------------------------
// 为什么必须读：Python 侧（radar/）走 python-dotenv 读 .env，所以「凭据都填在
// .env 里」是这台机器上的既有约定。Node 这层原来只认 process.env 与
// server/config.json，于是出现过「.env 里 DAMAI_COOKIE 填得好好的，小程序
// 点进大麦详情却只有标题、没有场次票档」——配置填了但根本没送达服务层。
// 这条坑查起来极费时间（日志里什么都正常），所以这里显式补上。
// 优先级：真实环境变量 > .env > server/config.json。不做 ${VAR} 插值。
function readDotEnv() {
  const out = {};
  try {
    const raw = fs.readFileSync(path.join(__dirname, '..', '.env'), 'utf8');
    for (const line of raw.split(/\r?\n/)) {
      const s = line.trim();
      if (!s || s.startsWith('#')) continue;
      const i = s.indexOf('=');
      if (i <= 0) continue;
      const k = s.slice(0, i).trim();
      let v = s.slice(i + 1).trim();
      if (
        (v.startsWith('"') && v.endsWith('"')) ||
        (v.startsWith("'") && v.endsWith("'"))
      ) {
        v = v.slice(1, -1);
      }
      if (k && v) out[k] = v;
    }
  } catch (e) {
    /* 没有 .env 不是错误：全匿名也能跑 */
  }
  return out;
}

// ---------------------------------------------------------------------------
// 服务端自持凭据（面向大众上架：终端用户不填任何 cookie/token）
//   本地：.env（首选）或 server/config.json（已 gitignore，不进仓库）
//   云端：环境变量 DAMAI_COOKIE / FWD_TOKEN / MY_TOKEN 优先
// 注入点在下面 handler 里：客户端没带才注入，客户端显式传的优先（调试用）。
// ---------------------------------------------------------------------------
function loadSrvConfig() {
  const cfg = {};
  const dotenv = readDotEnv();
  const env = (k) => process.env[k] || dotenv[k] || '';
  if (env('DAMAI_COOKIE')) cfg.damaiCookie = env('DAMAI_COOKIE');
  // FENWANDAO_COOKIE 是 .env 里的既有键名（Python 侧 radar/ 用它），
  // 这里当作 FWD_TOKEN 的别名——不然按 .env 注释填了凭证服务端却读不到。
  if (env('FWD_TOKEN') || env('FENWANDAO_COOKIE')) cfg.fwdToken = env('FWD_TOKEN') || env('FENWANDAO_COOKIE');
  if (env('MY_TOKEN')) cfg.myToken = env('MY_TOKEN');
  try {
    const local = JSON.parse(fs.readFileSync(path.join(__dirname, 'config.json'), 'utf8'));
    for (const k of ['damaiCookie', 'fwdToken', 'myToken']) {
      if (local[k] && !cfg[k]) cfg[k] = String(local[k]);
    }
  } catch (e) { /* 没有 config.json 就全走匿名 */ }
  return cfg;
}
const SRV_CONFIG = loadSrvConfig();

// ---------------------------------------------------------------------------
// mock 云运行时全局对象（必须在 require index.js 之前挂到 global）
// ---------------------------------------------------------------------------
const emptyCollection = () => ({
  // 链式查询统一走 emptyQuery（where/orderBy/limit 任意组合）
  where: () => emptyQuery(),
  orderBy: () => emptyQuery(),
  limit: () => emptyQuery(),
  doc: () => ({
    get: async () => ({ data: null }),      // 不存在 → null（首次无基线）
    set: async () => ({ _id: 'noop' }),     // 无状态模式：不落库
    update: async () => ({ stats: { updated: 0 } }),
  }),
  add: async () => ({ _id: 'noop' }),
  get: async () => ({ data: [] }),
  count: async () => ({ total: 0 }),
});

const emptyQuery = () => {
  const q = {
    where: () => q,
    orderBy: () => q,
    limit: () => q,
    skip: () => q,
    get: async () => ({ data: [] }),
  };
  return q;
};

// 内存级 KV 存储：替代云存储缓存（如 12306 站名表，fetchStationMap 用）
const memKV = {};
global.cloud = {
  database: () => ({ collection: emptyCollection }),
  getStorage: async (key) => (key in memKV ? memKV[key] : null),
  setStorage: async (opt) => {
    memKV[opt.key] = opt.value;
    return {};
  },
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
    } else if (req.body !== undefined && req.body !== null) {
      // Vercel / 部分 Node 托管会预解析 body 挂到 req.body；这时 req 流已经读完，
      // 再去 for-await 会永远挂住，必须优先用它。
      event = typeof req.body === 'string' ? JSON.parse(req.body) : req.body;
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

  // 服务端凭据注入：客户端没带才补（本地上强调试用），终端用户全程无感
  if (!event.damaiCookie && SRV_CONFIG.damaiCookie) event.damaiCookie = SRV_CONFIG.damaiCookie;
  if (!event.fwdToken && SRV_CONFIG.fwdToken) event.fwdToken = SRV_CONFIG.fwdToken;
  if (!event.myToken && SRV_CONFIG.myToken) event.myToken = SRV_CONFIG.myToken;

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
    // 只报「有没有」，不报值。没有这行，凭据没送达时排查全靠猜。
    const mark = (v) => (v ? '✓' : '✗');
    console.log(
      '[ticket-radar] credentials: damaiCookie=' + mark(SRV_CONFIG.damaiCookie) +
      ' fwdToken=' + mark(SRV_CONFIG.fwdToken) +
      ' myToken=' + mark(SRV_CONFIG.myToken) +
      '  (来源：环境变量 / .env / server/config.json)',
    );
    console.log('[ticket-radar] try: curl -X POST http://127.0.0.1:' + port + "/api -H 'Content-Type: application/json' -d '{\"action\":\"find\",\"platform\":\"maoyan\",\"keyword\":\"\",\"limit\":3}'");
  });
}

module.exports = apiHandler;
