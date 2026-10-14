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
    throw new Error('返回了 HTML——通常是风控拦截页（云 IP 被识别了？）');
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

const NOT_OFFERED = ['', '--', '*', '—', '-'];
const ABUNDANT = ['有', '充足', '有票'];
const SOLD_OUT = ['无', '无票', '0'];

/**
 * 解析单个席别的余票值。
 * 返回 null 表示本次列车不提供该席别——**不认识的值宁可漏报也不误报**。
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
async function pollMaoyan(params) {
  const id = params.performance_id || params.id;
  if (!id) throw new Error('缺少 performance_id');
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
// 官方会轮换这几个端点，按顺序试到能用为止
const RAIL_ENDPOINTS = ['queryE', 'queryZ', 'queryA', 'query'];

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
  // map 的 value 是中文名，反查它的 key（电报码）
  for (const code of Object.keys(map)) {
    if (map[code] === raw) return code;
  }
  return raw;
}

async function pollRail(params) {
  const { from, to, date, seat_types: seatTypes } = params;
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
  for (const ep of RAIL_ENDPOINTS) {
    try {
      data = await getJson(`${RAIL_BASE}/otn/leftTicket/${ep}?${q}`, {
        headers: railHeaders(cookie),
      });
      break;
    } catch (e) {
      lastErr = e;
    }
  }
  if (!data) throw lastErr || new Error('所有端点都失败了');
  if (data.httpstatus !== 200 && data.status === false) {
    throw new Error('12306 返回 status=false（日期超预售期或站码不对）');
  }
  const result = data.data || {};
  const rows = result.result || [];

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

// ===========================================================================
// 平台：大麦
// ===========================================================================
// 两条匿名可用路径（2026-10 实测打通）：
//   1) 详情页 SSR：用户提供 itemId（从大麦 App/网页分享链接里提取），
//      解析内嵌 JSON 拿项目级状态：开售时间 / 价格范围 / 场次列表（performBases）。
//   2) mtop 无线搜索：关键词 → mtop.damai.wireless.search.search，返回 projectInfo，
//      用户不用去 App 翻链接抠 id——和猫眼/摩天轮一样直接打字搜（见下方 mtop 段）。
// 仍不能做：票档余量（在选座页，需要登录态）；下单。

const DAMAI_DETAIL = 'https://detail.damai.cn/item.htm';

function damaiHeaders() {
  return {
    'User-Agent': UA,
    Referer: 'https://www.damai.cn/',
    Accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9',
  };
}

/** 从任意大麦链接/文本里抠 itemId。 */
function extractDamaiId(text) {
  const m = String(text || '').match(/(?:id=(\d{6,})|itemId[=:"]+(\d{6,}))/i);
  return (m && (m[1] || m[2])) || '';
}

// ---------------------------------------------------------------------------
// 大麦 mtop 匿名搜索（2026-10 实测打通）
// ---------------------------------------------------------------------------
// 大麦移动端整个并入淘票票壳（m.damai.cn 重定向到 m.taopiaopiao.com），
// 数据全走 m.h5 网关。关键事实：
//   · search.damai.cn/searchajax 老接口对非浏览器 IP 直接 x5sec 风控——死路；
//   · API 名从 show-h5-next 前端包挖出：
//       mtop.damai.wireless.search.search  v1.0 —— 关键词搜索（返回 projectInfo）
//       mtop.damai.wireless.search.discovery.bycity.get v1.0 —— 只回热搜词，热门列表做不了
//   · _m_h5_tk 是网关下发的**匿名**令牌（不是用户凭据），签名 = md5(token&t&appKey&data)，
//     首次请求通常 TOKEN_EMPTY，网关同时种下令牌，重试一次即成功；少数情况下
//     首轮直接回 API_NOT_FOUNDED（网关把无令牌请求路由到了没该 API 的后端），
//     mtopCall 已对这两种情况都做重试，最终都能拿到数据。
// 参数结构抄自搜索页源码：cityId/longitude/latitude/pageIndex/pageSize/keyword/
//   sourceType=11/returnItemOption=4/distanceCityId/option=434。

const MTOP_APPKEY = '12574478';
const MTOP_BASE = 'https://h5api.m.taobao.com/h5';
// 进程级缓存令牌 cookie；云函数实例常驻时复用，冷启动多一次重试
let mtopCookie = '';

function mtopAttempt(api, dataObj, v) {
  const t = Date.now().toString();
  const data = JSON.stringify(dataObj);
  const tkAt = mtopCookie.indexOf('_m_h5_tk=');
  const token = tkAt >= 0 ? mtopCookie.slice(tkAt + 9).split(';')[0].split('_')[0] : '';
  const sign = crypto.createHash('md5').update(`${token}&${t}&${MTOP_APPKEY}&${data}`).digest('hex');
  const headers = {
    'User-Agent': UA,
    Referer: 'https://m.damai.cn/',
    Accept: 'application/json',
  };
  if (mtopCookie) headers.Cookie = mtopCookie;
  return request(
    `${MTOP_BASE}/${api}/${v}/?jsv=2.7.2&appKey=${MTOP_APPKEY}&t=${t}` +
      `&sign=${sign}&api=${api}&v=${v}&type=originaljson&dataType=json&data=${encodeURIComponent(data)}`,
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

async function mtopCall(api, dataObj, v) {
  let j = await mtopAttempt(api, dataObj, v);
  let ret = (j.ret || [])[0] || '';
  // 首轮匿名请求可能回两种情况：
  //   TOKEN_EMPTY/EXPIRED —— 网关首次下发匿名令牌，重试一次即可；
  //   API_NOT_FOUNDED      —— 网关把"无令牌"请求路由到了没有该 API 的后端，
  //                            纯看运气（2026-10 实测时好时坏，连发还会触发
  //                            USER_VALIDATE 风控）。两种都再试：API_NOT_FOUNDED
  //                            时清掉旧令牌重新走签发流程，往往就路由对。最多多试
  //   3 次、每次加点退避，避免瞬间连发把自己风控死。
  let tries = 0;
  while (!/SUCCESS/.test(ret) && /TOKEN_EMPTY|TOKEN_EXPIRED|API_NOT_FOUNDED/.test(ret) && tries < 3) {
    if (/API_NOT_FOUNDED/.test(ret)) mtopCookie = ''; // 丢弃可能错路的匿名令牌，重新签发
    await sleep(200 + tries * 150);
    j = await mtopAttempt(api, dataObj, v);
    ret = (j.ret || [])[0] || '';
    tries++;
  }
  if (!/SUCCESS/.test(ret)) throw new Error('大麦 mtop：' + ret);
  return j.data || {};
}

/** 大麦关键词搜索 → 与 findMaoyan 同构的行结构。 */
async function damaiSearch(keyword, limit) {
  const kw = String(keyword || '').trim();
  if (!kw) throw new Error('搜索词不能为空');
  const data = await mtopCall('mtop.damai.wireless.search.search', {
    cityId: 0, longitude: 0, latitude: 0,
    pageIndex: 1, pageSize: Math.min(Number(limit) || 20, 50),
    keyword: kw, sourceType: 11, returnItemOption: 4,
    distanceCityId: '852', option: 434,
  });
  const rows = Array.isArray(data.projectInfo) ? data.projectInfo : [];
  return rows.map((p) => ({
    performanceId: String(p.id),
    itemId: String(p.id),
    name: p.name || p.ipName || ('大麦 #' + p.id),
    city: p.cityName || '',
    venue: p.venueName || '',
    dateRange: p.showTime || '',
    price: p.priceLow ? '¥' + p.priceLow + ' 起' : '',
    // itemSaleStatus 官方映射无公开文档：1 实测为在售；其余透传数字不猜
    status: p.itemSaleStatus === 1 ? '在售' : (p.itemSaleStatus != null ? '状态' + p.itemSaleStatus : ''),
    poster: p.posterUrl || (Array.isArray(p.videos) && p.videos[0] && p.videos[0].picUrl) || '',
  }));
}

/**
 * 抓大麦详情页，解析 SSR 内嵌 JSON。
 *
 * 数据藏在两个地方（2026-10 实测结构）：
 *   1. <div id="staticDataDefault" style="display: none">{...}</div>
 *   2. 更完整的 __INITIAL_STATE__ / mtop 请求壳
 * 只信 parse 出来的字段，缺就空着——不猜状态映射。
 */
async function pollDamai(params) {
  const itemId = params.item_id || params.id || extractDamaiId(params.url || '');
  if (!itemId) throw new Error('缺少大麦 itemId——把大麦 App 分享出来的链接粘贴进来即可');

  const r = await request(`${DAMAI_DETAIL}?id=${encodeURIComponent(itemId)}`, {
    headers: damaiHeaders(),
    timeout: 20000,
  });
  if (r.status !== 200) throw new Error('大麦详情页 HTTP ' + r.status);
  const html = r.body;
  if (/_____tmd_____\/punish/.test(html)) {
    throw new Error('大麦返回了风控验证页——云 IP 被识别了');
  }

  // 抽出页面上所有 display:none 的 JSON 块，逐个尝试 parse 后合并取字段。
  // 实测（2026-10）有两个块：id="dataDefault"（performBases/buyBtn*/sellStart*）
  // 和 id="staticDataDefault"（itemBase/venue/returnPolicy）——正则要同时罩住
  // data 与 staticData 两种 id 前缀，漏一个就丢一半字段。
  const blobs = [];
  const divRe = /<div id="[^"]*[Dd]ata[^"]*"[^>]*>([\s\S]*?)<\/div>/g;
  let m;
  while ((m = divRe.exec(html)) !== null) {
    blobs.push(m[1]);
  }
  if (!blobs.length) throw new Error('大麦详情页里没找到内嵌数据——页面结构可能变了');

  const merged = {};
  for (const raw of blobs) {
    try {
      Object.assign(merged, JSON.parse(raw.trim()));
    } catch (e) {
      // 解析失败的单块跳过，别中断
    }
  }

  const base = merged.itemBase || {};
  const name = base.itemName || merged.itemName || ('大麦 #' + itemId);
  const city = base.cityName || merged.cityName || '';
  const venue = (merged.venue && merged.venue.venueName) || merged.venueName || '';
  const price = merged.priceRange || '';
  const sellStr = merged.sellStartTimeStr || '';
  const sellMs = merged.sellStartTime || 0;
  // 状态文案用**官方按钮原文**（buyBtnText：立即购买 / 已下架 / 购票通道已关闭…），
  // 不自己编状态表——官方改词我们自动跟着对。projectStatus 的数字映射没有
  // 公开文档，只在 buyBtnText 缺失时兜底透出数字。
  const buyText = String(merged.buyBtnText || '').trim();
  const notYet = sellMs > Date.now();
  const buyable = buyText ? (/购买|预订|抢/.test(buyText) && !/不支持|关闭|已结束|已下架|售罄|缺货/.test(buyText)) : false;
  const statusLabel = buyText || (notYet ? '未开售' : '');

  const perfs = Array.isArray(merged.performBases) ? merged.performBases : [];
  const items = [];
  if (perfs.length) {
    for (const p of perfs.slice(0, 20)) {
      const perStatus = p.performStatus != null ? Number(p.performStatus) : null;
      items.push({
        label: p.performName || p.name || '场次',
        place: venue,
        when: p.performTimeStr || p.showTime || '',
        status: perStatus === 1 ? '可售' : perStatus != null ? '状态' + perStatus : '',
        // 场次级可售判定同样保守：只有明确 1 才算有票
        available: perStatus === 1,
        count: perStatus === 1 ? null : 0,
      });
    }
  } else {
    // 没挂出场次：把项目级信息当一条状态。开售时间是最有用的信号。
    items.push({
      label: name,
      place: venue || city,
      when: sellStr ? ('开售 ' + sellStr) : '',
      status: statusLabel || '详情见大麦',
      available: buyable,
      count: buyable ? null : 0,
    });
  }
  return items.map((it) => Object.assign(it, { price }));
}

/**
 * 大麦入口：链接/纯数字 id → 抓详情页；关键词 → mtop 无线搜索。
 * 和猫眼/摩天轮一样，用户直接打字就能搜——不再逼人去 App 翻链接。
 */
async function findDamai(linkOrId) {
  const raw = String(linkOrId || '').trim();
  if (/https?:\/\//i.test(raw) || /^\d{6,}$/.test(raw)) {
    const id = extractDamaiId(raw) || raw;
    const items = await pollDamai({ item_id: id });
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
  return damaiSearch(raw);
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

/** 关键词搜索（全国范围）→ 与 findMaoyan 同构的行结构。 */
async function findPiaoxingqiu(keyword, limit) {
  const kw = String(keyword || '').trim();
  if (!kw) throw new Error('输入演出名搜索票星球');
  const j = await getJson(
    pxqUrl('/cyy_gatewayapi/home/pub/v3/show_list/search', {
      keyword: kw,
      pageType: 'SEARCH_PAGE',
      offset: 0,
      length: Math.min(Number(limit) || 20, 50),
    }),
    { headers: pxqHeaders(), timeout: 15000 },
  );
  const d = pxqCheck(j, '搜索');
  const rows = Array.isArray(d.searchData) ? d.searchData : [];
  return rows.map((r) => ({
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
async function detailShow(platform, id) {
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

  // 其余平台：扁平 poll 包成单一分组
  const fn = PLATFORMS[platform];
  if (!fn) throw new Error('暂不支持平台 ' + platform);
  const paramKey =
    platform === 'moretickets' ? 'tour_id'
    : platform === 'maoyan' ? 'performance_id'
    : platform === 'damai' ? 'item_id'
    : 'id';
  const items = await fn({ [paramKey]: id });
  const first = items[0] || {};
  return {
    platform, id,
    name: first.label || '',
    venue: first.place || '',
    sessions: [{
      name: first.label || '票档', when: first.when || '', status: first.status || '',
      available: !!first.available,
      tiers: items.map((it) => ({
        name: it.label, price: it.price || '', status: it.status || '', available: !!it.available,
      })),
    }],
  };
}

// ===========================================================================
// 分发
// ===========================================================================

const PLATFORMS = {
  maoyan: pollMaoyan,
  moretickets: pollMoretickets,
  rail12306: pollRail,
  damai: pollDamai,
  bilibili: pollBilibili,
  piaoxingqiu: pollPiaoxingqiu,
};

// 平台中文名：推送消息里的「放票机构」字段用
const LABEL_CN = {
  maoyan: '猫眼', moretickets: '摩天轮', rail12306: '12306',
  damai: '大麦', bilibili: 'B站会员购', piaoxingqiu: '票星球',
};

const PLATFORM_PROBE = {
  maoyan: () => `${MAOYAN_BASE}/ajax/performance/501675?sellChannel=7`,
  moretickets: null, // POST 接口，probe 走真实轮询
  rail12306: () => `${RAIL_BASE}/otn/leftTicket/init`,
  damai: () => `${DAMAI_DETAIL}?id=889142177842`,
  bilibili: () =>
    `${BILI_SHOW}/api/ticket/project/listV2?version=134&platform=web&page=1&pagesize=5&ptype=100104&area=0&keyword=${encodeURIComponent('演唱会')}`,
  piaoxingqiu: () => `${PXQ_M}/cyy_gatewayapi/home/pub/v3/show/recommend`,
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
      const results = [];
      for (const task of tasks || []) {
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
            rec.items = await fn(task.params || {});
            rec.ok = true;
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
      return { ok: true, at: now, tasks: results.length, results: results };
    }

    // --- 搜索（小程序「场次」页用）-------------------------------------
    // 用户只打关键词，我们去平台把候选捞回来。不用让用户去别的 App
    // 翻链接抠 id——那一步 90% 的人会放弃。
    if (action === 'find') {
      const keyword = String((event && event.keyword) || '').trim();
      const limit = (event && event.limit) || 20;
      const platform = String((event && event.platform) || 'moretickets').trim();

      // 空关键词 = 各平台「正在热卖」。实测（2026-10）：
      //   猫眼 k= 直返在售（带海报）；B站 listV2 免 keyword 返推荐位；
      //   票星球 show/recommend 返 40 条热卖榜；大麦/摩天轮没有真正的
      //   热门端点，用「演唱会」关键词兜底（真实数据，不是编的）；
      //   12306 是站站查询，没有热卖榜。
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
        if (platform === 'damai') {
          const rows = await damaiSearch('演唱会', limit);
          return { ok: true, keyword: '', platform, rows };
        }
        if (platform === 'moretickets') {
          const rows = await findMoretickets('演唱会', limit);
          return { ok: true, keyword: '', platform, rows };
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
      // 大麦：链接/纯数字 id → 抓详情页；关键词 → mtop 无线搜索（2026-10 打通）。
      if (platform === 'damai') {
        const rows = await findDamai(keyword);
        return { ok: true, keyword, platform, rows };
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
      return { ok: false, error: '暂不支持平台 ' + platform };
    }

    // --- 演出详情：场次 × 票档 全展开（详情页用）------------------------
    if (action === 'detail') {
      const platform = String((event && event.platform) || '').trim();
      const id = String((event && event.id) || '').trim();
      if (!platform || !id) return { ok: false, error: '缺少 platform 或 id' };
      try {
        const r = await detailShow(platform, id);
        return Object.assign({ ok: true }, r);
      } catch (e) {
        return { ok: false, error: String((e && e.message) || e) };
      }
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
  damaiSearch: damaiSearch,
  mtopCall: mtopCall,
  findBilibili: findBilibili,
  pollBilibili: pollBilibili,
  findPiaoxingqiu: findPiaoxingqiu,
  pollPiaoxingqiu: pollPiaoxingqiu,
  hotPiaoxingqiu: hotPiaoxingqiu,
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
