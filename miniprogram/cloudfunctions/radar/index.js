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
 * 明确不做的事：不下单、不破签名、不带任何 Cookie。
 * 大麦这类需要登录态的平台这里接不了——Cookie 存云上等于把用户凭据
 * 放到第三方环境，既不安全也没必要。12306 / 猫眼 / 摩天轮都是匿名可用的。
 */

'use strict';

const https = require('https');
const http = require('http');

const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/124.0 Safari/537.36';

const DB_TASKS = 'radar_tasks';
const DB_STATE = 'radar_state';
const DB_POSTS = 'radar_posts';
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
// 平台：大麦（PC 详情页是 SSR，内嵌 JSON 匿名可读——2026-10 实测）
// ===========================================================================
//
// 能做什么：用户提供 itemId（从大麦 App/网页分享链接里提取），
//   轮询「项目级状态」：开售时间 / 价格范围 / 场次列表（performBases）。
// 不能做什么：关键词搜索（search.damai.cn 对云 IP 直接回风控验证页，
//   2026-10 实测 x5secdata punish）；票档余量（在选座页，需要登录态）。

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
 * 大麦「搜索」：只能搜用户粘贴的链接——关键词搜索被风控，不做假。
 * 返回结构和 findMaoyan 一致，前端渲染不用分叉。
 */
async function findDamai(linkOrId) {
  const id = extractDamaiId(linkOrId);
  if (!id) {
    throw new Error(
      '识别不了这个大麦链接。请到大麦 App 打开该演出 → 分享 → 复制链接，' +
        '粘贴进来（链接里要有 id=数字）',
    );
  }
  // 复用 pollDamai 拿状态，包一层成搜索行
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

// ===========================================================================
// 平台：票星球（接口未完全打通——适配器先走通链路，部署后 probe 实测）
// ===========================================================================
//
// 2026-10 查证：本机直连/代理都连不上 www.piaoxingqiu.com（疑似对非国内
// 常规 IP 段有限制），公开文档也没有。票星球 Web 端是 SPA，数据走
// api.piaoxingqiu.com。这里先按最可能的详情接口写，跑不通时把原始错误
// 原样报给用户——宁可「暂未打通」也不能做假状态。

const PXQ_BASE = 'https://www.piaoxingqiu.com';
const PXQ_API = 'https://api.piaoxingqiu.com';

function pxqHeaders() {
  return {
    'User-Agent': UA,
    Referer: PXQ_BASE + '/',
    Accept: 'application/json, text/plain, */*',
    'Content-Type': 'application/json',
  };
}

/** 票星球链接形如 /show/{id}，也有分享短链。 */
function extractPxqId(text) {
  const m = String(text || '').match(/piaoxingqiu\.com\/show\/([a-zA-Z0-9-]+)/i);
  return (m && m[1]) || '';
}

async function pollPiaoxingqiu(params) {
  const showId = params.show_id || params.id || extractPxqId(params.url || '');
  if (!showId) throw new Error('缺少票星球 show_id——把票星球小程序/网页分享的演出链接粘贴进来');

  // 逐个试已知可能的接口路径；全部失败时给出统一的「未打通」报错
  const attempts = [
    `${PXQ_API}/api/v1/web/content/item/${encodeURIComponent(showId)}`,
    `${PXQ_BASE}/show/${encodeURIComponent(showId)}`,
  ];
  const errors = [];
  for (const url of attempts) {
    try {
      const r = await request(url, { headers: pxqHeaders(), timeout: 15000 });
      if (r.status !== 200) {
        errors.push(url + ' → HTTP ' + r.status);
        continue;
      }
      let data;
      try {
        data = JSON.parse(r.body);
      } catch (e) {
        errors.push(url + ' → 返回了 HTML/非 JSON（可能是风控页）');
        continue;
      }
      // 命中了就尽力解析；字段名没有官方文档，只取确定的常用字段
      const d = data.data || data;
      const name = d.name || d.showName || ('票星球 #' + showId);
      const saleStatus = d.saleStatus != null ? String(d.saleStatus) : '';
      return [
        {
          label: name,
          place: d.venueName || d.cityName || '',
          when: d.showTimeStr || d.showTime || '',
          status: saleStatus || '状态未知',
          available: saleStatus === '1' || saleStatus === 'ON_SALE',
          count: saleStatus === '1' ? null : 0,
          price: d.priceRange || d.minPrice || '',
        },
      ];
    } catch (e) {
      errors.push(url + ' → ' + String((e && e.message) || e));
    }
  }
  throw new Error(
    '票星球接口暂未打通（接口无公开文档，且本平台网络限制较多）。\n尝试记录：' +
      errors.join('；'),
  );
}

/** 票星球「搜索」同大麦：只支持粘贴链接。 */
async function findPiaoxingqiu(linkOrId) {
  const id = extractPxqId(linkOrId) || String(linkOrId || '').trim();
  if (!id) throw new Error('请粘贴票星球演出链接（形如 .../show/xxxx）');
  const items = await pollPiaoxingqiu({ show_id: id });
  const first = items[0] || {};
  return [
    {
      performanceId: id,
      showId: id,
      name: first.label || '票星球 #' + id,
      city: '',
      venue: first.place || '',
      dateRange: first.when || '',
      price: first.price || '',
      status: first.status || '',
      poster: '',
    },
  ];
}

// ===========================================================================
// 分发
// ===========================================================================

const PLATFORMS = {
  maoyan: pollMaoyan,
  moretickets: pollMoretickets,
  rail12306: pollRail,
  damai: pollDamai,
  piaoxingqiu: pollPiaoxingqiu,
};

const PLATFORM_PROBE = {
  maoyan: () => `${MAOYAN_BASE}/ajax/performance/501675?sellChannel=7`,
  moretickets: null, // POST 接口，probe 走真实轮询
  rail12306: () => `${RAIL_BASE}/otn/leftTicket/init`,
  damai: () => `${DAMAI_DETAIL}?id=889142177842`,
  piaoxingqiu: () => `${PXQ_API}/api/v1/web/content/index`,
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
// 入口
// ===========================================================================

exports.main = async (event) => {
  const action = (event && event.action) || 'query';
  const db = cloud.database();

  try {
    // --- 部署后的第一件事：确认云 IP 能不能抓到数据 -------------------
    if (action === 'probe') {
      const out = { checked_at: new Date().toISOString(), platforms: {} };
      for (const name of Object.keys(PLATFORM_PROBE)) {
        const url = PLATFORM_PROBE[name];
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
      const { data: tasks } = await db.collection(DB_TASKS).where({ enabled: true }).get();
      const now = new Date().toISOString();
      const results = [];
      for (const task of tasks || []) {
        const fn = PLATFORMS[task.adapter];
        const rec = {
          _id: task._id, platform: task.adapter, name: task.name || task._id,
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
        // 只保留最近 30 次的有票数量，画曲线用
        let history = rec.history || [];
        if (rec.ok) history = history.concat([{ t: now, n: rec.available || 0 }]).slice(-30);
        rec.history = history;

        await db.collection(DB_STATE).doc(task._id).set({ data: rec });
        results.push({ _id: rec._id, ok: rec.ok, n: (rec.items || []).length, error: rec.error });
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

      // 空关键词 = 热门列表。实测（2026-10）只有猫眼支持：k= 直接返回
      // 在售演出（带海报）。摩天轮空关键词 403、大麦风控、票星球无接口
      // ——拿不到就明说，不造假列表。
      if (!keyword) {
        if (platform === 'maoyan') {
          const rows = await findMaoyan('', limit);
          return { ok: true, keyword: '', platform, rows };
        }
        return { ok: false, error: '该平台没有热门列表，请输入关键词搜索' };
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
      // 大麦/票星球没有可用的关键词搜索（大麦被风控、票星球无公开接口），
      // keyword 参数此时承载的是「用户粘贴的链接」，能抠出 id 就返回单行
      if (platform === 'damai') {
        const rows = await findDamai(keyword);
        return { ok: true, keyword, platform, rows };
      }
      if (platform === 'piaoxingqiu') {
        const rows = await findPiaoxingqiu(keyword);
        return { ok: true, keyword, platform, rows };
      }
      return { ok: false, error: '暂不支持平台 ' + platform };
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
};
