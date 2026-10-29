/**
 * ticket-radar 微信云函数
 *
 * 干四件事：
 *   probe —— 测各平台从「腾讯云 IP」能不能抓到数据（部署后先跑这个）
 *   poll  —— 轮询所有启用的任务，把结果写进云数据库
 *   query —— 小程序读最新结果
 *   find  —— 按关键词搜演出，小程序「场次」页用（用户不用自己找 id）
 *
 * 为什么云函数要重写一遍解析：本地版是 Python，而云开发只跑 Node.js。
 * 所以这里的解析逻辑是从 radar/adapters/ 翻成 JS 的，改那边记得同步这里。
 *
 * 明确不做的事：不下单、不破登录态签名、绝不存用户登录 Cookie。
 * 大麦走的是 mtop 无线网关下发的**匿名**令牌（_m_h5_tk），不是用户凭据，
 * 云上只缓存这个匿名令牌、不碰任何账号信息；票星球无公开搜索接口仍以
 * 粘贴链接为主。12306 / 猫眼 / 摩天轮 / 大麦均为匿名可用。
 */

'use strict';

const https = require('https');
const http = require('http');
const crypto = require('crypto');
// 机票（同程价格日历）：独立模块，免 Cookie 免签名。
const flightSvc = require('./flight');

const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/124.0 Safari/537.36';

const DB_TASKS = 'radar_tasks';
const DB_STATE = 'radar_state';
const DB_POSTS = 'radar_posts';
// 订阅额度云端记账：小程序端只记本地 storage 的话，云函数查不到额度，
// 推送永远发不出去（这就是「明明有票却收不到提醒」的根因）。
const DB_SUBS = 'radar_subs';
// 订阅消息模板 ID——与小程序端 mine.js 的 TMPL_ID 是同一个，改记得两边改。
// 字段（后台模板实际配置，2026-10 查证）：
//   thing6=演出名称  thing8=场次名称  time7=放票时间  thing5=放票机构  thing4=温馨提示
const NOTIFY_TMPL = 'YnMbqU_UqlWb1eUOSMOp1RwxfGJozlrlXYtSaXFnsF4';
const CFG_KEY = 'radar_config';

// ===========================================================================
// HTTP
// ===========================================================================

function request(url, opts) {
  const o = opts || {};
  return new Promise((resolve, reject) => {
    const lib = url.startsWith('http:') ? http : https;
    const r = lib.request(
      url,
      {
        method: o.method || 'GET',
        headers: o.headers || {},
        timeout: o.timeout || 15000,
        rejectUnauthorized: false,
      },
      (res) => {
        let body = '';
        res.setEncoding('utf8');
        res.on('data', (d) => {
          body += d;
        });
        res.on('end', () =>
          resolve({ status: res.statusCode, headers: res.headers, body: body }),
        );
      },
    );
    r.on('timeout', () => r.destroy(new Error('请求超时')));
    r.on('error', reject);
    // 请求体必须写进去再 end()。漏了这一行，POST 会变成"空 POST"——
    // 摩天轮那种只认 POST+JSON 的接口会回一个 111 字节的空壳，
    // 报错信息还特别有迷惑性（statusCode 12123，看着像平台故障）。
    if (o.body) r.write(o.body);
    r.end();
  });
}

/**
 * 请求并解析 JSON。
 *
 * 自动跟随 3xx（最多 3 跳）：12306 在缺会话时会 302 到别处，直接把
 * 「302」抛出去等于什么线索都没给。跟完再判断是不是 HTML——那时候
 * 才能下结论说「被风控了」。
 */
async function getJson(url, opts, redirectsLeft) {
  const left = redirectsLeft === undefined ? 3 : redirectsLeft;
  const r = await request(url, opts);
  if (r.status >= 300 && r.status < 400 && r.headers.location && left > 0) {
    let next;
    try {
      next = new URL(r.headers.location, url).toString();
    } catch (e) {
      throw new Error('重定向到了无法解析的地址：' + r.headers.location);
    }
    return getJson(next, opts, left - 1);
  }
  if (r.status !== 200) throw new Error('HTTP ' + r.status);
  // 被风控时会返回 HTML 而不是 JSON，这一条能把它和「真的没数据」分开
  if (/<html/i.test(r.body.slice(0, 300))) {
    if (process.env.RADAR_DEBUG) {
      console.error('[debug] HTML from', url, '| status', r.status, '| loc', r.headers.location || '无', '| head:', r.body.slice(0, 100).replace(/\n/g, ' '));
    }
    throw new Error('返回了 HTML——通常是日期超出预售期/参数不对，或被风控拦截');
  }
  try {
    return JSON.parse(r.body);
  } catch (e) {
    throw new Error('响应不是合法 JSON：' + r.body.slice(0, 120));
  }
}

// ===========================================================================
// 余票值的解析（三态：有票但数量未知 / 0 张 / n 张）
// ===========================================================================

const NOT_OFFERED = ['', '--', '*', '—', '-', '0'];
const ABUNDANT = ['有', '充足', '有票'];
const SOLD_OUT = ['无', '无票'];

/**
 * 解析单个席别的余票值。
 * 返回 null 表示本次列车不提供该席别——**不认识的值宁可漏报也不误报**。
 * 实测（2026-10-14，G1 京沪原始行）：不设此席别的列是 '' 或 '0'
 * （(动) 系列卧铺列尤其爱用 '0' 占位）；真卖光的列是 '无'。
 * 把 '0' 算进 NOT_OFFERED——不然没卧铺的高铁会冒出「高级软卧 无票」这种鬼话。
 */
function parseSeat(seatType, raw) {
  const v = String(raw == null ? '' : raw).trim();
  if (NOT_OFFERED.indexOf(v) >= 0) return null;
  if (ABUNDANT.indexOf(v) >= 0) return { seat_type: seatType, raw: v, count: null, available: true };
  if (SOLD_OUT.indexOf(v) >= 0) return { seat_type: seatType, raw: v, count: 0, available: false };
  if (/^\d+$/.test(v)) {
    const n = parseInt(v, 10);
    return { seat_type: seatType, raw: v, count: n, available: n > 0 };
  }
  return null; // 未知取值：跳过
}

function field(fields, i) {
  return i >= 0 && i < fields.length ? fields[i] : '';
}

// ===========================================================================
// 平台：猫眼（公开接口，无需登录）
// ===========================================================================

const MAOYAN_BASE = 'https://m.dianping.com/myshow';

//: ``ticketStatus`` → 官方文案。与 radar/adapters/maoyan.py 的表同源，改一处记得改两处。
const MAOYAN_TICKET_STATUS_LABELS = {
  1: '即将开售', 2: '预售', 3: '在售中', 4: '已售罄',
  5: '已结束', 11: '即将预售', 12: '演出延期',
};
//: 只有这两种算「能买到」。注意「即将开售」不算——那时候还买不了。
const MAOYAN_AVAILABLE = ['预售', '在售中'];


function maoyanHeaders() {
  return {
    'User-Agent': UA,
    Referer: 'https://show.maoyan.com/',
    Accept: 'application/json, text/plain, */*',
  };
}

/**
 * 按关键词搜索猫眼演出。
 *
 * 路径参数格式：/ajax/performances/<分类>;st=0;k=<词>;p=<页>;s=<条>;tft=0?cityId=<城>&sellChannel=7
 * 关键词是嵌在路径段里的，不是查询串。
 */
async function findMaoyan(keyword, limit) {
  const enc = encodeURIComponent(keyword);
  const path = `/ajax/performances/0;st=0;k=${enc};p=1;s=${Math.min(Number(limit) || 20, 50)};tft=0?cityId=10&sellChannel=7`;
  const data = await getJson(`${MAOYAN_BASE}${path}`, { headers: maoyanHeaders() });
  if (data.code !== 200) throw new Error('猫眼搜索失败：code=' + data.code + ' msg=' + data.msg);

  const rows = Array.isArray(data.data) ? data.data : [];
  const out = [];
  for (const row of rows) {
    if (!row || !row.performanceId) continue;
    const statusLabel = MAOYAN_TICKET_STATUS_LABELS[Number(row.ticketStatus)] || '';
    out.push({
      performanceId: String(row.performanceId),
      name: row.name || row.performanceName || String(row.performanceId),
      city: row.cityName || '',
      venue: row.shopName || row.venueName || '',
      dateRange: row.showTimeRange || '',
      price: row.priceRange || '',
      status: statusLabel,
      poster: row.posterUrl || row.verticalPic || row.poster || '',
    });
    if (out.length >= (limit || 20)) break;
  }
  return out;
}

/**
 * 抓一个演出的当前状态。
 *
 * 结构说明（实测踩过）：详情接口的 ``data`` **直接就是 performance 对象**，
 * 里面**没有场次列表**——所以只能拿到「项目级售票状态」（在售中 / 预售 / 售罄），
 * 拿不到票档余量。搜索接口才有 performances/sessions 那种嵌套，
 * 两者结构不一样，别照抄。
 */
// 猫眼「odea」登录通道（2026-10-14 实装，对照 ThinkerWen/TicketMonitoring 的 Monitor_MY.py）：
//   场次: POST https://wx.maoyan.com/my/odea/project/shows?token=<t>&clientPlatform=2  body {"projectId":<id>}
//         → data.showListVO[]: { showId, showName }
//   票档: GET https://wx.maoyan.com/my/odea/show/tickets?token=<t>&showId=<sid>&projectId=<pid>&clientPlatform=2
//         → data.ticketsVO[]: { ticketId, ticketPriceVO:{ticketPrice}, remainingStock }
// 匿名一律 403「请先登录」——所以 token 必须自带（config.json 的 myToken 或客户端随请求带）。
// m.dianping.com/myshow 的匿名详情只有项目级库存（inventoryCount/stockOut），没有票档粒度。
function myOdeaHeaders() {
  return {
    'User-Agent':
      'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) ' +
      'Mobile/15E148 MicroMessenger/8.0.53(0x18003531) NetType/WIFI Language/zh_CN',
    'X-Channel-ID': '70001',
    version: 'wallet-v5.10.9',
    'X-Requested-With': 'wxapp',
    'content-type': 'application/json',
    'x-wxa-referer': 'pages/show/detail/v2/index',
    'x-wxa-page': 'pages/showsubs/ticket-level/v2/index',
    Referer: 'https://servicewechat.com/wxdbb4c5f1b8ee7da1/1557/page-frame.html',
  };
}

/** odea 场次×票档全展开。返回 [{ showId, showName, tiers:[{name,price,stock}] }]。 */
async function myOdeaTicketLevels(projectId, token) {
  const showsRaw = await request(
    `https://wx.maoyan.com/my/odea/project/shows?token=${encodeURIComponent(token)}&clientPlatform=2`,
    {
      method: 'POST',
      headers: myOdeaHeaders(),
      body: JSON.stringify({ projectId: Number(projectId) }),
      timeout: 15000,
    },
  );
  let showsRes = null;
  try { showsRes = JSON.parse(showsRaw.body); } catch (e) { /* 落到下面统一报错 */ }
  if (!showsRes || showsRes.success === false || !showsRes.data) {
    throw new Error('猫眼登录凭证无效或过期（odea shows ' + JSON.stringify(showsRes && showsRes.error || {}).slice(0, 80) + '）');
  }
  const showList = showsRes.data.showListVO || [];
  const out = [];
  for (const s of showList.slice(0, 10)) {
    const ticketsRes = await getJson(
      `https://wx.maoyan.com/my/odea/show/tickets?token=${encodeURIComponent(token)}&showId=${s.showId}&projectId=${projectId}&clientPlatform=2`,
      { headers: myOdeaHeaders(), timeout: 15000 },
    );
    if (!ticketsRes || ticketsRes.success === false || !ticketsRes.data) continue;
    const tiers = (ticketsRes.data.ticketsVO || []).map((t) => {
      const price = t.ticketPriceVO && t.ticketPriceVO.ticketPrice != null ? String(t.ticketPriceVO.ticketPrice) : '';
      return {
        name: price ? (price.replace(/\.0+$/, '') + ' 元档') : ('票档 ' + t.ticketId),
        price: price ? '¥' + price.replace(/\.0+$/, '') : '',
        stock: Number(t.remainingStock) || 0,
      };
    });
    out.push({ showId: s.showId, showName: stripTags(s.showName) || '场次', tiers });
  }
  return out;
}

async function pollMaoyan(params) {
  const id = params.performance_id || params.id;
  if (!id) throw new Error('缺少 performance_id');
  // 登录通道：有 token 时走 odea，拿场次×票档×剩余库存的真实粒度，
  // 「只盯某个价位」靠 label 里的票价关键字就能罩住
  if (params.myToken) {
    const levels = await myOdeaTicketLevels(id, params.myToken);
    const items = [];
    for (const lv of levels) {
      if (!lv.tiers.length) {
        items.push({ label: lv.showName, place: '', when: lv.showName, status: '没挂出票档', available: false, count: 0 });
        continue;
      }
      for (const t of lv.tiers) {
        items.push({
          label: lv.showName + ' · ' + t.name,
          place: '',
          when: lv.showName,
          status: t.stock > 0 ? '可购' : '不可购',
          available: t.stock > 0,
          count: t.stock > 0 ? t.stock : 0,
          price: t.price,
        });
      }
    }
    if (items.length) return items;
    // odea 没回票档就落回匿名详情，别给用户一个空结果
  }
  const url = `${MAOYAN_BASE}/ajax/performance/${id}?sellChannel=7`;
  const data = await getJson(url, { headers: maoyanHeaders() });
  if (data.code !== 200) throw new Error('猫眼返回 code=' + data.code + ' msg=' + data.msg);

  const p = data.data || {};
  const label = MAOYAN_TICKET_STATUS_LABELS[Number(p.ticketStatus)];
  if (!label) {
    // 不猜：状态表对不上就说明平台改了映射，安静地当成「没票」会误报
    throw new Error(
      '猫眼详情的 ticketStatus=' + JSON.stringify(p.ticketStatus) + ' 看不懂，平台可能改了状态映射表',
    );
  }
  // stockOut 是**场次级**的缺货标记，ticketStatus 是**项目级**的。
  // 巡演横跨十几个城市，某个城市卖完了项目级依然是「在售中」——
  // 所以冲突时以 stockOut 为准。跟 Python 侧同一套判断，别只改一边。
  const conflict = p.stockOut === true && MAOYAN_AVAILABLE.indexOf(label) >= 0;
  const available = MAOYAN_AVAILABLE.indexOf(label) >= 0 && !conflict;
  return [
    {
      label: p.name || String(id),
      place: p.shopName || p.address || '',
      when: p.showTimeRange || '',
      status: conflict ? label + '·缺货标记' : label,
      available: available,
      count: available ? null : 0,
      price: p.priceRange || '',
    },
  ];
}

/**
 * 猫眼「场次列表」——**匿名可用**（2026-10-14 实测）。
 *   GET {MAOYAN_BASE}/ajax/performance/<id>/shows?sellChannel=7
 *   → data[] = [{ name:"2026-11-14 周六 19:00", showId, hasInventory,
 *                 ticketIdHasInventoryMap }]
 *
 * 为什么要它：项目级详情只回一个「整场价格区间」，此前的兜底代码把演出全名
 * 当成唯一「票档」塞进列表，详情页就出现「一个票档写着演出全名」这种荒唐显示
 * （用户截图反馈）。场次是真拿得到的，就把它摆出来——至少「哪几天」是准的。
 * 档位×剩余库存仍只有 odea 登录通道有（ticketClassIds 匿名实测 12/12 全空）。
 */
async function maoyanShows(id) {
  const data = await getJson(`${MAOYAN_BASE}/ajax/performance/${id}/shows?sellChannel=7`, {
    headers: maoyanHeaders(),
  });
  if (data.code !== 200) throw new Error('猫眼场次失败：code=' + data.code + ' msg=' + data.msg);
  const list = Array.isArray(data.data) ? data.data : [];
  return list.map((s) => {
    const inv = (s && s.ticketIdHasInventoryMap) || {};
    const anyStock = Object.keys(inv).some((k) => inv[k]);
    return {
      showId: s && s.showId,
      name: (s && s.name) || (s && s.startTimeTimeFormatted) || '场次',
      when: (s && s.name) || '',
      available: anyStock || (s && s.hasInventory === true),
    };
  });
}

/**
 * 猫眼「票价档位」——**匿名可用**（2026-10-14 实测，本轮新发现）。
 *   GET {MAOYAN_BASE}/ajax/performance/<id>/tickets?sellChannel=7
 *   → data[] = [{ ticketClassId, ticketPrice, hasInventory, showIds, rgb }]
 *
 * 为什么重要：此前注释里写「档位只有 odea 登录通道有」是**只查了 shows 接口
 * 的 ticketClassIds（匿名全空）就下的结论**。单独打 /tickets 匿名是通的——
 * 票价档位（480/680/980/1180）能如实拿到，所以详情页不该再只说一个
 * 「价格区间」，能把真实档位摆出来。
 *
 * 红线：`hasInventory` 匿名**恒为 false**（已用「在售中」的演出交叉验证：
 * 9.9 起的脱口秀 280 条档位、115 起的特展 594 条，全是 false），所以它
 * **不是**真库存信号——不能拿它标「售罄」，只能用来列价格档位。
 * 逐档库存仍要用户填猫眼凭证（odea 通道）。
 * 返回按价格去重后的数组：[{price:'480', name:'¥480'}]，保序。
 */
async function maoyanTickets(id) {
  const data = await getJson(`${MAOYAN_BASE}/ajax/performance/${id}/tickets?sellChannel=7`, {
    headers: maoyanHeaders(),
  });
  if (data.code !== 200) throw new Error('猫眼票档失败：code=' + data.code + ' msg=' + data.msg);
  const list = Array.isArray(data.data) ? data.data : [];
  const seen = {};
  const out = [];
  for (const t of list) {
    const price = t && t.ticketPrice != null ? String(t.ticketPrice).replace(/\.0+$/, '') : '';
    if (!price || seen[price]) continue;
    seen[price] = 1;
    out.push({ price, name: '¥' + price });
  }
  // 价格升序：档位列表按从低到高读着顺（平台返回顺序是 showId 序，不是价格序）
  out.sort((a, b) => Number(a.price) - Number(b.price));
  return out;
}


// ===========================================================================
// 平台：摩天轮（公开接口，POST + JSON）
// ===========================================================================

const MORE_BASE = 'https://unify.moretickets.com/user/foundation';
//: 公开接口前缀。别写成 "/" 根路径——那是错的，实测会回空壳。
const MORE_PUBLIC = '/pub';

const MORE_SOLD_OUT = ['sold out', '售罄', '已售完', '缺货'];

function moreHeaders() {
  return {
    'User-Agent': UA,
    'Content-Type': 'application/json',
    Accept: 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    Origin: 'https://moretickets.com',
    Referer: 'https://moretickets.com/',
  };
}

/**
 * 按关键词搜巡演，返回候选行。
 *
 * 路径 ``/show/search/v1`` **没有** ``/pub`` 前缀——跟 session/list 那个
 * 接口不一样，写错了只会拿到 12123 的空壳。
 *
 * ⚠️ 搜「孙燕姿」回来的 title 是 ``Sun Yanzi <AUT NIHILO> Tour In GUANGZHOU``。
 * 摩天轮的演出名**本来就是英文原文**，不是我们没匹配上。所以小程序列表里
 * 一定要把 location（Guangzhou, CN）显示出来，否则用户看到一堆英文会以为搜错了。
 */
async function findMoretickets(keyword, limit) {
  const body = {
    keyword: keyword,
    homeUiExperiment: true,
    offset: 0,
    length: Math.min(Number(limit) || 20, 50),
  };
  const r = await request(`${MORE_BASE}/show/search/v1`, {
    method: 'POST',
    headers: moreHeaders(),
    body: JSON.stringify(body),
  });

  let data;
  try {
    data = JSON.parse(r.body);
  } catch (e) {
    throw new Error('摩天轮搜索返回的不是 JSON（它只认 POST + JSON）');
  }
  const code = data.statusCode;
  if (code !== 0 && code !== 200 && code !== '0') {
    throw new Error('摩天轮搜索失败：statusCode=' + code + ' ' + (data.message || data.msg || ''));
  }

  const rows = Array.isArray(data.data) ? data.data : [];
  const out = [];
  for (const row of rows) {
    if (!row || !row.tourId) continue;
    // 字段名是实测出来的，别照想象写：
    //   title/showName 演出名、location 城市、showDate 档期、
    //   price 是**对象** {minSalePrice, currencySymbol}、imgUrl 海报。
    const price = row.price || {};
    out.push({
      tourId: row.tourId,
      name: row.title || row.showName || row.tourId,
      city: row.location || row.cityCode || '',
      venue: row.venueName || '',
      dateRange: row.showDate || '',
      price:
        price.minSalePrice != null
          ? String(price.currencySymbol || '') + price.minSalePrice
          : '',
      poster: row.imgUrl || '',
    });
    if (out.length >= body.length) break;
  }
  return out;
}

/**
 * 12306 线路搜索。
 *
 * 用户填的是中文站名（广州南 长沙南），但接口只认电报码。
 * 复用 pollRail 的查询逻辑，只是把返回值转成「演出列表」那套字段，
 * 这样小程序的结果卡片不用为 12306 单独写一套渲染。
 */
async function findRail(keyword, date, limit) {
  const seg = String(keyword || '').trim().split(/\s+/).filter(Boolean);
  if (seg.length < 2) throw new Error('要填两个站名，中间空格隔开');
  const day = String(date || '').trim();
  if (!day) throw new Error('缺少乘车日期');

  // pollRail 内部自己会取站名表做转换，这里不用重复取。
  const items = await pollRail({ from: seg[0], to: seg[1], date: day });
  const out = [];
  for (const it of items.slice(0, Math.min(Number(limit) || 20, 50))) {
    out.push({
      performanceId: it.label,   // 车次号，复用 id 字段
      name: it.label,
      city: seg[0],
      venue: it.place || '',
      dateRange: it.when || '',
      price: '',
      status: it.status || (it.available ? '有票' : '无票'),
      poster: '',
      route: it.place || '',
    });
  }
  return out;
}

async function pollMoretickets(params) {
  let tourId = params.tour_id;

  // 没给 tour_id 就按关键词自己搜。
  // 这一条是小程序「场次」页能用的关键：用户只要打「孙燕姿」三个字，
  // 不用去别的 App 里翻链接、抠 tour_id——竞品逼用户干的就是这件事。
  if (!tourId) {
    const kw = String(params.keyword || '').trim();
    if (!kw) throw new Error('既没给 tour_id 也没给 keyword');
    const rows = await findMoretickets(kw, 1);
    if (!rows.length) {
      throw new Error(
        '摩天轮搜不到「' + kw + '」。它是二手票平台，' +
          '冷门场次确实可能没挂单；换个说法（英文名/城市名）再试。',
      );
    }
    tourId = rows[0].tourId;
  }

  if (!tourId) throw new Error('缺少 tour_id');

  // 真实路径和 body 都是实测出来的，别凭印象改：
  //   路径  /pub/session/city/list/v1
  //   body  {"tourId": "..."}   —— 只这一个字段
  // 只认 POST+JSON；同一个地址用 GET 会返回 111 字节空壳（statusCode 12123），
  // 那个码不是业务状态，是「你姿势不对」。
  const r = await request(`${MORE_BASE}${MORE_PUBLIC}/session/city/list/v1`, {
    method: 'POST',
    headers: moreHeaders(),
    body: JSON.stringify({ tourId: tourId }),
  });

  let data;
  try {
    data = JSON.parse(r.body);
  } catch (e) {
    throw new Error(
      '响应不是 JSON（statusCode 12123 说明姿势不对——它只认 POST + JSON）',
    );
  }
  const code = data.statusCode;
  if (code !== 0 && code !== 200 && code !== '0') {
    throw new Error(
      '摩天轮返回 statusCode=' + code + ' msg=' + (data.message || data.msg || ''),
    );
  }

  const body = data.data || {};
  const groups = body.sessionGroupList || [];
  const wantCity = params.city || '';
  const enCity = cityEn(wantCity);
  const items = [];
  for (const g of groups) {
    const region = g.regionName || ''; // 形如 "Guangzhou, CN"
    // 城市中英都要试：配置里写「广州」，平台回的是 "Guangzhou, CN"
    if (wantCity && region.indexOf(wantCity) < 0 && (!enCity || region.toLowerCase().indexOf(enCity) < 0)) {
      continue;
    }
    for (const s of g.sessionList || []) {
      // hasTicket 是唯一可信的余票信号（布尔，没歧义）。
      // sessionStatusDesc 会随站点语言变（中/英/繁三套），只当旁证。
      // hasTicket 缺失 → 跳过这一场，不猜。
      if (typeof s.hasTicket !== 'boolean') continue;
      const desc = String(s.sessionStatusDesc || '');
      const available = s.hasTicket;
      const soldOutByText = MORE_SOLD_OUT.indexOf(desc.toLowerCase()) >= 0;
      items.push({
        label: (s.sessionName || '') + (s.venueName ? ' ' + s.venueName : ''),
        place: region,
        when: s.sessionTime || s.sessionName || '',
        status: desc || (available ? '有票' : '无票'),
        // 两个信号打架时按「没票」处理——宁可漏报不误报
        available: available && !soldOutByText,
        count: available ? null : 0,
      });
    }
  }
  return items;
}

/** 城市中英对照：平台返回 "Guangzhou, CN"，配置里写「广州」。 */
const CITY_EN = {
  深圳: 'shenzhen', 香港: 'hongkong', 广州: 'guangzhou', 上海: 'shanghai',
  北京: 'beijing', 澳门: 'macau', 台北: 'taipei', 成都: 'chengdu', 杭州: 'hangzhou',
  重庆: 'chongqing', 南京: 'nanjing', 武汉: 'wuhan', 西安: 'xian', 长沙: 'changsha',
  苏州: 'suzhou', 青岛: 'qingdao', 天津: 'tianjin', 东京: 'tokyo', 大阪: 'osaka',
  首尔: 'seoul', 曼谷: 'bangkok', 新加坡: 'singapore', 吉隆坡: 'kualalumpur',
};

function cityEn(name) {
  return CITY_EN[name] || '';
}

// ===========================================================================
// 平台：12306
// ===========================================================================

const RAIL_BASE = 'https://kyfw.12306.cn';
const RAIL_INIT = `${RAIL_BASE}/otn/leftTicket/init`;
const RAIL_STATION_JS = `${RAIL_BASE}/otn/resources/js/framework/station_name.js`;
// 官方会轮换这几个端点，按顺序试到能用为止。
// queryG 实测（2026-10-14）是当前在用的：queryE 会 302 到一个
// {"c_url":"leftTicket/queryG"} 的提示，说明官方挪到了 G。
const RAIL_ENDPOINTS = ['queryG', 'queryE', 'queryZ', 'queryA', 'query'];

const TRAIN_FIELDS = {
  train_code: 3,
  from_station_code: 6,
  to_station_code: 7,
  depart_time: 8,
  arrive_time: 9,
  duration: 10,
};

const SEAT_FIELDS = {
  21: '高级软卧', 22: '其他', 23: '软卧', 24: '软座', 25: '特等座', 26: '无座',
  27: '硬卧包房', 28: '硬卧', 29: '硬座', 30: '二等座', 31: '一等座', 32: '商务座',
  33: '高级动卧', 34: '动卧', 35: '餐车', 36: '特等座(动)', 37: '一等座(动)',
  38: '二等座(动)', 39: '无座(动)', 40: '高级软卧(动)', 41: '软卧(动)',
};

function railHeaders(cookie) {
  const h = {
    'User-Agent': UA,
    // Referer 必须是 init 页，不然 12306 直接返回风控页
    Referer: RAIL_INIT,
    Accept: 'application/json, text/javascript, */*; q=0.01',
    'Accept-Language': 'zh-CN,zh;q=0.9',
  };
  if (cookie) h.Cookie = cookie;
  return h;
}

/** 先访问 init 页拿会话 Cookie——12306 没有它会返回拦截页。 */
async function warmUpRail() {
  const r = await request(RAIL_INIT, { headers: railHeaders(), timeout: 15000 });
  const raw = r.headers['set-cookie'];
  if (!raw) return null;
  return raw.map((c) => c.split(';')[0]).join('; ');
}

async function fetchStationMap() {
  // 站名表很大（几千行），缓存在云存储里，一天取一次就够
  const cached = await cloud.getStorage(CFG_KEY + '_stations');
  if (cached && cached.ts && Date.now() - cached.ts < 86400000) {
    return cached.map;
  }
  const r = await request(RAIL_STATION_JS, { headers: railHeaders(), timeout: 20000 });
  // 每段形如：bjb|北京北|VAP|beijingbei|bjb|0|0357|北京|||
  //   索引 0 = 电报码（bjb）　索引 1 = 中文站名　索引 2 = 站码（VAP）
  //
  // **两种 key 都要建**：实测余票接口返回的起终点是站码（GZQ），
  // 而电报码那套也不是所有车次都有。少建一种，界面上就剩一串没人认识的字母。
  const map = {};
  for (const entry of r.body.split('@')) {
    const parts = entry.split('|');
    if (parts.length < 2 || !parts[1]) continue;
    if (parts[0]) map[parts[0]] = parts[1];
    if (parts[2]) map[parts[2]] = parts[1];
  }
  if (Object.keys(map).length > 100) {
    await cloud.setStorage({ key: CFG_KEY + '_stations', value: { ts: Date.now(), map: map } });
  }
  return map;
}

/**
 * 站名 → 电报码。
 *
 * **12306 的余票接口只认电报码**（如 GZQ、IZQ），直接把「广州南」丢给它
 * 会返回空或 status=false。而用户在界面上填的偏偏是中文名。
 * Python 端 parse_station_js 也做同样的转换——两边必须一致，
 * 否则「本地能查、小程序查不了」这种 bug 极难定位。
 *
 * 查不到就回传原值：让 12306 自己报错，比我们猜一个码强。
 */
function resolveStationCode(name, map) {
  const raw = String(name || '').trim();
  if (!raw) return '';
  // map 里一个站名挂了两个 key：小写电报码（izq）+ 大写站码（IZQ）。
  // 余票接口**只认大写站码**——传小写的会直接 302 到风控 error 页，
  // 所以同名多个 key 命中时，优先挑全大写的那个。
  let fallback = '';
  for (const code of Object.keys(map)) {
    if (map[code] === raw) {
      if (/^[A-Z0-9]+$/.test(code)) return code;
      if (!fallback) fallback = code;
    }
  }
  return fallback || raw;
}

/** 12306 余票查询公共段：站码转换 + 端点轮换，返回原始行与站名表。
 *  pollRail（盯票）和 railDetail（详情页）共用，别在两处各抄一份。 */
async function railQueryRaw(from, to, date) {
  if (!from || !to || !date) throw new Error('缺少 from / to / date');

  const stationMap = await fetchStationMap();
  // 12306 只认电报码，用户填的是中文名——这里做一次转换。
  // 转换不了就把原值传过去让它自己报错，比我们瞎猜一个码强。
  const fromCode = resolveStationCode(from, stationMap);
  const toCode = resolveStationCode(to, stationMap);

  const cookie = await warmUpRail();
  const q =
    'leftTicketDTO.train_date=' + encodeURIComponent(date) +
    '&leftTicketDTO.from_station=' + encodeURIComponent(fromCode) +
    '&leftTicketDTO.to_station=' + encodeURIComponent(toCode) +
    '&purpose_codes=ADULT';

  let data = null;
  let lastErr = null;
  const tried = new Set();
  let ep = RAIL_ENDPOINTS[0];
  // 官方会轮换端点：老端点会回 {"c_url":"leftTicket/queryX"} 的提示而不带数据，
  // 所以除了逐个试列表，还要跟着 c_url 的提示走（官方以后再挪也不用改代码）。
  while (ep && !tried.has(ep) && tried.size <= RAIL_ENDPOINTS.length + 2) {
    tried.add(ep);
    try {
      data = await getJson(`${RAIL_BASE}/otn/leftTicket/${ep}?${q}`, {
        headers: railHeaders(cookie),
      });
      if (data && data.c_url && !data.data) {
        const hinted = String(data.c_url).split('/').pop();
        if (hinted && !tried.has(hinted)) {
          ep = hinted;
          data = null;
          continue;
        }
      }
      break;
    } catch (e) {
      lastErr = e;
      const next = RAIL_ENDPOINTS.indexOf(ep) + 1;
      ep = RAIL_ENDPOINTS[next];
    }
  }
  if (!data) throw lastErr || new Error('所有端点都失败了');
  if (data.httpstatus !== 200 && data.status === false) {
    throw new Error('12306 返回 status=false（日期超预售期或站码不对）');
  }
  const result = data.data || {};
  return { rows: result.result || [], stationMap: stationMap };
}

async function pollRail(params) {
  const { from, to, date, seat_types: seatTypes } = params;

  // 过期日期拦截（2026-10-19 实测钉死）：12306 对过去日期返回的是 HTML
  // 错误页，曾被我当成「风控拦截/云 IP 被拒」来报错，纯属误导——
  // 用户只是选了已过去的日子。这里按北京时间提前拦下，说人话。
  const bjToday = new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10);
  const railDay = String(date || '').trim();
  if (/^\d{4}-\d{2}-\d{2}$/.test(railDay) && railDay < bjToday) {
    throw new Error('乘车日期 ' + railDay + ' 已经过期——12306 只能查 ' + bjToday + ' 及以后的车次，换个日期再查');
  }

  const { rows, stationMap } = await railQueryRaw(from, to, date);

  const wanted = seatTypes && seatTypes.length ? seatTypes : ['二等座'];
  const items = [];
  for (const row of rows) {
    if (!row) continue;
    const f = row.split('|');
    const code = field(f, TRAIN_FIELDS.train_code).trim();
    if (!code) continue;
    const seats = [];
    for (const idx of Object.keys(SEAT_FIELDS)) {
      const name = SEAT_FIELDS[idx];
      if (wanted.indexOf(name) < 0) continue;
      const s = parseSeat(name, field(f, Number(idx)));
      if (s && s.available) seats.push(s);
    }
    if (!seats.length) continue;
    const dep = field(f, TRAIN_FIELDS.from_station_code).trim();
    const arr = field(f, TRAIN_FIELDS.to_station_code).trim();
    items.push({
      label: code,
      place: (stationMap[dep] || dep) + ' → ' + (stationMap[arr] || arr),
      when: field(f, TRAIN_FIELDS.depart_time).trim() + '–' + field(f, TRAIN_FIELDS.arrive_time).trim(),
      status: seats.map((s) => s.seat_type + ' ' + (s.count == null ? s.raw : s.count + '张')).join('、'),
      available: true,
      count: seats.length,
    });
  }
  return items;
}

/** 12306 详情页：单趟车次的全部席别×余票（无票的也列出，标「无」）。
 *  和 pollRail 的区别：盯票只关心有票的席别；详情要展示全量让用户挑。 */
async function railDetail(from, to, date, trainCode) {
  const { rows, stationMap } = await railQueryRaw(from, to, date);
  const want = String(trainCode || '').trim().toUpperCase();
  for (const row of rows) {
    if (!row) continue;
    const f = row.split('|');
    const code = field(f, TRAIN_FIELDS.train_code).trim();
    if (code.toUpperCase() !== want) continue;
    const dep = field(f, TRAIN_FIELDS.from_station_code).trim();
    const arr = field(f, TRAIN_FIELDS.to_station_code).trim();
    const depTime = field(f, TRAIN_FIELDS.depart_time).trim();
    const arrTime = field(f, TRAIN_FIELDS.arrive_time).trim();
    const tiers = [];
    for (const idx of Object.keys(SEAT_FIELDS)) {
      const name = SEAT_FIELDS[idx];
      const s = parseSeat(name, field(f, Number(idx)));
      if (!s) continue; // 该车次不设这个席别
      tiers.push({
        name: name,
        price: '',
        status: s.available ? (s.count == null ? s.raw : s.count + ' 张') : '无票',
        available: s.available,
      });
    }
    return {
      platform: 'rail12306',
      id: code,
      name: code + ' 次',
      venue: (stationMap[dep] || dep) + ' → ' + (stationMap[arr] || arr),
      sessions: [{
        name: depTime + ' 开 – ' + arrTime + ' 到',
        when: depTime + '–' + arrTime,
        status: '',
        available: tiers.some((t) => t.available),
        tiers: tiers,
      }],
    };
  }
  throw new Error(
    '没查到 ' + trainCode + ' 次——确认日期在预售期内、出发地/目的地和盯票时一致（12306 只返回两站之间的车）'
  );
}

// ===========================================================================
// 平台：大麦
// ===========================================================================
// 2026-10-14 全线重构。旧路线全灭的实锤：
//   · m.damai.cn（H5 站）302 → outofservice.html，**整站下线**；
//   · detail.damai.cn/item.htm 302 → m.damai.cn/damai/detail/item.html（死），
//     所以旧的「详情页 SSR 解析」路线拿不到任何数据了；
//   · search.damai.cn/searchajax 老接口对非浏览器 IP 直接 x5sec 滑块——死路。
// 现役唯一路线（对照 ThinkerWen/TicketMonitoring 的 Monitor_DM.py，开源可查）：
//   全部走 **mtop.damai.cn** 网关 + 大麦官方微信小程序壳
//   （Referer: servicewechat.com/wx938b41d0d7e8def0/...，UA 带 MicroMessenger）：
//     1) 搜索: mtop.damai.wireless.search.search v1.0 → data.projectInfo
//     2) 详情: mtop.damai.item.detail.getdetail v1.0 → data.legacy(JSON字符串)
//              → detailViewComponentMap.item.item.performBases[].performs[]
//     3) 票档: mtop.alibaba.detail.subpage.getdetail v2.0 → data.result(JSON字符串)
//              → perform.skuList[{skuId, priceName, skuSalable}]
//   _m_h5_tk 是网关下发的**匿名**令牌（不是用户凭据），签名 = md5(token&t&appKey&data)，
//   首次请求通常 TOKEN_EMPTY，网关同时种下令牌，重试一次即成功。
//   被风控（RGV587 滑块）时响应带顶层 c 字段，Monitor_DM 的做法是带 c 参数重试。
// 仍不能做：下单。

/** 从任意大麦链接/文本里抠 itemId。 */
function extractDamaiId(text) {
  const m = String(text || '').match(/(?:id=(\d{6,})|itemId[=:"]+(\d{6,}))/i);
  return (m && (m[1] || m[2])) || '';
}

// ---------------------------------------------------------------------------
// 大麦 mtop 匿名通道（2026-10-14 重构）
// ---------------------------------------------------------------------------
// 历史坑（都踩过）：
//   · search.damai.cn/searchajax 老接口对非浏览器 IP 直接 x5sec 风控——死路；
//   · h5api.m.taobao.com 网关 2026-10 下旬起对 damai API 回
//     FAIL_SYS_API_NOT_FOUNDED——damai 路由整体迁到 **mtop.damai.cn**；
//   · 必须装成大麦官方微信小程序壳（Referer wx938b41d0d7e8def0 + MicroMessenger
//     UA）。**不要带 x-tap: wx 头**——实测带了反而被网关路由到没有无线搜索
//     API 的后端，回 API_NOT_FOUNDED；不带则 SUCCESS（Monitor_DM 之所以带是
//     因为它调的是详情 API，搜索/详情两条路的路由行为不同）。
//   · 疯狂连发会触发 RGV587 滑块风控（本机 IP 连跪时要冷却几十分钟），
//     所以这里内置退避重试 + 友好报错，不在网关上硬刚。
//
// 2026-10-14 第二轮修正（**推翻上面「搜索要 Cookie」的结论**）：
//   用真实浏览器抓 m.damai.cn/shows/*（新版 H5，show-h5-next）的网络，发现
//   搜索/分类页走的是 mtop.damai.cn 的**另一套请求形态**——同一 API 名，
//   只要带 `H5Request=true & forceAntiCreep=true & AntiCreep=true` 且
//   payload 里补 `platform/comboChannel/dmChannel`，匿名零 Cookie 就能通。
//   见 mtopH5Get / damaiSearch / damaiHot。上面那段「不带 H5Request 的搜索
//   被 RGV587」只对**旧请求形态**成立。

const MTOP_APPKEY = '12574478';
const MTOP_BASE = 'https://mtop.damai.cn/h5';
const MTOP_UA =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 ' +
  '(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.53(0x18003531) NetType/WIFI Language/zh_CN';
// 进程级缓存令牌 cookie；云函数实例常驻时复用，冷启动多一次重试
let mtopCookie = '';

function mtopAttempt(api, dataObj, v, cVal, userCookie, extraQ) {
  const t = Date.now().toString();
  const data = JSON.stringify(dataObj);
  const tkAt = mtopCookie.indexOf('_m_h5_tk=');
  const token = tkAt >= 0 ? mtopCookie.slice(tkAt + 9).split(';')[0].split('_')[0] : '';
  const sign = crypto.createHash('md5').update(`${token}&${t}&${MTOP_APPKEY}&${data}`).digest('hex');
  const headers = {
    'User-Agent': MTOP_UA,
    Referer: 'https://servicewechat.com/wx938b41d0d7e8def0/350/page-frame.html',
    Accept: 'application/json',
  };
  // 匿名令牌 + 用户自带 Cookie（可选）：登录态 Cookie 是过 RGV587 风控的
  // 最稳通道（Monitor_DM 同款）——匿名被拦时用户粘一次 Cookie 就能走
  // 注意：用户 Cookie 里的 _m_h5_tk 必须剥掉——签名用的是匿名 jar 的令牌，
  // 若用户 Cookie 带着过期令牌排在后面，服务端按它校验签名 → 永远 TOKEN_EXOIRED
  const cookieParts = [];
  if (mtopCookie) cookieParts.push(mtopCookie);
  if (userCookie) {
    const clean = String(userCookie).replace(/^Cookie:\s*/i, '').trim()
      .split(';')
      .filter((c) => !/^\s*_m_h5_tk(_enc)?\s*=/.test(c))
      .join('; ')
      .trim();
    if (clean) cookieParts.push(clean);
  }
  if (cookieParts.length) headers.Cookie = cookieParts.join('; ');
  // c 参数：被 AntiCreep 拦时响应带顶层 c 字段，带回去重试（Monitor_DM 同款）
  const cQ = cVal ? '&c=' + encodeURIComponent(cVal) : '';
  // AntiCreep 等额外参数：详情类接口（getdetail/subpage）不带这组会被
  // 网关当可疑流量直接 RGV587——搜索接口则不需要。键值对透传。
  let ex = '';
  for (const k of Object.keys(extraQ || {})) {
    ex += '&' + encodeURIComponent(k) + '=' + encodeURIComponent(extraQ[k]);
  }
  return request(
    `${MTOP_BASE}/${api}/${v}/?jsv=2.7.2&appKey=${MTOP_APPKEY}&t=${t}` +
      `&sign=${sign}&api=${api}&v=${v}&type=originaljson&dataType=json&data=${encodeURIComponent(data)}${cQ}${ex}`,
    { headers, timeout: 15000 },
  ).then((r) => {
    const sc = r.headers['set-cookie'];
    if (sc) mtopCookie = sc.map((c) => c.split(';')[0]).join('; ');
    let j;
    try {
      j = JSON.parse(r.body);
    } catch (e) {
      throw new Error('大麦 mtop 返回非 JSON：' + r.body.slice(0, 100));
    }
    return j;
  });
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// ---------------------------------------------------------------------------
// 大麦官方 H5 详情通道（2026-10-14 实测：匿名可用，不用 Cookie）
//
// 之前一直用 mtop.damai.item.detail.getdetail，被 RGV587 风控钉死。
// 实测官方 H5/小程序走的是 **mtop.alibaba.damai.detail.getdetail v1.2**：
//   · 与搜索接口相反，它**必须带 c 参数**（_m_h5_tk 令牌）参与签名，
//     不带/带错立刻 RGV587；
//   · **必须带 x-tap: wx 头**——带对了才路由到正确后端；
//   · 首包服务器会下发 _m_h5_tk（Set-Cookie），拿它重签一次即 SUCCESS。
// 返回 detailViewComponentMap.item：演出名/海报/时间/城市/价格区间 +
// performBases（场次列表）。票档级 subpage 接口仍被风控（见 damaiSessionTiers）。
// ---------------------------------------------------------------------------
let dmH5Tk = ''; // _m_h5_tk 的值（形如 <token>_<ts>），进程级复用
let dmH5Cookie = ''; // 完整 Cookie 头

function dmTokenOf(cookieStr) {
  const m = /(?:^|;\s*)_m_h5_tk=([^;]+)/.exec(String(cookieStr || ''));
  return m ? m[1].split('_')[0] : '0';
}

async function mtopH5(api, v, dataObj, extraQ, userCookie) {
  const data = JSON.stringify(dataObj || {});
  let c = dmH5Tk;
  for (let attempt = 0; attempt < 5; attempt++) {
    const t = Date.now().toString();
    const token = c ? String(c).split('_')[0] : '0';
    const cc = c || '0_' + t;
    const sign = crypto.createHash('md5').update(`${token}&${t}&${MTOP_APPKEY}&${data}`).digest('hex');
    let ex = '';
    for (const k of Object.keys(extraQ || {})) {
      ex += '&' + encodeURIComponent(k) + '=' + encodeURIComponent(extraQ[k]);
    }
    const url =
      `${MTOP_BASE}/${api}/${v}/?jsv=2.4.12&appKey=${MTOP_APPKEY}&t=${t}&sign=${sign}` +
      `&c=${encodeURIComponent(cc)}&api=${api}&v=${v}&type=json&dataType=json&env=m` +
      `&data=${encodeURIComponent(data)}${ex}`;
    const headers = {
      'User-Agent': MTOP_UA,
      Referer: 'https://servicewechat.com/wx938b41d0d7e8def0/350/page-frame.html',
      Accept: 'application/json',
      'content-type': 'application/x-www-form-urlencoded',
      'x-tap': 'wx',
    };
    const cks = [];
    if (dmH5Cookie) cks.push(dmH5Cookie);
    if (userCookie) cks.push(String(userCookie).replace(/^Cookie:\s*/i, '').trim());
    if (cks.length) headers.Cookie = cks.join('; ');
    const r = await request(url, { headers, timeout: 15000 });
    const sc = r.headers['set-cookie'];
    if (sc) {
      dmH5Cookie = sc.map((x) => x.split(';')[0]).join('; ');
      const m = /(?:^|;\s*)_m_h5_tk=([^;]+)/.exec(dmH5Cookie);
      if (m) dmH5Tk = m[1];
    }
    let j;
    try {
      j = JSON.parse(r.body);
    } catch (e) {
      throw new Error('大麦返回非 JSON：' + r.body.slice(0, 100));
    }
    const ret = (j.ret || []).join('|');
    if (/SUCCESS/.test(ret)) return j;
    const nc = j.c || (j.data && j.data.c) || dmH5Tk;
    if (nc && nc !== cc) {
      c = nc;
      continue;
    }
    throw damaiFail(ret);
  }
  throw new Error('大麦详情连续失败');
}

/** 在任意嵌套 JSON 里找第一个非空字符串字段（场馆名等散落在组件树里）。 */
function deepStr(o, key) {
  if (o == null) return '';
  if (Array.isArray(o)) {
    for (const v of o) {
      const r = deepStr(v, key);
      if (r) return r;
    }
    return '';
  }
  if (typeof o === 'object') {
    if (typeof o[key] === 'string' && o[key]) return o[key];
    for (const k of Object.keys(o)) {
      const r = deepStr(o[k], key);
      if (r) return r;
    }
  }
  return '';
}

/** 大麦官方 H5 详情（匿名）。返回 {name,poster,dateRange,city,venue,priceRange,sessions}。 */
async function damaiDetailV12(itemId, userCookie) {
  const j = await mtopH5(
    'mtop.alibaba.damai.detail.getdetail',
    '1.2',
    { itemId: String(itemId), dmChannel: 'damai@weixin_weapp' },
    { H5Request: 'true', valueType: 'string' },
    userCookie,
  );
  const raw = j && j.data && j.data.result;
  if (!raw) throw new Error('大麦详情返回为空');
  const res = JSON.parse(raw);
  const itemWrap = (res.detailViewComponentMap && res.detailViewComponentMap.item) || {};
  const base = (itemWrap.staticData && itemWrap.staticData.itemBase) || {};
  const it = itemWrap.item || {};
  const sessions = [];
  for (const b of (it.performBases || []).slice(0, 12)) {
    for (const p of b.performs || []) {
      const tiers = [];
      for (const sk of p.skuList || p.priceList || []) {
        tiers.push({
          name: stripTags(sk.priceName || sk.name || sk.skuName || '票档'),
          price: stripTags(
            sk.priceStr || sk.formattedPriceStr || (sk.price != null ? '¥' + sk.price : ''),
          ),
          status: '',
          available: !(sk.skuSalable === false || sk.skuSalable === 'false'),
        });
      }
      sessions.push({
        name: stripTags(p.performName || '场次'),
        when: stripTags(p.performName || ''),
        performId: p.performId || '',
        status: '',
        available: false,
        tiers,
      });
    }
  }
  // 巡演分站（匿名可读，本轮新解析）：父项目（如「许嵩 南宁站」的巡回总条目）
  // performBases 是空的，但 staticData.tourProjects 里带着各分站的 itemId/城市/档期。
  // 详情页据此渲染「巡演分站」列表让用户点进去——否则点进来一片空白，
  // 用户以为坏了（本轮反馈的原话：「点进去都是空白」）。
  const stops = [];
  for (const t of ((itemWrap.staticData && itemWrap.staticData.tourProjects) || []).slice(0, 20)) {
    const sid = String(t.itemId || '');
    if (!sid) continue;
    stops.push({
      itemId: sid,
      name: stripTags(t.cityName) || stripTags(t.name) || ('大麦 #' + sid),
      when: stripTags(t.showTime),
    });
  }
  return {
    name: stripTags(base.itemName || ''),
    poster: stripTags(base.itemPic || ''),
    dateRange: stripTags(base.showTime || ''),
    city: stripTags(base.cityName || ''),
    venue: stripTags(deepStr(res, 'venueName') || deepStr(res, 'venue') || ''),
    priceRange: stripTags(it.priceRange || ''),
    sessions,
    stops,
    source: 'h5',
  };
}

/** 单场次票档（subpage，需 Cookie 通道；匿名被 RGV587 时抛错，调用方降级）。 */
async function damaiSessionTiers(itemId, performId, userCookie) {
  const j = await mtopH5(
    'mtop.alibaba.detail.subpage.getdetail',
    '2.0',
    {
      itemId: String(itemId),
      bizCode: 'ali.china.damai',
      scenario: 'itemsku',
      exParams: JSON.stringify({ dataType: 2, dataId: String(performId) }),
      platform: '8',
      comboChannel: '2',
      dmChannel: 'damai@damaih5_h5',
    },
    {
      v: '2.0',
      H5Request: 'true',
      type: 'originaljson',
      timeout: '10000',
      valueType: 'original',
      forceAntiCreep: 'true',
      AntiCreep: 'true',
      useH5: 'true',
    },
    userCookie,
  );
  const raw = j && j.data && j.data.result;
  if (!raw) return [];
  const res = JSON.parse(raw);
  const skus = (res.perform && res.perform.skuList) || res.skuList || [];
  return skus.map((sk) => ({
    name: stripTags(sk.priceName || sk.name || '票档'),
    price: stripTags(sk.priceStr || (sk.price != null ? '¥' + sk.price : '')),
    status: '',
    available: !(sk.skuSalable === false || sk.skuSalable === 'false'),
  }));
}

/** 把 mtop 的失败 ret 翻译成用户能看懂、我们知道怎么办的话。 */
/** 把 mtop 的失败 ret 翻译成用户能看懂的话。管理员怎么配 Cookie 是运维的事
 * （server/config.example.json 里写了），不漏进用户界面。 */
function damaiFail(ret) {
  if (/RGV587|USER_VALIDATE/.test(ret)) {
    return new Error('大麦人太多排起了长队，详情暂时拿不到，稍后再试试吧');
  }
  return new Error('大麦 mtop：' + ret);
}

/**
 * 大麦详情 SSR 降级（2026-10-14 实测）：
 * PC 详情页 detail.damai.cn/item.htm?id=<id> 匿名稳定可达（33KB SSR HTML），
 * 标题与 meta description 里有演出全名、日期区间、场馆——
 * mtop 详情接口被风控拦时用它兜底，详情页演出信息照常渲染，
 * 只是拿不到场次×票档（那部分只有 Cookie 通道有）。
 * 返回 { name, venue, dateRange, source: 'ssr' }；页面拉不到时抛错。
 */
function damaiDetailSSR(itemId) {
  return new Promise((resolve, reject) => {
    const req = require('https').get(
      'https://detail.damai.cn/item.htm?id=' + encodeURIComponent(itemId),
      {
        headers: {
          'User-Agent':
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
          'Accept-Language': 'zh-CN,zh;q=0.9',
        },
      },
      (res) => {
        let b = '';
        res.on('data', (c) => (b += c));
        res.on('end', () => {
          try {
            const t = b.match(/<title>([^<]+)<\/title>/);
            const d = b.match(/name="description" content="([^"]*)"/);
            if (!t || !t[1]) return reject(new Error('SSR 页面无标题'));
            // 「XXX【网上订票】- 大麦网」→ 去尾巴拿演出名
            const name = t[1]
              .replace(/【网上订票】\s*[-－]\s*大麦网\s*$/, '')
              .replace(/\s*[-－]\s*大麦网\s*$/, '')
              .replace(/【网上订票】/g, '')
              .trim();
            const desc = d ? d[1] : '';
            // 描述里挖日期区间与场馆：「将于2026.10.30-11.01在广西体育中心主体育场上演」
            let dateRange = '';
            const dm = desc.match(/(\d{4}\.\d{1,2}\.\d{1,2}(?:\s*[-~至]\s*(?:\d{4}\.)?\d{1,2}\.\d{1,2})?)/);
            if (dm) dateRange = dm[1];
            let venue = '';
            const vm = desc.match(/在([^，。、！？]{2,30}?)(?:上演|举办|开演|举行|唱响)/);
            if (vm) venue = vm[1].trim();
            if (!name && !dateRange && !venue) return reject(new Error('SSR 页面无可解析信息'));
            resolve({ name: name || '', venue: venue || '', dateRange: dateRange || '', source: 'ssr' });
          } catch (e) {
            reject(e);
          }
        });
      }
    );
    req.setTimeout(12000, () => req.destroy(new Error('SSR 页面超时')));
    req.on('error', reject);
  });
}

async function mtopCall(api, dataObj, v, userCookie, extraQ) {
  let j = await mtopAttempt(api, dataObj, v, undefined, userCookie, extraQ);
  let ret = (j.ret || [])[0] || '';
  let tries = 0;
  let cookie = userCookie;
  while (!/SUCCESS/.test(ret) && tries < 4) {
    // TOKEN_EXOIRED 不是笔误——阿里网关返回的官方串就少个 P，必须照抄才匹配得到
    if (/TOKEN_EMPTY|TOKEN_EXPIRED|TOKEN_EXOIRED/.test(ret)) {
      // 用户 Cookie 过期/失效会让签名与令牌对不上 → 丢掉它降级匿名重试
      if (cookie && /TOKEN_EXPIRED|TOKEN_EXOIRED/.test(ret)) cookie = '';
      await sleep(200 + tries * 150); // 匿名令牌刚下发，重签即可
    } else if (/API_NOT_FOUNDED/.test(ret)) {
      mtopCookie = ''; // 丢弃可能错路的匿名令牌，重新签发
      await sleep(200 + tries * 150);
    } else if (/RGV587|USER_VALIDATE/.test(ret) && j.c) {
      await sleep(500); // 风控软拦：带响应里的 c 参数再试一次（Monitor_DM 同款）
    } else {
      break; // 其他错误重试无意义
    }
    j = await mtopAttempt(api, dataObj, v, /RGV587|USER_VALIDATE/.test(ret) ? j.c : undefined, cookie, extraQ);
    ret = (j.ret || [])[0] || '';
    tries++;
  }
  if (!/SUCCESS/.test(ret)) throw damaiFail(ret);
  return j.data || {};
}

/** 大麦关键词搜索 → 与 findMaoyan 同构的行结构。 */
/** 大麦接口的名称字段常带官方营销高亮 HTML（如 <span class="c4">林忆莲</span>），
 * 直接展示就是一坨标签乱码——统一剥掉，顺带还原几个常见实体。 */
function stripTags(s) {
  return String(s || '')
    .replace(/<[^>]*>/g, '')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, ' ')
    .trim();
}

/**
 * 大麦关键词搜索（2026-10-14 重大修正：**匿名可用**）。
 *
 * 上一轮把它判死是**用错了请求形态**：旧实现走 mtopCall（jsv=2.7.2 +
 * type=originaljson + 不带 H5Request），网关直接 RGV587 硬拦。实测官方
 * 新版 H5 搜索页（m.damai.cn/shows/search.html）走的是同一 API，但带
 * `H5Request=true & type=originaljson & valueType=original & ignTbpass=true
 * & forceAntiCreep=true & AntiCreep=true`，且 payload 里多了
 * `platform/comboChannel/dmChannel` —— 匿名零 Cookie 稳定返回真实结果。
 * 与 CMS 分类通道共用一支令牌（mtopH5Get）。仍不能做：下单。
 */
async function damaiSearch(keyword, limit, cityId, userCookie) {
  const kw = String(keyword || '').trim();
  if (!kw) throw new Error('搜索词不能为空');
  const j = await mtopH5Get(
    'mtop.damai.wireless.search.search',
    '1.0',
    {
      cityId: 0, longitude: 0, latitude: 0,
      pageIndex: 1, pageSize: Math.min(Number(limit) || 20, 50),
      keyword: kw, sourceType: 11, returnItemOption: 4,
      distanceCityId: String(cityId || '852'), option: 434,
      platform: DM_CMS_PLATFORM, comboChannel: DM_CMS_COMBO, dmChannel: DM_CMS_CHANNEL,
    },
    {
      type: 'originaljson',
      valueType: 'original',
      referer: 'https://m.damai.cn/shows/search.html',
      userCookie,
    },
  );
  const data = (j && j.data) || {};
  const rows = Array.isArray(data.projectInfo) ? data.projectInfo : [];
  return rows.map((p) => ({
    performanceId: String(p.id),
    itemId: String(p.id),
    name: stripTags(p.name || p.ipName) || ('大麦 #' + p.id),
    city: stripTags(p.cityName),
    venue: stripTags(p.venueName),
    dateRange: stripTags(p.showTime),
    // 官方价格字段优先用拼好文案的 priceStr / formattedPriceStr，没有再拼区间
    price: stripTags(p.priceStr || p.formattedPriceStr) ||
      (p.priceLow != null ? '¥' + p.priceLow + ' 起' : ''),
    // itemSaleStatus 官方映射无公开文档：1 实测为在售；0/其他不猜数字，直接留空
    status: p.itemSaleStatus === 1 ? '在售' : '',
    // 海报字段实测是 verticalPic（posterUrl 在搜索接口里不存在）
    poster: p.verticalPic || p.posterUrl ||
      (Array.isArray(p.videos) && p.videos[0] && p.videos[0].picUrl) || '',
  }));
}

// ---------------------------------------------------------------------------
// 大麦「热卖榜」匿名通道（2026-10-14 重大修正：零 Cookie 可用）
// ---------------------------------------------------------------------------
// 上一轮结论「热卖榜必须登录 Cookie」**只对关键词搜索接口成立**：
// mtop.damai.wireless.search.search 匿名被网关层 RGV587 硬拦（搜过 16 个
// 候选 API 名全灭）。但大麦新版 H5（m.damai.cn/shows/*，支付宝/微信端
// show-h5-next）的**分类频道页**匿名就能列演出，走的是两条与搜索完全
// 无关的 CMS 通道：
//   ① mtop.damai.wireless.search.cms.category.get v2.0
//      → 分类配置 [groupId, patternName, patternVersion]，静态、可缓存
//   ② mtop.damai.mec.aristotle.get v3.0
//      → 该分类整页 CMS 组件树；演出卡片散在 nodes 里，靠
//        name / priceLow / verticalPic / topRight(城市) / showTime /
//        schema 里的 itemId 认出来
// 开关：H5Request=true + forceAntiCreep=true + AntiCreep=true，**不要 x-tap**
// （与详情 V12 恰好相反）；令牌仍是 _m_h5_tk 轮转，首包 TOKEN_EMPTY 后重试。
// 实测零 Cookie、零登录拉回 29 条演唱会（真实浏览器抓包验证，非猜）。
// 仍未打通：关键词搜索（那条确实要 Cookie）——但热卖榜已经不再需要。
// ---------------------------------------------------------------------------
const DM_CMS_CHANNEL = 'damai@weixin_gzh';
const DM_CMS_PLATFORM = '282';
const DM_CMS_COMBO = '4';
// 热卖榜取这几个分类（按此顺序凑，演唱会优先），最多 3 个分类
const DM_HOT_CATS = ['category_solo', 'category_music_festival', 'category_drama_new', 'category_talkshow'];

let dmCmsCookie = ''; // _m_h5_tk 令牌 jar（进程级复用）
let dmCmsCats = null; // 分类配置缓存（静态，取一次即可）

/**
 * 大麦新版 H5 的匿名 mtop GET 通道（搜索 / CMS 分类页共用）。
 * 与详情 V12 的区别：**不带 x-tap**，靠 H5Request + AntiCreep 这组开关过网关。
 * opts.type / opts.valueType：搜索接口要用 originaljson/original，CMS 用 json/string。
 * opts.userCookie：可选登录 Cookie——search.search 有更强的滑块风控
 *   （FAIL_SYS_USER_VALIDATE / RGV587_ERROR），匿名突发易触发、按 IP 冷却，
 *   带登录 Cookie 能绕开；CMS 分类接口对匿名更宽容。
 */
async function mtopH5Get(api, v, dataObj, opts) {
  const data = JSON.stringify(dataObj || {});
  const ty = (opts && opts.type) || 'json';
  const vt = (opts && opts.valueType) || 'string';
  // 用户 Cookie 里的 _m_h5_tk 必须剥掉——签名用匿名 jar 的令牌
  let userCookie = '';
  if (opts && opts.userCookie) {
    userCookie = String(opts.userCookie).replace(/^Cookie:\s*/i, '').trim()
      .split(';')
      .filter((c) => !/^\s*_m_h5_tk(_enc)?\s*=/.test(c))
      .join('; ')
      .trim();
  }
  for (let attempt = 0; attempt < 5; attempt++) {
    const t = Date.now().toString();
    const m = /(?:^|;\s*)_m_h5_tk=([^;]+)/.exec(dmCmsCookie);
    const token = m ? m[1].split('_')[0] : '';
    const sign = crypto.createHash('md5').update(`${token}&${t}&${MTOP_APPKEY}&${data}`).digest('hex');
    const url =
      `${MTOP_BASE}/${api}/${v}/?jsv=2.7.5&appKey=${MTOP_APPKEY}&t=${t}&sign=${sign}` +
      `&api=${api}&v=${v}&H5Request=true&type=${ty}&timeout=10000&dataType=json&valueType=${vt}` +
      `&ignTbpass=true&forceAntiCreep=true&AntiCreep=true&data=${encodeURIComponent(data)}`;
    const headers = {
      'User-Agent': MTOP_UA,
      Referer: (opts && opts.referer) || 'https://m.damai.cn/shows/category.html',
      Accept: 'application/json',
    };
    const ck = [];
    if (dmCmsCookie) ck.push(dmCmsCookie);
    if (userCookie) ck.push(userCookie);
    if (ck.length) headers.Cookie = ck.join('; ');
    const r = await request(url, { headers, timeout: 15000 });
    const sc = r.headers['set-cookie'];
    if (sc) dmCmsCookie = sc.map((c) => c.split(';')[0]).join('; ');
    let j;
    try {
      j = JSON.parse(r.body);
    } catch (e) {
      throw new Error('大麦 CMS 返回非 JSON：' + r.body.slice(0, 100));
    }
    const ret = (j.ret || []).join('|');
    if (/SUCCESS/.test(ret)) return j;
    // 滑块风控（RGV587 / FAIL_SYS_USER_VALIDATE）：慢速再试 1 次；再不过就抛出，
    // 由上层给「过几分钟再试 / 填 Cookie」的可行动话术，绝不在网关上死磕。
    if (/RGV587|USER_VALIDATE/.test(ret) && attempt < 1) {
      await sleep(1500);
      continue;
    }
    if (/TOKEN_EMPTY|TOKEN_EXOIRED|TOKEN_EXPIRED|ILLEGAL_SIGN/.test(ret) && attempt < 4) {
      await sleep(250);
      continue;
    }
    throw damaiFail(ret);
  }
  throw new Error('大麦 CMS 连续失败');
}

function mtopCmsCall(api, v, dataObj) {
  return mtopH5Get(api, v, dataObj);
}

async function damaiCmsCats() {
  if (dmCmsCats) return dmCmsCats;
  const j = await mtopCmsCall('mtop.damai.wireless.search.cms.category.get', '2.0', {
    apiVersion: '3.4',
    platform: DM_CMS_PLATFORM,
    comboChannel: DM_CMS_COMBO,
    dmChannel: DM_CMS_CHANNEL,
  });
  dmCmsCats = (j.data && j.data.data) || [];
  if (!dmCmsCats.length) throw new Error('大麦分类配置为空');
  return dmCmsCats;
}

/** 一个 CMS 分类 → 演出行数组（递归挖 nodes 里的卡片）。 */
async function damaiCmsList(cat, cityId) {
  const grpArgs = JSON.parse(cat.args || '{}');
  const args = Object.assign(
    {
      comboConfigRule: 'true',
      latitude: '0',
      longitude: '0',
      currentCityId: cityId,
      comboCityId: cityId,
      platform: DM_CMS_PLATFORM,
      comboChannel: DM_CMS_COMBO,
      dmChannel: DM_CMS_CHANNEL,
    },
    grpArgs,
  );
  const j = await mtopCmsCall('mtop.damai.mec.aristotle.get', '3.0', {
    args: JSON.stringify(args),
    patternName: cat.patternName,
    patternVersion: cat.patternVersion,
    platform: DM_CMS_PLATFORM,
    comboChannel: DM_CMS_COMBO,
    dmChannel: DM_CMS_CHANNEL,
  });
  const rows = [];
  const seen = {};
  const cityOf = (d) => {
    if (typeof d.topRight === 'string') return d.topRight;
    if (d.topRightV2 && d.topRightV2.value) return d.topRightV2.value;
    return d.cityName || d.city || '';
  };
  const walk = (n) => {
    if (!n || typeof n !== 'object') return;
    if (Array.isArray(n)) return n.forEach(walk);
    const d = n.data;
    if (d && typeof d === 'object') {
      const sm = d.schema && String(d.schema).match(/[?&]id=(\d{6,})/);
      const id = String(d.id || (sm && sm[1]) || '');
      if (id && d.name && (d.priceLow != null || d.price)) {
        if (!seen[id]) {
          seen[id] = 1;
          rows.push({
            performanceId: id,
            itemId: id,
            name: stripTags(d.name),
            city: stripTags(cityOf(d)),
            venue: stripTags(d.venueName),
            dateRange: stripTags(d.showTime),
            price:
              stripTags(d.price && d.price.text) ||
              (d.priceLow != null ? '¥' + d.priceLow + ' 起' : ''),
            status: stripTags(d.saleTitle) || (d.itemSaleStatus === 1 ? '在售' : ''),
            poster:
              d.verticalPic ||
              (Array.isArray(d.verticalPicList) && d.verticalPicList[0] &&
                (d.verticalPicList[0].url || d.verticalPicList[0].pic)) ||
              d.cover ||
              d.img ||
              '',
          });
        }
      }
    }
    if (n.nodes) walk(n.nodes);
  };
  walk((j.data && j.data.nodes) || []);
  return rows;
}

/**
 * 大麦/淘票票热卖榜（匿名）：取几个分类的 CMS 页，轮询交错合并成混合榜单。
 * 全部失败才抛错——调用方据此给可行动指引。
 */
async function damaiHot(limit, cityId) {
  const lim = Math.min(Number(limit) || 20, 60);
  const city = String(cityId || '852');
  const cats = await damaiCmsCats();
  const picked = [];
  for (const want of DM_HOT_CATS) {
    const c = cats.find((x) => x.patternName === want);
    if (c) picked.push(c);
    if (picked.length >= 3) break;
  }
  if (!picked.length) picked.push(cats[0]);
  const lists = [];
  for (const c of picked) {
    try {
      lists.push(await damaiCmsList(c, city));
    } catch (e) {
      lists.push([]);
    }
  }
  const out = [];
  const seen = {};
  for (let i = 0; out.length < lim && i < 80; i++) {
    for (const l of lists) {
      const row = l[i];
      if (!row || seen[row.performanceId]) continue;
      seen[row.performanceId] = 1;
      out.push(row);
      if (out.length >= lim) break;
    }
  }
  if (!out.length) throw new Error('大麦分类页没返回演出');
  return out;
}

/**
 * 大麦盯票轮询（2026-10-14 第三次重构：poll 与 detail 同走匿名 H5 v1.2 通道）：
 * 旧链路（mtop.damai.item.detail.getdetail + subpage）匿名必被 RGV587，
 * 永远只回「人太多排长队」——等于盯不了。detail 的 H5 v1.2 通道
 * （mtop.alibaba.damai.detail.getdetail）匿名稳定，一次带回场次×票档；
 * 匿名时 skuList 常为空（票档库存只在 Cookie 通道）→ 退化按场次盯，
 * 场次行保持稳定、不伪造可购状态；用户填了大麦 Cookie 后票档自动生效。
 */

async function pollDamai(params) {
  const itemId = params.item_id || params.id || extractDamaiId(params.url || '');
  if (!itemId) throw new Error('缺少大麦 itemId——把大麦 App 分享出来的链接粘贴进来即可');
  const userCookie = params.damaiCookie || '';

  const det = await damaiDetailV12(itemId, userCookie);
  const items = [];
  let subpageDead = false; // subpage 被风控拦一次就不逐场次再试，省超时
  for (const s of (det.sessions || []).slice(0, 10)) {
    const name = stripTags(s.name) || '场次';
    if (s.tiers && s.tiers.length) {
      for (const t of s.tiers) {
        items.push({
          label: name + ' · ' + t.name,
          place: '',
          when: s.when || name,
          status: t.available ? '可购' : '不可购',
          available: !!t.available,
          count: t.available ? null : 0,
          price: t.price || '',
        });
      }
      continue;
    }
    // 匿名通道没带票档 → 有 Cookie 才试 subpage（Cookie 通道），否则按场次盯
    let tiers = [];
    if (!subpageDead && s.performId) {
      try {
        tiers = await damaiSessionTiers(itemId, s.performId, userCookie);
      } catch (e) {
        tiers = [];
        subpageDead = true; // 一次被拦视作整段被拦
      }
    }
    if (tiers.length) {
      for (const t of tiers) {
        items.push({
          label: name + ' · ' + t.name,
          place: '',
          when: s.when || name,
          status: t.available ? '可购' : '不可购',
          available: !!t.available,
          count: t.available ? null : 0,
          price: t.price || '',
        });
      }
    } else {
      items.push({
        label: name,
        place: '',
        when: s.when || name,
        status: userCookie ? '没挂出票档' : '票档要凭证（「我的」页填大麦 Cookie 后按价位盯）',
        available: false,
        count: 0,
        price: det.priceRange || '',
      });
    }
  }
  if (!items.length) {
    // 连场次都没有：大概率被风控拦或项目已下架——给一句能行动的话
    const ssr = await damaiDetailSSR(itemId).catch(() => null);
    throw new Error(
      ssr
        ? '大麦场次列表暂时被风控拦住——「我的」页填一次大麦 Cookie 就能盯'
        : '大麦没返回场次（项目可能已下架或 id 不对）'
    );
  }
  return items;
}

/**
 * 大麦入口：链接/纯数字 id → mtop 详情+票档；关键词 → mtop 无线搜索。
 * 和猫眼/摩天轮一样，用户直接打字就能搜——不再逼人去 App 翻链接。
 * 新增：空关键词时走热卖榜（CMS 分类通道，匿名可用），失败时退回搜索。
 */
async function findDamai(linkOrId, userCookie, cityId) {
  const raw = String(linkOrId || '').trim();
  if (/https?:\/\//i.test(raw) || /^\d{6,}$/.test(raw)) {
    const id = extractDamaiId(raw) || raw;
    // 演出名优先走详情通道——poll 行标签是「场次 · 票档」，
    // 拿它当演出名会把列表页名字写成场次（同摩天轮那次踩坑）
    const det = await damaiDetailV12(id, userCookie).catch(() => null);
    if (det && det.name) {
      return [
        {
          performanceId: id,
          itemId: id,
          name: det.name,
          city: det.city || '',
          venue: det.venue || '',
          dateRange: det.dateRange || '',
          price: det.priceRange || '',
          status: '',
          poster: det.poster || '',
        },
      ];
    }
    const items = await pollDamai({ item_id: id, damaiCookie: userCookie });
    const first = items[0] || {};
    return [
      {
        performanceId: id,
        itemId: id,
        name: first.label || '大麦 #' + id,
        city: '',
        venue: first.place || '',
        dateRange: first.when || '',
        price: first.price || '',
        status: first.status || '',
        poster: '',
      },
    ];
  }
  return damaiSearch(raw, 20, cityId, userCookie);
}

// ===========================================================================
// 平台：B站会员购（show.bilibili.com，接口公开匿名可读——2026-10 实测打通）
// ===========================================================================
// 两条路径，全部匿名（不需要 SESSDATA 登录态——那是抢票脚本才需要的）：
//   搜索: /api/ticket/project/listV2?version=134&platform=web&page=1&pagesize=20
//         &ptype=100104&area=0&keyword=<词>     （ptype=100104 = 演出类目）
//   详情: /api/ticket/project/getV2?version=134&id=<pid>&project_id=<pid>&requestSource=pcweb
//   老接口 project/get 已废弃（永远回"请慢一点"），别再用。
// 关键结构（实测）：
//   · 价格单位是**分**（4800 = ¥48），展示时除以 100；
//   · 票档状态用官方 sale_flag.display_name 原文（预售中/即将售罄/售罄…），
//     不自己编映射表——官方改词我们自动跟着对；
//   · available = clickable && is_sale===1 && 文案不含 售罄/缺货/结束；
//   · 匿名请求最好带 buvid3（从 www.bilibili.com 首页拿，进程内缓存 1h），
//     不带偶尔会被限频"请慢一点"。

const BILI_SHOW = 'https://show.bilibili.com';
// 进程级缓存匿名 cookie；云函数实例常驻时复用
let biliCookie = '';
let biliCookieTs = 0;

async function biliWarm() {
  if (biliCookie && Date.now() - biliCookieTs < 3600000) return biliCookie;
  const r = await request('https://www.bilibili.com/', {
    headers: { 'User-Agent': UA },
    timeout: 15000,
  });
  const sc = r.headers['set-cookie'];
  if (sc) {
    biliCookie = sc.map((c) => c.split(';')[0]).join('; ');
    biliCookieTs = Date.now();
  }
  return biliCookie;
}

function biliHeaders() {
  const h = {
    'User-Agent': UA,
    Referer: 'https://show.bilibili.com/',
    Accept: 'application/json, text/plain, */*',
  };
  if (biliCookie) h.Cookie = biliCookie;
  return h;
}

/** B站会员购统一错误判断：新旧两套响应壳（errno/code/success）都罩住。 */
function biliCheck(j) {
  if (!j || typeof j !== 'object') throw new Error('会员购返回了空响应');
  const bad =
    j.success === false ||
    (j.code != null && j.code !== 0) ||
    (j.errno != null && j.errno !== 0);
  if (bad) {
    throw new Error('会员购返回：' + (j.message || j.msg || ('code=' + (j.code != null ? j.code : j.errno))));
  }
}

/** 会员购关键词搜索 → 与 findMaoyan 同构的行结构。空关键词 = 正在热卖列表。 */
async function findBilibili(keyword, limit) {
  const kw = String(keyword || '').trim();
  await biliWarm().catch(() => {}); // buvid3 拿不到也照搜，匿名多半放行
  const url =
    `${BILI_SHOW}/api/ticket/project/listV2?version=134&platform=web&page=1` +
    `&pagesize=${Math.min(Number(limit) || 20, 20)}&ptype=100104&area=0` +
    (kw ? `&keyword=${encodeURIComponent(kw)}` : '');
  const j = await getJson(url, { headers: biliHeaders() });
  biliCheck(j);
  const rows = (j.data && j.data.result) || [];
  return rows.map((r) => {
    const cover = String(r.cover || '');
    const flag = (r.sale_flag && r.sale_flag.display_name) || '';
    const priceFen = r.price_low != null ? r.price_low : r.price;
    return {
      performanceId: String(r.project_id || r.id),
      projectId: String(r.project_id || r.id),
      name: r.project_name || r.name || r.words || ('会员购 #' + (r.project_id || r.id)),
      city: r.city || '',
      venue: '',
      dateRange: r.performance_date || '',
      price: priceFen != null ? '¥' + priceFen / 100 + ' 起' : '',
      status: flag,
      poster: cover ? (cover.startsWith('//') ? 'https:' + cover : cover) : '',
    };
  });
}

/**
 * 抓一个会员购项目的当前状态：场次 × 票档 全展开。
 * 和摩天轮 hasTicket 同构——每行一个可判定的 available，diffRestock 直接可用。
 */
async function pollBilibili(params) {
  let pid = params.project_id || params.id || '';
  if (!pid) {
    const m = String(params.url || '').match(/id=(\d+)/);
    if (m) pid = m[1];
  }
  if (!pid) throw new Error('缺少会员购 project_id——在 show.bilibili.com 演出页地址栏里找，或直接粘贴详情链接');

  await biliWarm().catch(() => {});
  const url =
    `${BILI_SHOW}/api/ticket/project/getV2?version=134&id=${encodeURIComponent(pid)}` +
    `&project_id=${encodeURIComponent(pid)}&requestSource=pcweb`;
  const j = await getJson(url, { headers: biliHeaders() });
  biliCheck(j);
  const d = j.data || {};
  const venue =
    (d.venue_info && (d.venue_info.name || d.venue_info.venue_name)) ||
    (d.place_info && (d.place_info.name || d.place_info.place_name)) ||
    '';

  const screens = Array.isArray(d.screen_list) ? d.screen_list : [];
  const items = [];
  for (const s of screens.slice(0, 20)) {
    for (const t of s.ticket_list || []) {
      const disp = (t.sale_flag && t.sale_flag.display_name) || '';
      const soldOut = /售罄|缺货|结束|下架|停止|暂停/.test(disp);
      const available = t.clickable === true && t.is_sale === 1 && !soldOut;
      items.push({
        label: (s.name || s.start_time_str || '场次') + ' ' + (t.desc || ''),
        place: venue,
        when: s.start_time_str || s.name || '',
        status: disp || (available ? '可购' : '不可购'),
        available: available,
        count: available ? null : 0,
        price: t.price != null ? '¥' + t.price / 100 : '',
      });
    }
  }
  if (!items.length) {
    // 没挂出场次（或已全下架）：项目级信息当一条，官方状态原文兜底
    const disp = (d.sale_flag && d.sale_flag.display_name) || '';
    items.push({
      label: d.name || ('会员购 #' + pid),
      place: venue,
      when: '',
      status: disp || '详情见会员购',
      available: d.is_sale === 1 && !/售罄|缺货|结束/.test(disp),
      count: d.is_sale === 1 ? null : 0,
      price: d.price_low != null ? '¥' + d.price_low / 100 + ' 起' : '',
    });
  }
  return items;
}

// ===========================================================================
// 平台：票星球（m.piaoxingqiu.com / cyy_gatewayapi，公开匿名可读——2026-10 实测打通）
// ===========================================================================
// 端点全部来自 H5 前端 bundle（uni-app，global-static.caiyicloud.com），
// 都是 pub（公开）路径，不需要登录态：
//   搜索: GET /cyy_gatewayapi/home/pub/v3/show_list/search
//         ?keyword=&pageType=SEARCH_PAGE&offset=&length=        （不带城市=全国）
//   推荐: GET /cyy_gatewayapi/home/pub/v3/show/recommend         （热卖榜，40 条）
//   静态: GET /cyy_gatewayapi/show/pub/v5/show/{showId}/static   （海报/名称/场馆）
//   动态: GET /cyy_gatewayapi/show/pub/v5/show/{showId}/dynamic  （showDetailStatus）
//   场次: GET /cyy_gatewayapi/show/pub/v3/show/{showId}/sessions_static_data（场次名/时间）
//         GET /cyy_gatewayapi/show/pub/v3/show/{showId}/sessions_dynamic_data（场次态）
//   票档: GET /cyy_gatewayapi/show/pub/v3/show/{showId}/show_session/{sid}/seat_plans_static_data（票名/原价）
//         GET /cyy_gatewayapi/show/pub/v3/show/{showId}/show_session/{sid}/seat_plans_dynamic_data（canBuyCount）
// 请求头固定 Src: H5 / Terminal-Src: H5 + iPhone UA；同盾 blackbox 只用于
// 抢票下单（本项目不做），监控链路全程匿名。
// 状态枚举（官方原文）：ONSALE 在售 / PRESALE 预售 / PURCHASE_TO_BEGIN 未开售 /
//   SOLD_OUT 售罄 / LACK_OF_TICKET 缺票登记（回流票的信号源）/ PENDING 待定。

const PXQ_M = 'https://m.piaoxingqiu.com';
const PXQ_VER = '4.3.3-20240404095840';

function pxqHeaders() {
  return {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1',
    Src: 'H5',
    'Terminal-Src': 'H5',
    Accept: 'application/json, text/plain, */*',
  };
}

function pxqUrl(path, query) {
  const q = Object.assign({ src: 'H5', ver: PXQ_VER }, query || {});
  const qs = Object.keys(q)
    .filter((k) => q[k] !== '' && q[k] != null)
    .map((k) => encodeURIComponent(k) + '=' + encodeURIComponent(q[k]))
    .join('&');
  return `${PXQ_M}${path}${qs ? '?' + qs : ''}`;
}

function pxqCheck(j, what) {
  if (!j || typeof j !== 'object') throw new Error('票星球返回了空响应');
  if (j.statusCode != null && j.statusCode !== 200) {
    throw new Error('票星球' + what + '返回：' + (j.comments || ('statusCode=' + j.statusCode)));
  }
  const d = j.data;
  if (!d || typeof d !== 'object') throw new Error('票星球' + what + '无数据');
  if (d.rsCode != null && d.rsCode !== 200) {
    throw new Error('票星球' + what + '业务错误 rsCode=' + d.rsCode);
  }
  return d;
}

/** 票星球分享链接形如 /show/{bizShowId}（24 位十六进制）。 */
function extractPxqId(text) {
  const m = String(text || '').match(/piaoxingqiu\.com\/show\/([a-zA-Z0-9-]+)/i);
  return (m && m[1]) || '';
}

/** 官方票态 → 是否可买。缺票登记/售罄/未开售都算不可买。 */
function pxqAvailable(status) {
  const s = String(status || '').toUpperCase();
  return s === 'ONSALE' || s === 'ON_SALE' || s === 'PRESALE' || s === 'PRE_SALE';
}

const PXQ_STATUS_CN = {
  ONSALE: '在售', ON_SALE: '在售',
  PRESALE: '预售中', PRE_SALE: '预售中',
  PURCHASE_TO_BEGIN: '未开售',
  SOLD_OUT: '售罄',
  LACK_OF_TICKET: '缺票登记',
  PENDING: '待定',
};

function pxqStatusCn(status) {
  const s = String(status || '').toUpperCase();
  return PXQ_STATUS_CN[s] || (status ? String(status) : '状态未知');
}

/** 关键词搜索（全国范围）→ 与 findMaoyan 同构的行结构。
 * 票星球搜索 API 不工作（固定返回推荐结果），改用多端点聚合 + 本地过滤。 */
async function findPiaoxingqiu(keyword, limit) {
  const kw = String(keyword || '').trim();

  // 并行获取多个数据源
  const [recommendRes, hotRes] = await Promise.allSettled([
    getJson(pxqUrl('/cyy_gatewayapi/home/pub/v3/show/recommend'), {
      headers: pxqHeaders(), timeout: 15000,
    }),
    // 试不同分类的推荐
    kw ? getJson(pxqUrl('/cyy_gatewayapi/home/pub/v3/show_list/search', {
      kw: kw, pageType: 'SEARCH_PAGE', offset: 0, length: 50, src: 'H5', ver: '4.3.3-20240404095840'
    }), { headers: pxqHeaders(), timeout: 15000 }).catch(() => null) : null,
  ]);

  // 合并所有结果
  const allShows = new Map(); // 用 showId 去重

  // 从 recommend 获取
  if (recommendRes.status === 'fulfilled') {
    const shows = pxqCheck(recommendRes.value, '推荐') || [];
    for (const r of (Array.isArray(shows) ? shows : [])) {
      const id = String(r.showId || '');
      if (id && !allShows.has(id)) allShows.set(id, r);
    }
  }

  // 从 search 获取（如果有效）
  if (kw && hotRes && hotRes.status === 'fulfilled' && hotRes.value) {
    const searchData = hotRes.value.data?.searchData || [];
    for (const r of searchData) {
      const id = String(r.showId || '');
      if (id && !allShows.has(id)) allShows.set(id, r);
    }
  }

  let shows = Array.from(allShows.values());

  // 如果有关键词，本地过滤
  if (kw && shows.length > 0) {
    const k = kw.toLowerCase();
    shows = shows.filter((r) => {
      const name = (r.showName || '').toLowerCase();
      const city = (r.cityName || '').toLowerCase();
      return name.includes(k) || city.includes(k);
    });
  }

  // 如果还是没结果，返回全部推荐
  if (!shows.length) {
    shows = Array.from(allShows.values());
  }

  return shows.slice(0, limit || 20).map((r) => ({
    performanceId: String(r.showId),
    showId: String(r.showId),
    stdShowId: r.stdShowId ? String(r.stdShowId) : '',
    name: r.showName || ('票星球 #' + r.showId),
    city: r.cityName || '',
    venue: r.venueName || '',
    dateRange: r.showDate || '',
    price: r.minOriginalPrice != null ? '¥' + r.minOriginalPrice + ' 起' : '',
    status: pxqStatusCn(r.showStatus),
    available: pxqAvailable(r.showStatus),
    poster: r.posterUrl || '',
    category: (r.backendCategory && r.backendCategory.displayName) || '',
  }));
}

/** 票星球热卖榜（推荐位 40 条，匿名直读）。 */
async function hotPiaoxingqiu(limit) {
  const j = await getJson(pxqUrl('/cyy_gatewayapi/home/pub/v3/show/recommend'), {
    headers: pxqHeaders(),
    timeout: 15000,
  });
  const rows = pxqCheck(j, '推荐') || [];
  return (Array.isArray(rows) ? rows : [])
    .slice(0, Math.min(Number(limit) || 20, 40))
    .map((r) => ({
      performanceId: String(r.showId),
      showId: String(r.showId),
      name: r.showName || ('票星球 #' + r.showId),
      city: r.cityName || '',
      venue: r.venueName || '',
      dateRange: r.showDate || '',
      price: r.minOriginalPrice != null ? '¥' + r.minOriginalPrice + ' 起' : '',
      status: pxqStatusCn(r.showStatus),
      poster: r.posterUrl || '',
    }));
}

/**
 * 抓一个票星球演出的当前状态：场次 × 票档 全展开。
 * 每行一个可判定的 available，diffRestock 直接可用。
 * params: { show_id } 或 { url: 分享链接 }
 */
async function pollPiaoxingqiu(params) {
  let showId = params.show_id || params.id || extractPxqId(params.url || '') || '';
  if (!showId) {
    // 也接受直接粘 24 位 hex id
    const m = String(params.url || '').match(/\b([a-f0-9]{24})\b/i);
    if (m) showId = m[1];
  }
  if (!showId) throw new Error('缺少票星球 show_id——把票星球分享的演出链接粘贴进来，或去「搜演出」直接搜');

  // 场次静态（名称/时间）+ 动态（状态）；动态失败不致命，下面按静态兜底
  const sStatic = pxqCheck(
    await getJson(pxqUrl(`/cyy_gatewayapi/show/pub/v3/show/${showId}/sessions_static_data`), {
      headers: pxqHeaders(), timeout: 15000,
    }), '场次',
  );
  let sDynamic = null;
  try {
    sDynamic = pxqCheck(
      await getJson(pxqUrl(`/cyy_gatewayapi/show/pub/v3/show/${showId}/sessions_dynamic_data`), {
        headers: pxqHeaders(), timeout: 15000,
      }), '场次状态',
    );
  } catch (e) { /* 场次动态拿不到就用静态兜底 */ }
  const dynMap = {};
  for (const s of (sDynamic && sDynamic.sessionVOs) || []) {
    dynMap[String(s.bizShowSessionId)] = s;
  }

  const sessions = Array.isArray(sStatic.sessionVOs) ? sStatic.sessionVOs : [];
  const showName = sStatic.showName || ('票星球 #' + showId);
  const items = [];
  for (const s of sessions.slice(0, 10)) {
    const sid = String(s.bizShowSessionId || '');
    const dyn = dynMap[sid] || {};
    const sStatus = String(dyn.sessionStatus || s.sessionStatus || '').toUpperCase();
    // 票档静态（票名/原价）+ 动态（canBuyCount）
    let plans = [];
    try {
      const pStatic = pxqCheck(
        await getJson(
          pxqUrl(`/cyy_gatewayapi/show/pub/v3/show/${showId}/show_session/${sid}/seat_plans_static_data`),
          { headers: pxqHeaders(), timeout: 15000 },
        ), '票档',
      );
      plans = Array.isArray(pStatic.seatPlans) ? pStatic.seatPlans : [];
      let pDynamic = null;
      try {
        pDynamic = pxqCheck(
          await getJson(
            pxqUrl(`/cyy_gatewayapi/show/pub/v3/show/${showId}/show_session/${sid}/seat_plans_dynamic_data`),
            { headers: pxqHeaders(), timeout: 15000 },
          ), '票档状态',
        );
      } catch (e) { /* 动态拿不到就用静态的 isStopSale 判 */ }
      const canBuy = {};
      for (const p of (pDynamic && pDynamic.seatPlans) || []) {
        canBuy[String(p.seatPlanId)] = p;
      }
      for (const p of plans) {
        const pid = String(p.seatPlanId || '');
        const dynP = canBuy[pid] || {};
        const cnt = dynP.canBuyCount != null ? Number(dynP.canBuyCount) : null;
        const available =
          p.saleStarted !== false &&
          p.isStopSale !== true &&
          p.channelHideFlag !== true &&
          (cnt == null ? pxqAvailable(sStatus) : cnt > 0);
        items.push({
          label: (s.sessionName || '场次') + ' ' + (p.seatPlanName || '票档'),
          place: sStatic.venueName || '',
          when: s.sessionName || '',
          status: available
            ? (cnt != null && cnt > 0 ? '可购 ×' + cnt : pxqStatusCn(sStatus))
            : (p.isStopSale === true ? '停售' : pxqStatusCn(sStatus)),
          available: available,
          count: available ? (cnt != null && cnt > 0 ? cnt : null) : 0,
          price: p.originalPrice != null ? '¥' + p.originalPrice : '',
        });
      }
    } catch (e) {
      // 票档拿不到：场次级一条兜底（状态原文）
      items.push({
        label: s.sessionName || showName,
        place: sStatic.venueName || '',
        when: s.sessionName || '',
        status: pxqStatusCn(sStatus) || '场次',
        available: pxqAvailable(sStatus) && dyn.hasSessionSoldOut !== true,
        count: pxqAvailable(sStatus) ? null : 0,
        price: '',
      });
    }
  }
  if (!items.length) {
    // 没挂出场次：项目级一条，用动态详情状态兜底
    let dStatus = '';
    try {
      const dyn = pxqCheck(
        await getJson(pxqUrl(`/cyy_gatewayapi/show/pub/v5/show/${showId}/dynamic`), {
          headers: pxqHeaders(), timeout: 15000,
        }), '详情',
      );
      dStatus = String(dyn.showDetailStatus || '').toUpperCase();
    } catch (e) { /* 忽略，走未知 */ }
    items.push({
      label: showName,
      place: '',
      when: '',
      status: pxqStatusCn(dStatus) || '状态未知',
      available: pxqAvailable(dStatus),
      count: pxqAvailable(dStatus) ? null : 0,
      price: '',
    });
  }
  return items;
}

// ===========================================================================
// 演出详情（详情页用）：统一返回 { platform, id, name, venue, sessions[] }
//   sessions[].tiers[] 每行带官方状态原文 + available（可判定）。
//   B站/票星球出真场次结构；其余平台 poll 是扁平的，包成单一分组。
// ===========================================================================
async function detailShow(platform, id, event) {
  // 容错：id 位置可能被塞了一整条分享链接。
  // 场景：首页监控卡片 / 「我的-正在盯」跳详情时，本地任务里存的可能是
  // `params.url`（用户在加监控弹层里粘的链接），而不是裸 id。
  // 与其让用户点进来说「缺 id」，不如在这里按各平台规则统一抠一次。
  if (/https?:\/\//i.test(id)) {
    let bare = '';
    if (platform === 'damai' || platform === 'taopiaopiao') bare = extractDamaiId(id);
    else if (platform === 'fenwandao') bare = extractFwdId(id);
    else if (platform === 'piaoxingqiu') bare = extractPxqId(id);
    else if (platform === 'bilibili') bare = (id.match(/[?&]id=(\d+)/) || [])[1] || '';
    else if (platform === 'maoyan') bare = (id.match(/[?&]performanceId=(\d+)/i) || [])[1] || '';
    else if (platform === 'moretickets') {
      bare = (id.match(/tour(?:Id)?[=\/]([a-f0-9]{6,})/i) || [])[1] || '';
    }
    if (bare) id = bare;
  }

  // 12306：详情 = 单趟车次全席别余票。站名/日期从查询上下文带过来
  // （shows 页跳转时随 URL 传，客户端再随请求传上来）。
  if (platform === 'rail12306') {
    const from = String((event && event.from) || '').trim();
    const to = String((event && event.to) || '').trim();
    const date = String((event && event.date) || '').trim();
    if (!from || !to || !date) {
      throw new Error('缺少 from / to / date');
    }
    return railDetail(from, to, date, id);
  }

  if (platform === 'fenwandao') {
    // 容错：detail 也可能被直接塞一整条分享链接
    const pid = /https?:\/\//i.test(id) ? extractFwdId(id) : id;
    const token = String((event && event.fwdToken) || '').trim();
    // 匿名项目级详情（免登录）先拿——名称/海报/城市/场馆/日期/票价区间 + 部分场次
    let proj = null;
    try {
      proj = await fwdGetProjectInfo(pid);
    } catch (e) {
      proj = null;
    }

    if (token) {
      // 有 token：逐场次逐档最准，再合并到匿名项目级信息上
      try {
        const infos = await fwdGetPerforms(pid, token);
        const tiers = {};
        for (const info of infos.slice(0, 20)) {
          const s = (info.performInfo || [])[0] || {};
          const plans = Array.isArray(s.seatPlans) ? s.seatPlans : [];
          const list = plans.map((p) => ({
            name: p.seatPlanName || '票档',
            price: p.seatPlanPrice != null ? '¥' + p.seatPlanPrice : '',
            status: p.display === 1 ? '可购' : '不可购',
            available: p.display === 1,
          }));
          if (s.id != null) tiers[String(s.id)] = list;
          if (s.name) tiers[String(s.name)] = list;
        }
        if (proj) {
          const d = fwdDetailFromProject(pid, proj, tiers);
          if (d.sessions.length) return d; // 匿名已带回场次，逐档已合并
        }
        // 匿名没带回场次（无退票规则的项目）→ 直接用 get_performs 的场次列表
        const sessions = infos.slice(0, 20).map((info) => {
          const s = (info.performInfo || [])[0] || {};
          const plans = Array.isArray(s.seatPlans) ? s.seatPlans : [];
          return {
            name: s.name || '场次',
            when: s.startTimeStr || s.name || '',
            status: '',
            available: plans.some((p) => p.display === 1),
            tiers: plans.map((p) => ({
              name: p.seatPlanName || '票档',
              price: p.seatPlanPrice != null ? '¥' + p.seatPlanPrice : '',
              status: p.display === 1 ? '可购' : '不可购',
              available: p.display === 1,
            })),
          };
        });
        if (proj) return Object.assign(fwdDetailFromProject(pid, proj, tiers), { sessions, noTiers: !sessions.some((s) => s.tiers.length) });
        return {
          platform,
          id: pid,
          name: (infos[0] && infos[0].projectName) || ('纷玩岛 #' + pid),
          venue: '',
          sessions,
        };
      } catch (e) {
        // token 失效/风控 → 落回匿名（不整页报错）
      }
    }

    if (proj) return fwdDetailFromProject(pid, proj, null);
    throw fwdTokenError();
  }

  // 猫眼详情：有登录 token（myToken）时走 odea 拿真实场次×票档×库存；
  // 没有 token 匿名通道只有项目级库存，落回 generic 分支（诚实：只有一个价位，
  // 详情页会提示填凭证后才能按价位盯）
  if (platform === 'maoyan' && event && event.myToken) {
    try {
      const levels = await myOdeaTicketLevels(id, event.myToken);
      const sessions = levels.map((lv) => ({
        name: lv.showName,
        when: lv.showName,
        status: '',
        available: lv.tiers.some((t) => t.stock > 0),
        tiers: lv.tiers.map((t) => ({
          name: t.name,
          price: t.price,
          status: t.stock > 0 ? (t.stock + ' 张') : '无票',
          available: t.stock > 0,
        })),
      }));
      const d = (await getJson(`${MAOYAN_BASE}/ajax/performance/${id}?sellChannel=7`, { headers: maoyanHeaders() })).data || {};
      return { platform, id, name: stripTags(d.name) || '', venue: stripTags(d.shopName || d.address) || '', sessions };
    } catch (e) {
      // token 失效别整页报错——落回匿名项目级详情
      if (!/凭证/.test(String(e.message))) throw e;
    }
  }

  // 猫眼（匿名 / token 失效）：项目级信息 + **真实场次**，但**不造票档**。
  // 匿名通道只到「项目级价格区间」和「场次级有没有货」，档位×库存要登录 odea。
  // 所以这里明确 noTiers，让详情页照实说「票档要登录后可见，先按整场盯」，
  // 而不是塞一个名字等于演出全名的假票档糊弄（那正是用户截图里骂的点）。
  if (platform === 'maoyan') {
    const p = (await getJson(`${MAOYAN_BASE}/ajax/performance/${id}?sellChannel=7`, { headers: maoyanHeaders() })).data || {};
    if (Number(p.ticketStatus) == null || !MAOYAN_TICKET_STATUS_LABELS[Number(p.ticketStatus)]) {
      // 状态表对不上 = 平台改了映射，别静默当成「没票」（与 pollMaoyan 同一条红线）
      throw new Error('猫眼详情的 ticketStatus=' + JSON.stringify(p.ticketStatus) + ' 看不懂，平台可能改了状态映射表');
    }
    let shows = [];
    try {
      shows = await maoyanShows(id);
    } catch (e) {
      shows = []; // 场次拿不到不影响项目级信息照常返回
    }
    // 票价档位（匿名 /tickets 可读，本轮新打通）——摆出来，但**不可勾选**：
    // 匿名 hasInventory 恒 false，逐档库存判断不了，做成选择器就是骗人。
    let tiers = [];
    try {
      tiers = await maoyanTickets(id);
    } catch (e) {
      tiers = [];
    }
    // 场次不做成「可勾选列表」：轮询走的是项目级通道，按场次过滤落不了地，
    // 摆一个勾了也没用的选择器就是在骗人。改成信息行——把日期如实摆出来，
    // 特展这种 199 场的只列前几个 + 总数，不铺满屏。
    const times = shows.map((s) => s.name).filter(Boolean);
    const head = times.slice(0, 3).join(' / ');
    const showLine = times.length
      ? '场次 · ' + head + (times.length > 3 ? ' 等 ' + times.length + ' 场' : '')
      : '';
    const tierLine = tiers.length ? '票档 · ' + tiers.map((t) => t.name).join(' / ') : '';
    return {
      platform,
      id,
      name: stripTags(p.name) || '',
      venue: stripTags(p.shopName || p.address) || '',
      poster: p.posterUrl || '',
      dateRange: p.showTimeRange || '',
      priceRange: p.priceRange || '',
      saleNote: [showLine, tierLine].filter(Boolean).join('　'),
      // 只作展示用（前端渲染成不可点的价格标签）；勾选/过滤仍走 odea 通道
      priceTiers: tiers.map((t) => t.name),
      sessions: [],
      noTiers: true,
    };
  }

  if (platform === 'bilibili') {
    await biliWarm().catch(() => {});
    const j = await getJson(
      `${BILI_SHOW}/api/ticket/project/getV2?version=134&id=${encodeURIComponent(id)}` +
        `&project_id=${encodeURIComponent(id)}&requestSource=pcweb`,
      { headers: biliHeaders() },
    );
    biliCheck(j);
    const d = j.data || {};
    const venue =
      (d.venue_info && (d.venue_info.name || d.venue_info.venue_name)) ||
      (d.place_info && (d.place_info.name || d.place_info.place_name)) || '';
    const sessions = (Array.isArray(d.screen_list) ? d.screen_list : []).map((s) => ({
      name: s.name || s.start_time_str || '场次',
      when: s.start_time_str || s.name || '',
      status: '',
      available: false,
      tiers: (s.ticket_list || []).map((t) => {
        const disp = (t.sale_flag && t.sale_flag.display_name) || '';
        const soldOut = /售罄|缺货|结束|下架|停止|暂停/.test(disp);
        const available = t.clickable === true && t.is_sale === 1 && !soldOut;
        return {
          name: t.desc || t.screen_name || '票档',
          price: t.price != null ? '¥' + t.price / 100 : '',
          status: disp || (available ? '可购' : '不可购'),
          available: available,
        };
      }),
    }));
    return { platform, id, name: d.name || '', venue, sessions };
  }

  if (platform === 'piaoxingqiu') {
    const sStatic = pxqCheck(
      await getJson(pxqUrl(`/cyy_gatewayapi/show/pub/v3/show/${id}/sessions_static_data`), {
        headers: pxqHeaders(), timeout: 15000,
      }), '场次',
    );
    let sDynamic = null;
    try {
      sDynamic = pxqCheck(
        await getJson(pxqUrl(`/cyy_gatewayapi/show/pub/v3/show/${id}/sessions_dynamic_data`), {
          headers: pxqHeaders(), timeout: 15000,
        }), '场次状态',
      );
    } catch (e) { /* 动态拿不到用静态兜底 */ }
    const dynMap = {};
    for (const s of (sDynamic && sDynamic.sessionVOs) || []) dynMap[String(s.bizShowSessionId)] = s;
    const sessions = [];
    for (const s of (Array.isArray(sStatic.sessionVOs) ? sStatic.sessionVOs : []).slice(0, 10)) {
      const sid = String(s.bizShowSessionId || '');
      const dyn = dynMap[sid] || {};
      const sStatus = String(dyn.sessionStatus || s.sessionStatus || '').toUpperCase();
      const tiers = [];
      try {
        const pStatic = pxqCheck(
          await getJson(
            pxqUrl(`/cyy_gatewayapi/show/pub/v3/show/${id}/show_session/${sid}/seat_plans_static_data`),
            { headers: pxqHeaders(), timeout: 15000 },
          ), '票档',
        );
        let pDynamic = null;
        try {
          pDynamic = pxqCheck(
            await getJson(
              pxqUrl(`/cyy_gatewayapi/show/pub/v3/show/${id}/show_session/${sid}/seat_plans_dynamic_data`),
              { headers: pxqHeaders(), timeout: 15000 },
            ), '票档状态',
          );
        } catch (e) { /* 动态拿不到用静态判 */ }
        const canBuy = {};
        for (const p of (pDynamic && pDynamic.seatPlans) || []) canBuy[String(p.seatPlanId)] = p;
        for (const p of pStatic.seatPlans || []) {
          const cnt = canBuy[String(p.seatPlanId)];
          const n = cnt && cnt.canBuyCount != null ? Number(cnt.canBuyCount) : null;
          const available =
            p.saleStarted !== false && p.isStopSale !== true && p.channelHideFlag !== true &&
            (n == null ? pxqAvailable(sStatus) : n > 0);
          tiers.push({
            name: p.seatPlanName || '票档',
            price: p.originalPrice != null ? '¥' + p.originalPrice : '',
            status: available
              ? (n != null && n > 0 ? '可购 ×' + n : pxqStatusCn(sStatus))
              : (p.isStopSale === true ? '停售' : pxqStatusCn(sStatus)),
            available: available,
          });
        }
      } catch (e) { /* 票档拿不到就空着 */ }
      sessions.push({
        name: s.sessionName || '场次',
        when: s.sessionName || '',
        status: pxqStatusCn(sStatus),
        available: pxqAvailable(sStatus) && dyn.hasSessionSoldOut !== true,
        tiers: tiers,
      });
    }
    return { platform, id, name: sStatic.showName || '', venue: sStatic.venueName || '', sessions };
  }

  // 大麦 / 淘票票详情（2026-10-14 重做）：走官方 H5 匿名通道
  // mtop.alibaba.damai.detail.getdetail（不再依赖 Cookie），拿演出名/海报/
  // 时间/城市/价格区间 + 场次列表；票档级 subpage 仍被风控拦时保留场次、
  // 标记 noTiers（详情页照常渲染场次，只是按价位盯需要 Cookie）。
  // 淘票票的演出就是 m.taopiaopiao.com/damai/detail/item.html?itemId=X——
  // 与大麦同一 itemId 命名空间，共用同一条通道。
  if (platform === 'damai' || platform === 'taopiaopiao') {
    const cookie = (event && event.damaiCookie) || '';
    try {
      const d = await damaiDetailV12(id, cookie);
      // 票档级 subpage 接口被风控更狠（匿名必 RGV587）——只试一枪，
      // 失败即止，避免详情页白白多等好几个超时。
      if (d.sessions.length && !d.sessions.some((s) => s.tiers.length)) {
        let failed = false;
        for (const s of d.sessions) {
          if (failed) break;
          try {
            const tiers = await damaiSessionTiers(id, s.performId || s.name, cookie);
            if (tiers.length) s.tiers = tiers;
          } catch (e) {
            failed = true;
          }
        }
      }
      if (!d.sessions.length) {
        const ssr = await damaiDetailSSR(id).catch(() => null);
        if (ssr) {
          d.venue = d.venue || ssr.venue;
          d.dateRange = d.dateRange || ssr.dateRange;
        }
        return Object.assign(
          { platform, id, degraded: true, degradeMsg: '场次和票档暂时拿不到，演出信息先看看' },
          d,
        );
      }
      if (!d.sessions.some((s) => s.tiers.length)) d.noTiers = true;
      return Object.assign({ platform, id }, d);
    } catch (e) {
      const ssr = await damaiDetailSSR(id).catch(() => null);
      if (!ssr) throw e;
      return Object.assign({ platform, id, degraded: true, sessions: [] }, ssr);
    }
  }

  // 其余平台：扁平 poll 的**一行就是一个「可盯的东西」**。
  // 摩天轮这类平台一行 = 一个场次（label 形如「2027-06-08 19:00 场馆」），
  // 它压根不是票档。旧代码把 N 行压成「一场 N 档」，详情页就出现
  // 「票档里写着别的日期」的错位显示（与猫眼那处同款的显示问题）。
  // 这里按行展开成 N 个场次；拿不到价格就标 noTiers，不硬凑价位。
  const fn = PLATFORMS[platform];
  if (!fn) throw new Error('暂不支持平台 ' + platform);
  const paramKey =
    platform === 'moretickets' ? 'tour_id'
    : platform === 'maoyan' ? 'performance_id'
    : (platform === 'damai' || platform === 'taopiaopiao') ? 'item_id'
    : 'id';
  const fnParams = { [paramKey]: id };
  // 注意：不把 event.myToken 传给 generic 分支——有 token 的情形在前面 maoyan
  // 专属分支处理（失败已静默落回匿名）；这里再传会让 token 失效错误穿透到详情页
  const items = await fn(fnParams);
  const sessions = (items || []).map((it) => ({
    name: it.label || it.when || '场次',
    when: it.when || '',
    status: it.status || '',
    available: !!it.available,
    tiers: [],
  }));
  // 不返回 name / venue：这条通道只能给「日期+场馆」这种行标签，拿它当演出名会把
  // 详情页标题写成日期。演出名/场馆由列表页跳转时随 URL 带进来，更准（实测踩过）。
  return { platform, id, sessions, noTiers: true };
}

// ===========================================================================
// 平台：纷玩岛（api.livelab.com.cn——纷玩岛小程序/H5 同一数据源）
// ===========================================================================
// 分两层：
//  ① 匿名项目级（免登录，2026-10-14 实测打通）：
//     GET /performance/app/project/h5/get_project_info?project_id=<id>
//     Referer: https://m.livelab.com.cn/ — 回 code 10000，含项目名/海报/城市/场馆/
//     起止日期/票价区间(lowPrice~highPrice)，并在 projectServices[].refundRuleList[]
//     带出部分场次(performId/performName/performStartTime)。id 可为数字 projectId，
//     也可为分享页那串加密 id。→ 详情页免凭证即可看演出信息。
//  ② 逐场次×逐票档（get_performs）需账号 token：无 token / 过期一律 code 11004
//     「没有登录」。所以 token 走**用户自带**：抓一次纷玩岛小程序的请求，把
//     authorization 请求头原文粘到小程序「我的」页存本地，随 event.fwdToken 传上来。
// 端点（对照 ThinkerWen/TicketMonitoring 的 Monitor_FWD.py）：
//   场次+票档: GET /performance/app/project/get_performs?project_id=<id>&v=<ms>&retry=false
//     → data.performInfos[].performInfo[0]: id/name/seatPlans[{seatPlanId,seatPlanName,display}]
//       display===1 = 在售（社区实测约定；其他值不猜）
//   开售倒计时: GET /performance/app/project/countDown?projectId=<id>
// 返回 code 10000 = 成功；11004 = 未登录（token 缺失/过期）。

const FWD_BASE = 'https://api.livelab.com.cn';
const FWD_UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) ' +
  'Chrome/98.0.4758.102 Safari/537.36 MicroMessenger/6.8.0(0x16080000) NetType/WIFI ' +
  'MiniProgramEnv/Mac MacWechat/WMPF XWEB/30817';

function fwdHeaders(token) {
  return {
    'User-Agent': FWD_UA,
    Accept: '*/*',
    'Content-Type': 'application/json',
    'Accept-Language': 'zh-CN,zh',
    Referer: 'https://servicewechat.com/wx5a8f481d967649eb/70/page-frame.html',
    authorization: 'Bearer ' + String(token || '').replace(/^Bearer\s+/i, ''),
  };
}

function fwdTokenError() {
  // 只在「匿名项目级详情也拿不到」时才走到这里。话术面向终端用户，短、可执行。
  return new Error('这场纷玩岛没打开——可能是链接过期或该场要登录才能看。换个链接试试；想按票档盯就在「我的」页填一次纷玩岛凭证');
}

// 匿名项目级详情（无需任何 token）：
//   GET /performance/app/project/h5/get_project_info?project_id=<id>
// 实测（2026-10-14）：不带 authorization 直接回 code 10000。project_id 既接受
// 数字 projectId，也接受分享页 hppreview 链接里那串加密 id（形如 AKj...=）。
// 返回项目名/海报/城市/场馆/起止日期/票价区间（lowPrice~highPrice），
// 并在 projectServices[].refundRuleList[] 里顺带带出部分场次
// （performId / performName / performStartTime）——这是匿名能拿到的最全一层。
// 逐场次×逐票档（get_performs）仍需账号 token，故匿名详情会标记 noTiers。
function fwdAnonHeaders() {
  return {
    'User-Agent': FWD_UA,
    Accept: 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh',
    Referer: 'https://m.livelab.com.cn/',
    Origin: 'https://m.livelab.com.cn',
  };
}

async function fwdGetProjectInfo(projectId) {
  const j = await getJson(
    `${FWD_BASE}/performance/app/project/h5/get_project_info?project_id=${encodeURIComponent(projectId)}`,
    { headers: fwdAnonHeaders(), timeout: 15000 },
  );
  if (!j || j.code !== 10000 || !j.data) {
    throw new Error('纷玩岛返回：' + ((j && j.msg) || ('code=' + (j && j.code))));
  }
  return j.data;
}

/** 把匿名项目级数据拼成统一的 detail 结构（场次来自 refundRuleList，去重保序）；
 *  tiers 是可选的有 token 场次票档映射（键为 performId 或场次名）。 */
function fwdDetailFromProject(id, d, tiers) {
  const seen = {};
  const sessions = [];
  const services = Array.isArray(d.projectServices) ? d.projectServices : [];
  for (const svc of services) {
    const rules = Array.isArray(svc.refundRuleList) ? svc.refundRuleList : [];
    for (const rr of rules) {
      const pid = String(rr.performId || '');
      const pname = String(rr.performName || '');
      const key = pid || pname;
      if (!key || seen[key]) continue;
      seen[key] = 1;
      sessions.push({
        name: pname || '场次',
        when: String(rr.performStartTime || '').replace('T', ' ').slice(0, 16),
        status: '',
        available: true,
        // 有 token 时按 performId / 场次名双键取逐档票；没有则留空 → 前端提示「要登录」
        tiers: (tiers && (tiers[pid] || tiers[pname])) || [],
      });
    }
  }
  const low = d.lowPrice != null ? Number(d.lowPrice) : null;
  const high = d.highPrice != null ? Number(d.highPrice) : null;
  let priceRange = '';
  if (low != null && high != null) priceRange = low === high ? '¥' + low : '¥' + low + '–' + high;
  else if (low != null) priceRange = '¥' + low + ' 起';
  const dateRange = d.timeDisplay || [d.projectStartDate, d.projectEndDate].filter(Boolean).join(' 至 ');
  const venue = (d.venueInfo && (d.venueInfo.name || d.venueInfo.address)) || '';
  // 预售/开售时间（字段名即语义，原文照搬不加工；只做 2026/10/15 13:00:00 → 10/15 13:00 的缩略）
  const shortTime = (s) => {
    const m = String(s || '').match(/(\d{1,2})\/(\d{1,2})\s+(\d{1,2}):(\d{2})/);
    return m ? m[1] + '/' + m[2] + ' ' + m[3] + ':' + m[4] : String(s || '');
  };
  const saleNotes = [];
  if (d.preSaleTime) saleNotes.push('预售 ' + shortTime(d.preSaleTime));
  if (d.saleStartTime) saleNotes.push('开售 ' + shortTime(d.saleStartTime));
  return {
    platform: 'fenwandao',
    id: String(id),
    name: d.projectName || d.nameDisplay || '',
    poster: d.poster || '',
    city: d.projectCity || '',
    venue: String(venue).slice(0, 60),
    dateRange,
    priceRange,
    saleNote: saleNotes.join(' · '),
    sessions,
    noTiers: !sessions.some((s) => s.tiers.length),
    anon: true,
  };
}

/** 拉 get_performs 并做 code 检查。11004 = token 缺失/过期，给用户能看懂的指引。 */
async function fwdGetPerforms(projectId, token) {
  if (!String(token || '').trim()) throw fwdTokenError();
  const j = await getJson(
    `${FWD_BASE}/performance/app/project/get_performs?project_id=${encodeURIComponent(projectId)}&v=${Date.now()}&retry=false`,
    { headers: fwdHeaders(token), timeout: 15000 },
  );
  if (j && j.code === 11004) {
    throw fwdTokenError();
  }
  if (!j || j.code !== 10000) {
    throw new Error('纷玩岛返回：' + ((j && j.msg) || ('code=' + (j && j.code))));
  }
  return (j.data && j.data.performInfos) || [];
}

/** 从链接/文本里抠纷玩岛项目 id。
 *  分享页形如 m.livelab.com.cn/hppreview/pages/concert/detail?id=AKj...=（加密 id，
 *  接口同样接受）；否则抠最长数字串兜底。 */
function extractFwdId(text) {
  const s = String(text || '');
  const m = s.match(/[?&]id=([A-Za-z0-9+/=_%-]{10,})/);
  if (m) {
    let v = m[1];
    try { v = decodeURIComponent(v); } catch (e) { /* 原样用 */ }
    return v;
  }
  const nums = s.match(/\d{3,}/g) || [];
  return nums.sort((a, b) => b.length - a.length)[0] || '';
}

async function pollFenwandao(params) {
  let pid = params.project_id || params.show_id || params.id || '';
  if (!pid) pid = extractFwdId(params.url || '');
  if (!pid) throw new Error('缺少纷玩岛项目 id——把分享链接或数字 id 粘进来即可');

  const infos = await fwdGetPerforms(pid, params.fwdToken);
  const items = [];
  for (const info of infos.slice(0, 20)) {
    const s = (info.performInfo || [])[0] || {};
    const plans = Array.isArray(s.seatPlans) ? s.seatPlans : [];
    if (plans.length) {
      for (const p of plans) {
        const on = p.display === 1;
        items.push({
          label: (s.name || '场次') + ' ' + (p.seatPlanName || '票档'),
          place: '',
          when: s.name || '',
          status: on ? '可购' : '不可购',
          available: on,
          count: on ? null : 0,
          price: p.seatPlanPrice != null ? '¥' + p.seatPlanPrice : '',
        });
      }
    } else {
      items.push({
        label: s.name || '场次',
        place: '',
        when: s.name || '',
        status: '没挂出票档',
        available: false,
        count: 0,
        price: '',
      });
    }
  }
  if (!items.length) throw new Error('纷玩岛没返回场次（项目可能已下架或 id 不对）');
  return items;
}

/** t.cn 短链 → 真实链接（微博分享出来的纷玩岛入口全是 t.cn，必须先解开）。
 *  解不出来就原样返回，让后面的 extractFwdId 兜底报错。 */
async function resolveShortLink(u) {
  try {
    const r = await request(u, { headers: { 'User-Agent': FWD_UA, Accept: '*/*' }, timeout: 10000 });
    const loc = r.headers && (r.headers.location || r.headers.Location);
    if ((r.status === 301 || r.status === 302) && loc) return String(loc);
  } catch (e) { /* 跟随不了就当普通文本 */ }
  return u;
}

/** 纷玩岛 find：链接/数字 id → 直取详情行（匿名即可）；关键词 → 先试搜索，失败用热卖榜兜底 */
async function findFenwandao(keyword, limit, token) {
  let raw = String(keyword || '').trim();
  if (/^https?:\/\/t\.cn\//i.test(raw)) raw = await resolveShortLink(raw);
  if (/https?:\/\//i.test(raw) || /[?&]id=[A-Za-z0-9+/=_%-]{10,}/.test(raw) || /^\d{3,}$/.test(raw)) {
    const id = /^\d{3,}$/.test(raw) ? raw : extractFwdId(raw);
    // 匿名项目级优先（免凭证），拿不到再退回带 token 的 get_performs
    try {
      const d = await fwdGetProjectInfo(id);
      const built = fwdDetailFromProject(id, d, null);
      return [{
        performanceId: String(id),
        projectId: String(id),
        name: built.name || ('纷玩岛 #' + id),
        city: built.city,
        venue: built.venue,
        dateRange: built.dateRange,
        price: built.priceRange,
        status: '',
        poster: built.poster,
      }];
    } catch (e) {
      // 落回 token 通道
    }
    const infos = await fwdGetPerforms(id, token);
    const first = ((infos[0] || {}).performInfo || [])[0] || {};
    const plans = Array.isArray(first.seatPlans) ? first.seatPlans : [];
    const prices = plans.map((p) => p.seatPlanPrice).filter((x) => x != null);
    return [{
      performanceId: id,
      projectId: id,
      name: (infos[0] && infos[0].projectName) || first.name || ('纷玩岛 #' + id),
      city: '', venue: '',
      dateRange: first.name || '',
      price: prices.length ? '¥' + Math.min.apply(null, prices) + ' 起' : '',
      status: plans.some((p) => p.display === 1) ? '在售' : '',
      poster: (infos[0] && (infos[0].projectImg || infos[0].poster)) || '',
    }];
  }
  if (!raw) throw new Error('搜索词不能为空');

  // 关键词搜索：先试搜索接口，失败用热卖榜兜底
  const searchOnce = async (hdrs) => {
    const j = await getJson(
      `${FWD_BASE}/performance/app/search/project?keyword=${encodeURIComponent(raw)}`,
      { headers: hdrs, timeout: 15000 },
    );
    if (j && j.code === 10000 && j.data) {
      const rows = (j.data.projects || j.data.list || j.data.result || []).slice(0, limit || 20);
      return rows.map((r) => ({
        performanceId: String(r.projectId || r.id),
        projectId: String(r.projectId || r.id),
        name: r.projectName || r.name || ('纷玩岛 #' + (r.projectId || r.id)),
        city: r.cityName || '',
        venue: r.venueName || '',
        dateRange: r.showTime || '',
        price: r.priceLow != null ? '¥' + r.priceLow + ' 起' : '',
        status: r.display === 1 ? '在售' : '',
        poster: r.projectImg || r.poster || '',
      }));
    }
    throw new Error('code=' + ((j && j.code) || '?') + ' ' + ((j && j.msg) || ''));
  };

  let hasToken = !!String(token || '').trim();
  try {
    return await searchOnce(hasToken ? fwdHeaders(token) : fwdAnonHeaders());
  } catch (anonErr) {
    // 搜索失败 → 用热卖榜兜底（匿名可用）
    try {
      const hotRows = await fwdHotList(limit);
      if (hotRows.length) return hotRows;
    } catch (e) {
      /* 热卖榜也失败 */
    }
    // 最终报错
    if (!hasToken) {
      throw new Error(
        '纷玩岛关键词搜索需要登录——请直接粘贴演出分享链接（从纷玩岛小程序「···」→ 分享 → 复制链接），或先在「我的」页填写 token'
      );
    }
    throw new Error(
      '纷玩岛搜索没通（' + String(anonErr.message || anonErr) + '）——请粘贴演出分享链接'
    );
  }
}

/** 纷玩岛热卖榜（匿名可用）→ 返回演出列表 */
async function fwdHotList(limit) {
  const j = await getJson(
    `${FWD_BASE}/appShow/app/homepage/projects?projectModuleId=1&pageNum=1&pageSize=${Math.min(Number(limit) || 20, 50)}`,
    { headers: fwdAnonHeaders(), timeout: 15000 },
  );
  if (j.code !== 10000 || !j.data || !Array.isArray(j.data.list)) return [];
  return j.data.list.slice(0, limit || 20).map((r) => ({
    performanceId: String(r.projectId || r.id || ''),
    projectId: String(r.projectId || r.id || ''),
    name: r.projectName || r.name || '',
    city: r.projectCity || r.cityName || '',
    venue: (r.venueInfo && r.venueInfo.name) || '',
    dateRange: r.timeDisplay || r.projectStartDate || '',
    price: r.lowPrice != null ? '¥' + r.lowPrice + ' 起' : '',
    status: '',
    poster: r.projectImg || '',
  }));
}

// ===========================================================================
// 分发
// ===========================================================================

const PLATFORMS = {
  maoyan: pollMaoyan,
  moretickets: pollMoretickets,
  rail12306: pollRail,
  damai: pollDamai,
  taopiaopiao: pollDamai,
  bilibili: pollBilibili,
  piaoxingqiu: pollPiaoxingqiu,
  fenwandao: pollFenwandao,
};

// 平台中文名：推送消息里的「放票机构」字段用
const LABEL_CN = {
  maoyan: '猫眼', moretickets: '摩天轮', rail12306: '12306',
  damai: '大麦', taopiaopiao: '淘票票', bilibili: 'B站会员购', piaoxingqiu: '票星球',
  fenwandao: '纷玩岛',
};

const PLATFORM_PROBE = {
  maoyan: () => `${MAOYAN_BASE}/ajax/performance/501675?sellChannel=7`,
  moretickets: null, // POST 接口，probe 走真实轮询
  rail12306: () => `${RAIL_BASE}/otn/leftTicket/init`,
  damai: () => `${MTOP_BASE}/mtop.damai.item.detail.getdetail/1.0/`,
  taopiaopiao: () => `${MTOP_BASE}/mtop.alibaba.damai.detail.getdetail/1.2/`,
  bilibili: () =>
    `${BILI_SHOW}/api/ticket/project/listV2?version=134&platform=web&page=1&pagesize=5&ptype=100104&area=0&keyword=${encodeURIComponent('演唱会')}`,
  piaoxingqiu: () => `${PXQ_M}/cyy_gatewayapi/home/pub/v3/show/recommend`,
  fenwandao: null, // 全接口要用户 token，probe 跳过（无 token 时 poll 会给指引）
};

function errorKind(e) {
  const msg = String((e && e.message) || e);
  if (/HTML|风控/.test(msg)) return 'blocked';       // 被平台风控（云 IP 可能被识别）
  if (/HTTP 4|HTTP 5/.test(msg)) return 'http';
  if (/超时/.test(msg)) return 'timeout';
  if (/statusCode|code=/.test(msg)) return 'platform'; // 平台自己报错（日期/站码/参数）
  return 'unknown';
}

// ===========================================================================
// 回流票判定：只报「从无到有」，别的状态变化不吵人
// ===========================================================================
//
// 用户盯票真正的诉求不是「开售了吗」，而是「售罄之后有没有票放回来」
// （退票、锁票释放、分批放库存）。所以判定只做一件事：
// 上一轮 available=false 且这一轮 available=true 的那个瞬间 = 回流。

/**
 * 对比上一轮和这一轮的场次余票，返回回流的场次名列表。
 * 上轮抓取失败（prev.ok=false）时不判定——拿失败数据当基线会漏报/误报。
 */
/** 从任务参数里取「要盯的价位/席别」关键字列表。
 *  price_labels（多选，当前）> price_label（旧单数）> seat_types（12306 席别）。 */
function wantedTiers(params) {
  const raw =
    (params && (params.price_labels || params.price_label || params.seat_types)) || [];
  return (Array.isArray(raw) ? raw : [raw])
    .map((s) => String(s == null ? '' : s).trim())
    .filter(Boolean);
}

/** 单个条目是否命中某个关键字。
 *  数字关键字必须**按数字整体相等**匹配——否则 399 会命中 1399、699 会命中 1699，
 *  用户「只想盯 399 看台」却被 1399 内场放票推醒（实测踩到，2026-10）。
 *  非数字关键字（如 12306 的「二等座」、票星球的「看台」）才走子串匹配。 */
function tierHit(it, w) {
  if (/^\d+$/.test(w)) {
    const nums = String(it.price || '').match(/\d+/g) || [];
    if (nums.length) return nums.indexOf(w) >= 0;
    // 没给价格字段才退化到整行找数字
    const all = (String(it.label || '') + ' ' + String(it.status || '')).match(/\d+/g) || [];
    return all.indexOf(w) >= 0;
  }
  return [it.label, it.status, it.price].some((s) => String(s || '').indexOf(w) >= 0);
}

/** 只保留命中至少一个关键字的条目。
 *  多选语义：勾了几个价位，几个都盯——**并集**，不是「同时命中」。 */
function filterItemsByTiers(items, params) {
  const wanted = wantedTiers(params);
  if (!wanted.length) return items || [];
  return (items || []).filter((it) => wanted.some((w) => tierHit(it, w)));
}

/** 任务里勾的场次名（多选）。没勾返回空数组 = 全选，不过滤。 */
function wantedSessions(params) {
  const raw = (params && params.sessions) || [];
  return (Array.isArray(raw) ? raw : [raw])
    .map((s) => String(s == null ? '' : s).trim())
    .filter(Boolean);
}

/** 只保留命中已勾场次的条目。
 *  场次与行的对应各平台不同：摩天轮是「一行 = 一个场次」（label 里带日期+场馆），
 *  票星球/B站一行的 label 里也含场次名。所以命中 when 或 label 任一即算该场。
 *  加它的原因：详情页早就让用户勾场次了，但服务端从来没按它过滤过——
 *  勾了等于没勾。 */
function filterItemsBySessions(items, params) {
  const wanted = wantedSessions(params);
  if (!wanted.length) return items || [];
  return (items || []).filter((it) =>
    wanted.some((sn) => {
      const w = String((it && it.when) || '').trim();
      const l = String((it && it.label) || '');
      return (w && (w === sn || w.indexOf(sn) >= 0 || sn.indexOf(w) >= 0)) || l.indexOf(sn) >= 0;
    }),
  );
}

function diffRestock(prev, next) {
  if (!prev || !prev.ok || !next || !next.ok) return [];
  const before = {};
  for (const it of prev.items || []) {
    if (it && it.label != null) before[it.label] = !!it.available;
  }
  const back = [];
  for (const it of next.items || []) {
    if (it && it.available && before[it.label] === false) back.push(it.label);
  }
  return back;
}

/** 给某个 openid 扣一次订阅额度。扣成功（有额度）返回 true。 */
async function takeSubQuota(db, openid) {
  const { data } = await db.collection(DB_SUBS).where({ openid: openid }).limit(1).get();
  const sub = (data || [])[0];
  if (!sub || !(sub.quota > 0)) return false;
  await db.collection(DB_SUBS).doc(sub._id).update({ data: { quota: sub.quota - 1 } });
  return true;
}

// ===========================================================================
// 跨平台比价合并
// ===========================================================================

/** 从搜索关键词提取核心名字（去数字、年份、站名等噪音） */
function normalizeCompareName(raw) {
  return String(raw || '')
    .replace(/\d{4}[\/年\.]\d{1,2}[\/月\.]?/g, '')   // 年份日期
    .replace(/第?\s*\d+[代轮届]?巡回/g, '')           // 巡演描述
    .replace(/[一-龥]{1,2}站/g, '')          // 站名
    .trim();
}

/** 两个名字是否指向同一演出（模糊匹配） */
function sameShow(a, b) {
  const na = normalizeCompareName(a);
  const nb = normalizeCompareName(b);
  if (na === nb) return true;
  if (na.length > 3 && nb.length > 3) {
    // 互相包含且核心词一致
    const short = na.length < nb.length ? na : nb;
    const long = na.length < nb.length ? nb : na;
    if (long.indexOf(short) >= 0 && short.length >= 4) return true;
  }
  return false;
}

/** 合并多平台搜索结果：同名演出归一组，组内按价格排序 */
function mergeCompareResults(platforms, keyword) {
  // 1. 把所有平台的行 flattened，带 platform 标记
  const all = [];
  for (const [p, r] of Object.entries(platforms)) {
    if (!r.ok || !Array.isArray(r.rows)) continue;
    for (const row of r.rows) {
      all.push(Object.assign({}, row, { _cmpPlatform: p }));
    }
  }
  // 2. 按名字分组：同演出的放一起
  const groups = [];
  for (const row of all) {
    const name = row.name || row.title || row.showName || '';
    let placed = false;
    for (const g of groups) {
      if (sameShow(g.name, name)) { g.rows.push(row); g.name = g.name.length > name.length ? g.name : name; placed = true; break; }
    }
    if (!placed) groups.push({ name, rows: [row] });
  }
  // 3. 每组按最低价排序（有 priceRange 的优先）
  for (const g of groups) {
    g.rows.sort((a, b) => {
      const pa = extractPriceMin(a.price);
      const pb = extractPriceMin(b.price);
      if (pa && pb) return pa - pb;
      if (pa) return -1;
      if (pb) return 1;
      return 0;
    });
  }
  // 4. 过滤掉只搜到 1 行且其他平台完全没命中的（可能是噪音）
  return groups.filter((g) => g.rows.length >= 1).slice(0, 8);
}

function extractPriceMin(priceStr) {
  const m = String(priceStr || '').match(/(\d+)/);
  return m ? Number(m[1]) : null;
}

// ===========================================================================
// 入口
// ===========================================================================

// 主逻辑（原 exports.main 主体）。海报 URL 的规范化在新 exports.main
// 的出口统一做，各 adapter 只管把平台返回的字段掏出来。
async function handleAction(event, action, db) {

  try {
    // --- 部署后的第一件事：确认云 IP 能不能抓到数据 -------------------
    if (action === 'probe') {
      const out = { checked_at: new Date().toISOString(), platforms: {} };
      for (const name of Object.keys(PLATFORM_PROBE)) {
        const probe = PLATFORM_PROBE[name];
        // 值是 thunk（延迟求值的 URL 模板串），不是字符串——要先调用
        const url = typeof probe === 'function' ? probe() : probe;
        if (!url) {
          out.platforms[name] = { ok: null, note: 'POST 接口，跳过（用 poll 测）' };
          continue;
        }
        const t0 = Date.now();
        try {
          const r = await request(url, { headers: railHeaders(), timeout: 15000 });
          const html = /<html/i.test(r.body.slice(0, 300));
          out.platforms[name] = {
            ok: r.status === 200,
            status: r.status,
            ms: Date.now() - t0,
            html_page: html,
            note: html ? '返回了 HTML 风控页——云 IP 很可能被拒' : '',
          };
        } catch (e) {
          out.platforms[name] = { ok: false, ms: Date.now() - t0, error: String(e.message || e) };
        }
      }
      return { ok: true, ...out };
    }

    // --- 轮询 ---------------------------------------------------------
    if (action === 'poll') {
      // 无状态模式（第二十轮，Vercel 免费架构）：tasks 与上一轮状态由客户端
      // （小程序本地 storage）随请求传入，服务端只查平台、算回流、返回结果，
      // 不落任何库。event.tasks 缺省时仍读云数据库（云函数模式保持兼容）。
      let tasks;
      if (Array.isArray(event.tasks)) {
        tasks = event.tasks;
      } else {
        const t = await db.collection(DB_TASKS).where({ enabled: true }).get();
        tasks = (t && t.data) || [];
      }
      const prevStates = (event.prevStates && typeof event.prevStates === 'object') ? event.prevStates : null;
      const now = new Date().toISOString();

      // 并发轮询：每个任务独立请求，总耗时 = 最慢的那个而非累加
      // 限制并发数避免触发平台限频（最多同时 6 个）
      const CONCURRENCY = 6;
      const results = [];

      async function pollOne(task) {
        // 先读上一轮状态再覆盖写：回流判定、history 曲线都要拿它当基线。
        // 无状态模式下基线来自 event.prevStates[task._id]；云函数模式读数据库。
        let prev = null;
        if (prevStates) {
          prev = prevStates[task._id] || null;
        } else {
          // 文档不存在时 get 会抛错，catch 成 null（第一次跑没有基线，正常）。
          const prevRes = await db.collection(DB_STATE).doc(task._id).get().catch(() => null);
          prev = (prevRes && prevRes.data) || null;
        }

        const fn = PLATFORMS[task.adapter];
        const rec = {
          _id: task._id, platform: task.adapter, name: task.name || task._id,
          // 海报 URL 建任务时存在 radar_tasks 里，这里透传给前端渲染监控卡缩略图
          poster: task.poster || '',
          checked_at: now, ok: false, error: '', error_kind: '', items: [],
        };
        if (!fn) {
          rec.error = '云函数暂不支持平台 ' + task.adapter;
          rec.error_kind = 'unsupported';
        } else {
          try {
            // 纷玩岛 token 优先用随请求传的最新值（用户在「我的」页更新后
            // 不用重建任务），任务参数里存的那份只做兜底。
            const fnParams = Object.assign({}, task.params || {});
            if (task.adapter === 'fenwandao' && !fnParams.fwdToken && event.fwdToken) {
              fnParams.fwdToken = event.fwdToken;
            }
            if (task.adapter === 'damai' && event.damaiCookie) {
              fnParams.damaiCookie = event.damaiCookie;
            }
            if (task.adapter === 'maoyan' && event.myToken) {
              fnParams.myToken = event.myToken;
            }
            rec.items = await fn(fnParams);
            rec.ok = true;
            // 价位级监控：建任务时勾了价位就只盯这几档（**多选**，并集）。
            // 关键字命中 label/status/price 任一即算该档，因为平台给的 items 粒度
            // 不一：B站/纷玩岛/票星球本来就是票档级，12306 把席别写在 status 里，
            // 大麦/猫眼是整场价格区间——都能被关键字罩住。
            const wanted = wantedTiers(task.params);
            if (wanted.length) {
              rec.items = filterItemsByTiers(rec.items, task.params);
              rec.tier_filter = wanted;
            }
            // 场次级过滤：详情页勾了场次就只算这几场（摩天轮「一行=一个场次」靠这个落地）
            const wantSess = wantedSessions(task.params);
            if (wantSess.length) {
              rec.items = filterItemsBySessions(rec.items, task.params);
              rec.session_filter = wantSess;
            }
            rec.available = rec.items.filter((i) => i.available).length;
          } catch (e) {
            rec.error = String((e && e.message) || e);
            rec.error_kind = errorKind(e);
          }
        }

        // 回流判定：无 → 有 的场次
        const back = diffRestock(prev, rec);
        if (back.length) {
          rec.transition = 'restock';
          rec.restock = back;
        }

        // history 从上一轮接续（旧实现每次都是新对象，曲线永远只有一根柱——bug）
        let history = (prev && prev.history) || [];
        if (rec.ok) history = history.concat([{ t: now, n: rec.available || 0 }]).slice(-30);
        rec.history = history;

        // 回流推送：一次性订阅，发一条扣一条额度
        rec.notify = null;
        if (rec.transition === 'restock' && task._openid) {
          rec.notify = { attempted: true, sent: false };
          try {
            if (await takeSubQuota(db, task._openid)) {
              const first = (rec.items || []).find((i) => i.available) || {};
              // 模板字段以小程序后台实际配置为准；thing 类字段限 20 字
              await cloud.openapi.subscribeMessage.send({
                touser: task._openid,
                templateId: NOTIFY_TMPL,
                page: 'pages/monitor/monitor',
                data: {
                  thing6: { value: String(rec.name || '你盯的演出').slice(0, 20) },
                  thing8: { value: String((first.label || '有场次放票')).slice(0, 20) },
                  time7: { value: new Date().toLocaleString('zh-CN', { hour12: false }) },
                  thing5: { value: String(LABEL_CN[task.adapter] || task.adapter).slice(0, 20) },
                  thing4: { value: '回流票出现，尽快去官方渠道下单' },
                },
              });
              rec.notify.sent = true;
            } else {
              rec.notify.reason = 'no_quota';
            }
          } catch (e) {
            rec.notify.error = String((e && e.errMsg) || (e && e.message) || e);
          }
        }

        await db.collection(DB_STATE).doc(task._id).set({ data: rec });
        results.push({
          _id: rec._id, ok: rec.ok,
          restock: (rec.restock || []).length,
          notify: rec.notify,
          error: rec.error,
          // 无状态模式（第二十四轮）：云库是 no-op，客户端要拿全量记录
          // 渲染监控卡 + 存下一轮基线。云函数模式客户端忽略此字段，不受影响。
          full: rec,
        });
      }

      // 并发执行所有任务（限制 CONCURRENCY 同时跑，避免平台限频）
      const taskPromises = (tasks || []).map((task) => pollOne(task));
      const batches = [];
      for (let i = 0; i < taskPromises.length; i += CONCURRENCY) {
        batches.push(Promise.all(taskPromises.slice(i, i + CONCURRENCY)));
      }
      await Promise.all(batches.flat());

      return { ok: true, at: now, tasks: results.length, results: results };
    }

    // --- 搜索（小程序「场次」页用）-------------------------------------
    // 用户只打关键词，我们去平台把候选捞回来。不用让用户去别的 App
    // 翻链接抠 id——那一步 90% 的人会放弃。
    if (action === 'find') {
      const keyword = String((event && event.keyword) || '').trim();
      const limit = (event && event.limit) || 20;
      const platform = String((event && event.platform) || 'moretickets').trim();

      // 空关键词 = 各平台「正在热卖」。实测（2026-10-14 补测）：
      //   猫眼 k= 直返在售（带海报）；B站 listV2 免 keyword 返推荐位；
      //   票星球 show/recommend 返 40 条热卖榜；大麦/淘票票走新版 H5 的
      //   CMS 分类通道（damaiHot，匿名零 Cookie 可用）。
      //   摩天轮用「演唱会」关键词兜底（真实数据）；12306 是站站查询。
      if (!keyword) {
        if (platform === 'maoyan') {
          const rows = await findMaoyan('', limit);
          return { ok: true, keyword: '', platform, rows };
        }
        if (platform === 'bilibili') {
          const rows = await findBilibili('', limit);
          return { ok: true, keyword: '', platform, rows };
        }
        if (platform === 'piaoxingqiu') {
          const rows = await hotPiaoxingqiu(limit);
          return { ok: true, keyword: '', platform, rows };
        }
        if (platform === 'damai' || platform === 'taopiaopiao') {
          // 热卖榜：优先走新版 H5 的 CMS 分类通道（**匿名零 Cookie 可用**，本轮打通）。
          // 拿不到再退回搜索接口（要 Cookie），最后才给指引——不硬编假数据。
          try {
            const rows = await damaiHot(limit, event.cityId);
            if (rows.length) return { ok: true, keyword: '', platform, rows };
          } catch (e) {
            /* 落到搜索通道 */
          }
          try {
            const rows = await damaiSearch('演唱会', limit, event.cityId, event.damaiCookie);
            return { ok: true, keyword: '', platform, rows };
          } catch (e) {
            if (/排起了长队|RGV587|挤爆/.test(String((e && e.message) || e))) {
              return {
                ok: false,
                error: '大麦热卖榜接口暂时不可用——把演出分享链接或数字编号粘进搜索框，匿名就能盯',
              };
            }
            return { ok: false, error: String((e && e.message) || e) };
          }
        }
        if (platform === 'moretickets') {
          const rows = await findMoretickets('演唱会', limit);
          return { ok: true, keyword: '', platform, rows };
        }
        if (platform === 'fenwandao') {
          // 纷玩岛：先试匿名热卖榜，失败再提示粘贴链接
          try {
            const rows = await fwdHotList(limit);
            if (rows.length) return { ok: true, keyword: '', platform, rows };
          } catch (e) {
            /* 热卖榜失败 */
          }
          return { ok: false, error: '纷玩岛热门演出暂时拿不到——请直接粘贴演出分享链接（从纷玩岛小程序「···」→ 分享 → 复制链接），或先在「我的」页填写 token 后搜关键词' };
        }
        return { ok: false, error: '12306 是查两站之间的余票，没有「热卖榜」——填上出发地和目的地就能查' };
      }

      if (platform === 'maoyan') {
        const rows = await findMaoyan(keyword, limit);
        return { ok: true, keyword, platform, rows };
      }
      if (platform === 'moretickets') {
        const rows = await findMoretickets(keyword, limit);
        return { ok: true, keyword, platform, rows };
      }
      if (platform === 'rail12306') {
        const rows = await findRail(keyword, (event && event.date) || '', limit);
        return { ok: true, keyword, platform, rows };
      }
      // 大麦/淘票票：链接/纯数字 id → mtop 详情+票档；关键词 → mtop 无线搜索。
      // 淘票票的演出就是大麦的 itemId（m.taopiaopiao.com/damai/detail/item.html?id=X），
      // 搜索/详情全走大麦同一套通道——一套代码两个 tab 共用（2026-10-14）。
      if (platform === 'damai' || platform === 'taopiaopiao') {
        try {
          const rows = await findDamai(keyword, event.damaiCookie, event.cityId);
          return { ok: true, keyword, platform, rows };
        } catch (e) {
          const msg = String((e && e.message) || e);
          // 关键词搜索有滑块风控（FAIL_SYS_USER_VALIDATE / RGV587），突发请求会触发、
          // 按 IP 冷却几分钟。别把它当成「没有这个演出」——说清怎么办。
          if (/RGV587|挤爆|排起了长队|USER_VALIDATE/.test(msg)) {
            return {
              ok: false,
              error: '大麦搜索被风控拦了一下（短时间内请求过多）—— 过几分钟再搜，'
                + '或直接切到空搜索看热卖榜；在「我的」页填一次大麦 Cookie 会更稳',
            };
          }
          return { ok: false, error: msg };
        }
      }
      // B站会员购：公开搜索接口（listV2），和大麦/猫眼一样直接打字搜
      if (platform === 'bilibili') {
        const rows = await findBilibili(keyword, limit);
        return { ok: true, keyword, platform, rows };
      }
      // 票星球：关键词 → 全国搜索（2026-10 打通）；粘贴分享链接 → 抠 id 抓详情
      if (platform === 'piaoxingqiu') {
        const linkId = extractPxqId(keyword);
        if (linkId) {
          const items = await pollPiaoxingqiu({ show_id: linkId });
          const first = items[0] || {};
          return {
            ok: true, keyword, platform,
            rows: [{
              performanceId: linkId, showId: linkId,
              name: first.label || ('票星球 #' + linkId),
              city: '', venue: first.place || '', dateRange: first.when || '',
              price: first.price || '', status: first.status || '', poster: '',
            }],
          };
        }
        const rows = await findPiaoxingqiu(keyword, limit);
        return { ok: true, keyword, platform, rows };
      }
      // 纷玩岛：链接/数字 id → 直取详情行；关键词 → 带用户 token 搜（失败给指引）
      if (platform === 'fenwandao') {
        const rows = await findFenwandao(keyword, limit, event.fwdToken);
        return { ok: true, keyword, platform, rows };
      }
      return { ok: false, error: '暂不支持平台 ' + platform };
    }

    // --- 机票：同程价格日历（价格浮动 / 特价发现）------------------------
    // 全程匿名，不碰任何 Cookie / token：用户只需要选「从哪到哪」。
    if (action === 'flight') {
      try {
        const data = await flightSvc.calendar(
          event && event.from,
          event && event.to,
          event && event.date,
          event && event.days,
        );
        return Object.assign({ ok: true, platform: 'flight' }, data);
      } catch (e) {
        return { ok: false, error: String((e && e.message) || e) };
      }
    }

    // --- 机票：某天全部航班 + 每个航班的多档舱位 ------------------------
    // 走同程列表页的 Nuxt SSR 内嵌状态（见 flight.js bookList）
    if (action === 'flightList') {
      try {
        const data = await flightSvc.bookList(
          event && event.from,
          event && event.to,
          event && event.date,
        );
        return Object.assign({ ok: true, platform: 'flight' }, data);
      } catch (e) {
        return { ok: false, error: String((e && e.message) || e) };
      }
    }

    // --- 机票城市检索（本地表，不走网络）------------------------------
    if (action === 'flightCities') {
      return { ok: true, cities: flightSvc.citySearch(event && event.keyword) };
    }

    // --- 演出详情：场次 × 票档 全展开（详情页用）------------------------
    if (action === 'detail') {
      const platform = String((event && event.platform) || '').trim();
      const id = String((event && event.id) || '').trim();
      if (!platform || !id) return { ok: false, error: '缺少 platform 或 id' };
      try {
        const r = await detailShow(platform, id, event);
        return Object.assign({ ok: true }, r);
      } catch (e) {
        return { ok: false, error: String((e && e.message) || e) };
      }
    }

    // --- 跨平台比价：同关键词打所有平台，返回合并结果 -------------------
    // 用户想看"这场演出在不同平台什么价、有没有票"，不用切来切去。
    // 并发请求各平台 find，超时/失败的只记 error 不阻断整体。
    if (action === 'compare') {
      const keyword = String((event && event.keyword) || '').trim();
      const limit = Math.min(10, Math.max(1, Number((event && event.limit) || 5)));
      if (!keyword) return { ok: false, error: '需要搜索关键词' };
      const platforms = ['damai', 'maoyan', 'moretickets', 'bilibili', 'piaoxingqiu'];
      const results = {};
      await Promise.all(
        platforms.map(async (p) => {
          try {
            const r = await handleAction(
              Object.assign({}, event, { action: 'find', platform: p, keyword, limit }),
              'find', db,
            );
            results[p] = r.ok ? { ok: true, rows: (r.rows || []).slice(0, limit) } : { ok: false, error: r.error || '未知错误' };
          } catch (e) {
            results[p] = { ok: false, error: String((e && e.message) || e) };
          }
        }),
      );
      // 合并：按名字 fuzzy 匹配归并同一演出
      const merged = mergeCompareResults(results, keyword);
      return { ok: true, at: new Date().toISOString(), platforms: results, merged };
    }

    // --- 智能预测：基于历史数据预测最佳抢票时间 -------------------------
    // 分析最近 N 次采样，找出「从无票变有票」的时间规律，给用户建议。
    // 不需要数据库——客户端把 history 数组传上来，服务端算。
    if (action === 'predict') {
      const history = Array.isArray(event.history) ? event.history : [];
      if (history.length < 3) {
        return { ok: true, prediction: null, note: '采样数据不足 3 条，无法预测' };
      }
      const pts = history.slice(-50).filter((h) => h && h.t && h.n != null);
      if (pts.length < 3) {
        return { ok: true, prediction: null, note: '有效采样不足 3 条' };
      }
      // 找出所有「从 0 变正」的转折点
      const drops = [];
      for (let i = 1; i < pts.length; i++) {
        if (pts[i - 1].n === 0 && pts[i].n > 0) {
          drops.push(new Date(pts[i].t).getHours() + pts[i].t.slice(14, 16));
        }
      }
      // 统计各时段的放票频率
      const hourCounts = {};
      for (const d of drops) {
        const h = d.slice(0, 2);
        hourCounts[h] = (hourCounts[h] || 0) + 1;
      }
      const topHours = Object.entries(hourCounts)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 3)
        .map(([h, c]) => h + '时（' + c + '次）');

      // 判断当前趋势：最近 5 次采样里有多少次有票
      const recent = pts.slice(-5);
      const recentHas = recent.filter((p) => p.n > 0).length;
      const last = pts[pts.length - 1];
      let trend = '无明显规律';
      if (recentHas >= 4) trend = '近期频繁放票，建议持续关注';
      else if (recentHas === 0 && drops.length > 0) trend = ' historically 有回流记录，建议蹲守';
      else if (recentHas === 0) trend = '连续售罄，可能是未开售或真的没票';

      return {
        ok: true,
        prediction: {
          trend,
          topHours: topHours.length ? topHours : ['暂无历史放票规律'],
          totalDrops: drops.length,
          samples: pts.length,
          lastCount: last.n,
          lastTime: last.t,
        },
      };
    }

    // --- 讨论区帖子列表 ---------------------------------------------
    if (action === 'listPosts') {
      const keyword = String((event && event.keyword) || '').trim();
      const offset = Math.max(0, Number((event && event.offset) || 0));
      const limit = Math.min(50, Math.max(1, Number((event && event.limit) || 20)));

      let query = db.collection(DB_POSTS);
      if (keyword) {
        // 简单搜索：内容或标签包含关键词。云数据库不支持全文检索，
        // 这里用正则做模糊匹配；帖子量不大时够用了。
        const reg = db.RegExp({ regexp: keyword, options: 'i' });
        query = query.where(
          db.command.or([
            { content: reg },
            { tags: keyword },
            { nickname: reg },
          ])
        );
      }
      const { data: posts } = await query
        .orderBy('createdAt', 'desc')
        .skip(offset)
        .limit(limit)
        .get();
      return { ok: true, posts: posts || [] };
    }

    // --- 讨论区发帖 -------------------------------------------------
    if (action === 'createPost') {
      const content = String((event && event.content) || '').trim();
      if (!content) return { ok: false, error: '内容不能为空' };
      if (content.length > 500) return { ok: false, error: '内容超过 500 字' };

      const tags = Array.isArray(event && event.tags) ? event.tags : [];
      const nickname = String((event && event.nickname) || '匿名狐友').trim();
      const avatar = String((event && event.avatar) || '').trim();
      // type：问题反馈/建议留言/求助求助；sticker：留言墙素材 id（白名单校验防路径注入）
      const type = String((event && event.type) || '').trim().slice(0, 10);
      const sticker = String((event && event.sticker) || '').trim();
      if (sticker && !/^(note[1-8]|apple|starPurple|leaf|dolls|puzzle|twins|ice)$/.test(sticker)) {
        return { ok: false, error: '素材不合法' };
      }

      // 简单内容审核：禁止常见广告/引流关键词
      const forbidden = /(加微信|加v|转账|二维码|代拍|黄牛|票贩子|出票.*先款)/i;
      if (forbidden.test(content)) {
        return { ok: false, error: '检测到广告/引流内容，已拦截。请发与场次相关的交流信息。' };
      }

      const { OPENID } = cloud.getWXContext();
      const doc = {
        openid: OPENID || '',
        nickname,
        avatar,
        content,
        tags,
        createdAt: new Date().toISOString(),
        likes: 0,
        replies: 0,
      };
      const { _id } = await db.collection(DB_POSTS).add({ data: doc });
      return { ok: true, _id };
    }

    // --- 订阅额度云端记账 ---------------------------------------------
    // 用户每点一次「允许通知」，小程序端就调一次 grantSub。
    // 云函数发推送时从这里扣额度；本地 storage 的数字只是显示缓存。
    if (action === 'grantSub') {
      const n = Math.min(5, Math.max(1, Number((event && event.n) || 1)));
      const { OPENID } = cloud.getWXContext();
      if (!OPENID) return { ok: false, error: '拿不到 openid' };
      const { data } = await db.collection(DB_SUBS).where({ openid: OPENID }).limit(1).get();
      let quota;
      if (data && data.length) {
        quota = (data[0].quota || 0) + n;
        await db.collection(DB_SUBS).doc(data[0]._id).update({
          data: { quota: quota, updatedAt: new Date().toISOString() },
        });
      } else {
        quota = n;
        await db.collection(DB_SUBS).add({
          data: { openid: OPENID, quota: quota, updatedAt: new Date().toISOString() },
        });
      }
      return { ok: true, quota: quota };
    }
    if (action === 'querySub') {
      const { OPENID } = cloud.getWXContext();
      if (!OPENID) return { ok: false, error: '拿不到 openid' };
      const { data } = await db.collection(DB_SUBS).where({ openid: OPENID }).limit(1).get();
      return { ok: true, quota: (data && data[0] && data[0].quota) || 0 };
    }

    // --- 查询（小程序读这个）-------------------------------------------
    const { data: states } = await db.collection(DB_STATE).limit(100).get();
    return { ok: true, at: new Date().toISOString(), states: states || [] };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
};

// 导出内部函数供离线测试用。生产环境走 exports.main 就行，
// 但没有这个出口就没法在本地用真 API 验证解析逻辑对不对——
// 而「解析错了」是云函数最容易犯且最难发现的错。
exports._internals = {
  parseSeat: parseSeat,
  pollMaoyan: pollMaoyan,
  pollMoretickets: pollMoretickets,
  pollRail: pollRail,
  findMaoyan: findMaoyan,
  findMoretickets: findMoretickets,
  findRail: findRail,
  fetchStationMap: fetchStationMap,
  resolveStationCode: resolveStationCode,
  errorKind: errorKind,
  diffRestock: diffRestock,
  wantedTiers: wantedTiers,
  filterItemsByTiers: filterItemsByTiers,
  wantedSessions: wantedSessions,
  filterItemsBySessions: filterItemsBySessions,
  maoyanShows: maoyanShows,
  damaiSearch: damaiSearch,
  pollDamai: pollDamai,
  damaiDetailSSR: damaiDetailSSR,
  damaiDetailV12: damaiDetailV12,
  damaiSessionTiers: damaiSessionTiers,
  mtopH5: mtopH5,
  getJson: getJson,
  maoyanHeaders: maoyanHeaders,
  mtopCall: mtopCall,
  findBilibili: findBilibili,
  pollBilibili: pollBilibili,
  findPiaoxingqiu: findPiaoxingqiu,
  pollPiaoxingqiu: pollPiaoxingqiu,
  hotPiaoxingqiu: hotPiaoxingqiu,
  fwdGetProjectInfo: fwdGetProjectInfo,
  fwdDetailFromProject: fwdDetailFromProject,
  fwdGetPerforms: fwdGetPerforms,
  extractFwdId: extractFwdId,
  detailShow: detailShow,
};

// ===========================================================================
// 海报 URL 出口规范化（第十九轮）
// ---------------------------------------------------------------------------
// 小程序 <image> 加载外链海报的硬约束：
//   1. 协议相对 URL（//i2.hdslb.com/...）不认——必须补 https:；
//   2. http:// 混合内容不支持——统一升 https；
//   3. 域名必须在小程序后台「downloadFile 合法域名」里（见 README 清单）。
// 各 adapter 返回的字段名不统一（poster/imgUrl/verticalPic/cover），
// 在这里递归统一处理，出口只有一种干净形态。
function normalizePosterUrl(u) {
  if (typeof u !== 'string') return '';
  let s = u.trim();
  if (!s) return '';
  if (s.startsWith('//')) s = 'https:' + s;
  else if (s.slice(0, 7).toLowerCase() === 'http://') s = 'https:' + s.slice(5);
  return s;
}

const POSTER_FIELDS = ['poster', 'imgUrl', 'verticalPic', 'cover'];

function normalizePosters(out) {
  if (!out || typeof out !== 'object') return out;
  const seen = new Set();
  const walk = (v) => {
    if (Array.isArray(v)) { v.forEach(walk); return; }
    if (!v || typeof v !== 'object' || seen.has(v)) return;
    seen.add(v);
    for (const k of POSTER_FIELDS) {
      if (k in v) v[k] = normalizePosterUrl(v[k]);
    }
    for (const k of Object.keys(v)) {
      const x = v[k];
      if (x && typeof x === 'object') walk(x);
    }
  };
  walk(out);
  return out;
}

exports.main = async (event) => {
  try {
    return normalizePosters(
      await handleAction(event, (event && event.action) || 'query', cloud.database())
    );
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
};
