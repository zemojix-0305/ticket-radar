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
const TICKET_STATUS_LABELS = {
  1: '即将开售', 2: '预售', 3: '在售中', 4: '已售罄',
  5: '已结束', 11: '即将预售', 12: '演出延期',
};
//: 只有这两种算「能买到」。注意「即将开售」不算——那时候还买不了。
const TICKET_AVAILABLE = ['预售', '在售中'];

function maoyanHeaders() {
  return {
    'User-Agent': UA,
    Referer: 'https://show.maoyan.com/',
    Accept: 'application/json, text/plain, */*',
  };
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
  const label = TICKET_STATUS_LABELS[Number(p.ticketStatus)];
  if (!label) {
    // 不猜：状态表对不上就说明平台改了映射，安静地当成「没票」会误报
    throw new Error(
      '猫眼详情的 ticketStatus=' + JSON.stringify(p.ticketStatus) + ' 看不懂，平台可能改了状态映射表',
    );
  }
  // stockOut 是**场次级**的缺货标记，ticketStatus 是**项目级**的。
  // 巡演横跨十几个城市，某个城市卖完了项目级依然是「在售中」——
  // 所以冲突时以 stockOut 为准。跟 Python 侧同一套判断，别只改一边。
  const conflict = p.stockOut === true && TICKET_AVAILABLE.indexOf(label) >= 0;
  const available = TICKET_AVAILABLE.indexOf(label) >= 0 && !conflict;
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

async function pollRail(params) {
  const { from, to, date, seat_types: seatTypes } = params;
  if (!from || !to || !date) throw new Error('缺少 from / to / date');

  const cookie = await warmUpRail();
  const q =
    'leftTicketDTO.train_date=' + encodeURIComponent(date) +
    '&leftTicketDTO.from_station=' + encodeURIComponent(from) +
    '&leftTicketDTO.to_station=' + encodeURIComponent(to) +
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

  const stationMap = await fetchStationMap();
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
// 分发
// ===========================================================================

const PLATFORMS = {
  maoyan: pollMaoyan,
  moretickets: pollMoretickets,
  rail12306: pollRail,
};

const PLATFORM_PROBE = {
  maoyan: () => `${MAOYAN_BASE}/ajax/performance/501675?sellChannel=7`,
  moretickets: null, // POST 接口，probe 走真实轮询
  rail12306: () => `${RAIL_BASE}/otn/leftTicket/init`,
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
      if (!keyword) return { ok: false, error: '关键词是空的' };
      const rows = await findMoretickets(keyword, limit);
      return { ok: true, keyword, rows };
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
  fetchStationMap: fetchStationMap,
  errorKind: errorKind,
};
