// 机票：同程「价格日历」通道（免 Cookie、免签名、纯 JSON）
// ===========================================================================
// 为什么是同程（2026-10-29 实测，别再走回头路）：
//   · 携程 flights.ctrip.com 对非浏览器流量直接 432 + "whaleguard block"，
//     移动端 m.ctrip.com 也时通时断（ERR_CONNECTION_CLOSED）；
//   · 去哪儿 m.flight.qunar.com 的 touchInnerList 要 Bella 令牌（JS 加密串），
//     裸 POST 永远回「没有查询到符合条件的航班」；
//   · 同程 /flights/api/getpricecalendar 是明文 JSON POST，匿名零 Cookie 可调，
//     一次给一整条航线 30+ 天的逐日最低价（含航班号 / 起降时刻 / 折扣）。
// 航班列表（含舱位）来自同一个站点的另一条路：机票列表页是 Nuxt 服务端渲染，
// 整页航班数据直接写在 HTML 里 window.__NUXT__.state.book1.flightLists，
// 纯 GET、零 Cookie、零签名就能拿全（2026-10-29 实测 SHA-BJS / CAN-CTU / BJS-SYX
// 三条航线各 10 个航班、19 档舱位）。**别再试 getflightlist**：那个接口匿名恒
// 返回空数组（依赖前端算的 route Cookie），列表数据本来就不在它那儿。
'use strict';

const https = require('https');
const vm = require('vm');

// 国内城市（同程 getpricecalendar 走这里，机场三字码）。
// 国际城市另起一张表：那批走的是「低价日历」，跟国内不是同一个接口，
// 混在一张表里会让 citySearch 把国际码喂给国内接口然后全军覆没。
const FLIGHT_CITIES = [
  { code: 'BJS', name: '北京', py: 'beijing' },
  { code: 'SHA', name: '上海', py: 'shanghai' },
  { code: 'CAN', name: '广州', py: 'guangzhou' },
  { code: 'SZX', name: '深圳', py: 'shenzhen' },
  { code: 'CTU', name: '成都', py: 'chengdu' },
  { code: 'CKG', name: '重庆', py: 'chongqing' },
  { code: 'HGH', name: '杭州', py: 'hangzhou' },
  { code: 'NKG', name: '南京', py: 'nanjing' },
  { code: 'WUH', name: '武汉', py: 'wuhan' },
  { code: 'XIY', name: '西安', py: 'xian' },
  { code: 'CSX', name: '长沙', py: 'changsha' },
  { code: 'CGO', name: '郑州', py: 'zhengzhou' },
  { code: 'TSN', name: '天津', py: 'tianjin' },
  { code: 'TAO', name: '青岛', py: 'qingdao' },
  { code: 'DLC', name: '大连', py: 'dalian' },
  { code: 'SHE', name: '沈阳', py: 'shenyang' },
  { code: 'HRB', name: '哈尔滨', py: 'haerbin' },
  { code: 'CGQ', name: '长春', py: 'changchun' },
  { code: 'TYN', name: '太原', py: 'taiyuan' },
  { code: 'SJW', name: '石家庄', py: 'shijiazhuang' },
  { code: 'HET', name: '呼和浩特', py: 'huhehaote' },
  { code: 'XMN', name: '厦门', py: 'xiamen' },
  { code: 'FOC', name: '福州', py: 'fuzhou' },
  { code: 'NGB', name: '宁波', py: 'ningbo' },
  { code: 'WNZ', name: '温州', py: 'wenzhou' },
  { code: 'HFE', name: '合肥', py: 'hefei' },
  { code: 'KMG', name: '昆明', py: 'kunming' },
  { code: 'KWE', name: '贵阳', py: 'guiyang' },
  { code: 'NNG', name: '南宁', py: 'nanning' },
  { code: 'KWL', name: '桂林', py: 'guilin' },
  { code: 'HAK', name: '海口', py: 'haikou' },
  { code: 'SYX', name: '三亚', py: 'sanya' },
  { code: 'LHW', name: '兰州', py: 'lanzhou' },
  { code: 'INC', name: '银川', py: 'yinchuan' },
  { code: 'XNN', name: '西宁', py: 'xining' },
  { code: 'URC', name: '乌鲁木齐', py: 'wulumuqi' },
  { code: 'LXA', name: '拉萨', py: 'lasa' },
  { code: 'JJN', name: '泉州', py: 'quanzhou' },
  { code: 'SWA', name: '揭阳', py: 'jieyang' },
  { code: 'ZUH', name: '珠海', py: 'zhuhai' },
];

// 国际/地区城市：值面是 IATA **城市**码（不是机场码）。
// 同程低价日历按城市归并（NRT→TYO、PEK→BJS、PVG→SHA），喂机场码也能出数，
// 但同一城市会算出两条不同航线，监控里就成了两份，所以统一收城市码。
const INTL_CITIES = [
  { code: 'HKG', name: '香港', py: 'xianggang', intl: 1 },
  { code: 'MFM', name: '澳门', py: 'aomen', intl: 1 },
  { code: 'TPE', name: '台北', py: 'taibei', intl: 1 },
  { code: 'TYO', name: '东京', py: 'dongjing', intl: 1 },
  { code: 'OSA', name: '大阪', py: 'daban', intl: 1 },
  { code: 'SPK', name: '札幌', py: 'zhahuang', intl: 1 },
  { code: 'FUK', name: '福冈', py: 'fugang', intl: 1 },
  { code: 'OKA', name: '冲绳', py: 'chongsheng', intl: 1 },
  { code: 'SEL', name: '首尔', py: 'shouer', intl: 1 },
  { code: 'PUS', name: '釜山', py: 'fushan', intl: 1 },
  { code: 'BKK', name: '曼谷', py: 'mangu', intl: 1 },
  { code: 'CNX', name: '清迈', py: 'qingmai', intl: 1 },
  { code: 'HKT', name: '普吉', py: 'puji', intl: 1 },
  { code: 'SIN', name: '新加坡', py: 'xinjiapo', intl: 1 },
  { code: 'KUL', name: '吉隆坡', py: 'jilongpo', intl: 1 },
  { code: 'SGN', name: '胡志明', py: 'huzhiming', intl: 1 },
  { code: 'HAN', name: '河内', py: 'henei', intl: 1 },
  { code: 'DPS', name: '巴厘岛', py: 'balidao', intl: 1 },
  { code: 'MNL', name: '马尼拉', py: 'manila', intl: 1 },
  { code: 'DEL', name: '德里', py: 'deli', intl: 1 },
  { code: 'KTM', name: '加德满都', py: 'jiademandu', intl: 1 },
  { code: 'CMB', name: '科伦坡', py: 'kelunpo', intl: 1 },
  { code: 'DXB', name: '迪拜', py: 'dibai', intl: 1 },
  { code: 'DOH', name: '多哈', py: 'duoha', intl: 1 },
  { code: 'IST', name: '伊斯坦布尔', py: 'yisitanbuer', intl: 1 },
  { code: 'MOW', name: '莫斯科', py: 'mosike', intl: 1 },
  { code: 'LON', name: '伦敦', py: 'lundun', intl: 1 },
  { code: 'PAR', name: '巴黎', py: 'bali', intl: 1 },
  { code: 'FRA', name: '法兰克福', py: 'falanqifu', intl: 1 },
  { code: 'AMS', name: '阿姆斯特丹', py: 'amusitedan', intl: 1 },
  { code: 'ROM', name: '罗马', py: 'luoma', intl: 1 },
  { code: 'MAD', name: '马德里', py: 'madeli', intl: 1 },
  { code: 'BCN', name: '巴塞罗那', py: 'basailuona', intl: 1 },
  { code: 'MIL', name: '米兰', py: 'milan', intl: 1 },
  { code: 'MUC', name: '慕尼黑', py: 'munihei', intl: 1 },
  { code: 'ZRH', name: '苏黎世', py: 'sulishi', intl: 1 },
  { code: 'VIE', name: '维也纳', py: 'weiyena', intl: 1 },
  { code: 'NYC', name: '纽约', py: 'niuyue', intl: 1 },
  { code: 'LAX', name: '洛杉矶', py: 'luoshanji', intl: 1 },
  { code: 'SFO', name: '旧金山', py: 'jiujinshan', intl: 1 },
  { code: 'SEA', name: '西雅图', py: 'xiyatu', intl: 1 },
  { code: 'CHI', name: '芝加哥', py: 'zhijiage', intl: 1 },
  { code: 'BOS', name: '波士顿', py: 'boshidun', intl: 1 },
  { code: 'YVR', name: '温哥华', py: 'wengehua', intl: 1 },
  { code: 'YYZ', name: '多伦多', py: 'duolunduo', intl: 1 },
  { code: 'SYD', name: '悉尼', py: 'xini', intl: 1 },
  { code: 'MEL', name: '墨尔本', py: 'moerben', intl: 1 },
  { code: 'AKL', name: '奥克兰', py: 'aokelan', intl: 1 },
  { code: 'HNL', name: '檀香山', py: 'tanxiangshan', intl: 1 },
  { code: 'GUM', name: '关岛', py: 'guandao', intl: 1 },
  { code: 'CAI', name: '开罗', py: 'kailuo', intl: 1 },
  { code: 'JNB', name: '约翰内斯堡', py: 'yuehanneisibao', intl: 1 },
  { code: 'GRU', name: '圣保罗', py: 'shengbaoluo', intl: 1 },
];

const ALL_CITIES = FLIGHT_CITIES.concat(INTL_CITIES);

/** 简单 POST JSON（同程不需要任何签名 / Cookie） */
function postJson(url, payload, headers, timeout) {
  return new Promise((resolve, reject) => {
    const body = Buffer.from(JSON.stringify(payload), 'utf8');
    const req = https.request(
      url,
      {
        method: 'POST',
        headers: Object.assign(
          {
            'Content-Type': 'application/json',
            'Content-Length': body.length,
            Host: 'www.ly.com',
          },
          headers || {},
        ),
        timeout: timeout || 20000,
        rejectUnauthorized: false,
      },
      (res) => {
        let raw = '';
        res.setEncoding('utf8');
        res.on('data', (d) => { raw += d; });
        res.on('end', () => resolve({ status: res.statusCode, body: raw }));
      },
    );
    req.on('timeout', () => req.destroy(new Error('请求超时')));
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

/** 北京时间当天 +offset 天，YYYY-MM-DD */
function bjDate(offset) {
  const t = new Date(Date.now() + (Number(offset) || 0) * 86400000 + 8 * 3600000);
  return t.toISOString().slice(0, 10);
}

/** 日期字符串 +n 天（跨月/跨年由 Date 自己处理） */
function addDays(date, n) {
  const ms = new Date(date + 'T00:00:00+08:00').getTime() + (Number(n) || 0) * 86400000;
  return new Date(ms + 8 * 3600000).toISOString().slice(0, 10);
}

/** 城市检索：三字码 / 中文 / 拼音。空关键词给国内热门 + 几个出境热门。 */
function citySearch(kw) {
  const q = String(kw || '').trim().toLowerCase();
  if (!q) {
    // 空关键词时混几个出境热门，不然用户不知道这里还能查国际线
    const hot = ['TYO', 'HKG', 'SEL', 'BKK'];
    return FLIGHT_CITIES.slice(0, 10).concat(INTL_CITIES.filter((c) => hot.indexOf(c.code) >= 0));
  }
  const hit = ALL_CITIES.filter(
    (c) => c.code.toLowerCase() === q || c.name.indexOf(q) >= 0 || c.py.indexOf(q) >= 0,
  );
  return hit.length ? hit : [];
}

function cityName(code) {
  const c = ALL_CITIES.filter((x) => x.code === String(code || '').toUpperCase())[0];
  return c ? c.name : String(code || '');
}

/** 这个城市码是不是国际/地区（值面是 IATA 城市码而非国内机场码） */
function isIntl(code) {
  const c = String(code || '').toUpperCase();
  return INTL_CITIES.some((x) => x.code === c);
}

/** 一条航线是不是跨境：任一端在国际表里即算 */
function isIntlRoute(dep, arr) {
  return isIntl(dep) || isIntl(arr);
}

/**
 * 一条航线在 [startDate, startDate+days) 内的逐日最低价。
 * 返回 { fromCode,toCode,fromName,toName,rows,stats }
 */
async function calendar(depCode, arrCode, startDate, days) {
  const dep = String(depCode || '').trim().toUpperCase();
  const arr = String(arrCode || '').trim().toUpperCase();
  if (!/^[A-Z]{3}$/.test(dep) || !/^[A-Z]{3}$/.test(arr)) {
    throw new Error('城市代码不对（要三个字母，例如上海=SHA、北京=BJS）');
  }
  if (dep === arr) throw new Error('出发地和到达地不能是一样的');

  const n = Math.max(7, Math.min(60, Number(days) || 30));
  const beg = /^\d{4}-\d{2}-\d{2}$/.test(String(startDate || '')) ? String(startDate) : bjDate(0);
  const begMs = new Date(beg + 'T00:00:00+08:00').getTime();
  const end = new Date(begMs + (n - 1) * 86400000 + 8 * 3600000).toISOString().slice(0, 10);

  const r = await postJson(
    'https://www.ly.com/flights/api/getpricecalendar',
    {
      StartPort: dep,
      EndPort: arr,
      QueryBegDate: beg,
      QueryEndDate: end,
      QueryType: 1,
      travelTypes: [1],
      flat: 1,
      plat: 1,
      isFromKylin: 1,
      refid: '',
    },
    {
      Referer: 'https://www.ly.com/flights/itinerary/oneway/' + dep + '-' + arr,
      Origin: 'https://www.ly.com',
      'User-Agent':
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    },
    25000,
  );

  if (r.status !== 200) throw new Error('同程返回 HTTP ' + r.status);
  let j;
  try {
    j = JSON.parse(r.body);
  } catch (e) {
    throw new Error('同程返回的不是 JSON：' + String(r.body).slice(0, 80));
  }
  if (Number(j.resCode) !== 0) throw new Error(j.resDesc || '同程返回了异常结构');

  const body = j.body || {};
  const list = Array.isArray(body.fzpriceinfos) ? body.fzpriceinfos : [];
  const rows = list
    .filter((x) => x && x.flydate && Number(x.price) > 0)
    .map((x) => ({
      date: x.flydate,
      week: x.week || '',
      price: Number(x.price),
      discount: Number(x.discount) || 0,
      flightNo: x.flightno || '',
      depTime: String(x.flytime || '').slice(11, 16),
      arrTime: String(x.arrtime || '').slice(11, 16),
    }));

  if (!rows.length) {
    throw new Error('这条航线在所选区间没有可售航班（可能超出预售期、或没有直飞）');
  }

  const prices = rows.map((x) => x.price);
  const min = Math.min.apply(null, prices);
  const max = Math.max.apply(null, prices);
  const avg = Math.round(prices.reduce((a, b) => a + b, 0) / prices.length);

  return {
    fromCode: dep,
    toCode: arr,
    fromName: body.startportname || cityName(dep),
    toName: body.endportname || cityName(arr),
    rows: rows,
    stats: {
      min: min,
      max: max,
      avg: avg,
      cheapest: rows.filter((x) => x.price === min)[0] || null,
    },
  };
}

// ===========================================================================
// 多平台低价日历（同程 / 飞猪 / 途牛）—— 免登录、免签名、免 Cookie
// ===========================================================================
// 来源：dengmeiluan/ticket-monitoring（MIT）里 crawlers/special.py 的三条通道，
// 2026-10-30 逐条实测通过（参数、口径、失败码都照抄，别自己改参数）：
//
//   同程  POST https://wx.17u.cn/cheapflights/bffapi/cheapflightapi/lowestprice/queryCalendarLowestPrice
//         · 一次返回整窗口（国内 180 天 / 国际 90 天），含税起价
//         · type/nameType = 1 表示国际城市；channel 固定 4
//         · success=false 且 code=5 → 渠道明确「这条航线没数据」，不是网络失败
//         · **别用 www.ly.com/miflightapi/ts/calendar**：那个要页内 as-sdk 签名，
//           裸请求恒 444「非法链接」，是同程国际长期零数据的老坑
//   飞猪  GET  https://r.fliggy.com/cheapestCalendar/pc?...&callback=cb
//         · JSONP，国内国际同源；price 是裸价，**tax 要另加**
//         · 正常返回里带 result[].url，可以直接给用户点去下单
//   途牛  GET  https://flight-api.tuniu.com/query/flight/v1/calendar?d=<JSON>
//         · 国内才有，国际该接口无日历；price 裸价不含税
//
// 口径差异必须记住，否则「谁最便宜」会算错：
//   同程 = 含税；飞猪 = 裸价 + tax；途牛 = 裸价（无税字段）
// 国内机建燃油相对票价很小，统一按「total」比；国际差别大，宁可标「起价」。

const CAL_UA =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 ' +
  '(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1';

/** GET 文本（JSON / JSONP / HTML 都行），自己带 headers，不共用同程那套 */
function getText(url, headers, timeout) {
  return new Promise((resolve, reject) => {
    const req = https.get(
      url,
      {
        headers: Object.assign({ 'User-Agent': CAL_UA }, headers || {}),
        timeout: timeout || 18000,
        rejectUnauthorized: false,
      },
      (res) => {
        let buf = '';
        res.setEncoding('utf8');
        res.on('data', (d) => { buf += d; });
        res.on('end', () => resolve({ status: res.statusCode, body: buf }));
      },
    );
    req.on('timeout', () => req.destroy(new Error('请求超时')));
    req.on('error', reject);
  });
}

/** 同程低价日历：国内 180 天 / 国际 90 天，一次拿整窗口 */
async function calTongcheng(dep, arr, beg, end) {
  const oint = isIntl(dep) ? 1 : 0;
  const dint = isIntl(arr) ? 1 : 0;
  const r = await postJson(
    'https://wx.17u.cn/cheapflights/bffapi/cheapflightapi/lowestprice/queryCalendarLowestPrice',
    {
      dateInfo: { dateType: 0, start: beg, end: end },
      originList: [{ code: dep, name: cityName(dep), type: oint, nameType: String(oint) }],
      destList: [{ code: arr, name: cityName(arr), type: dint, nameType: String(dint) }],
      channel: 4,
    },
    { Host: 'wx.17u.cn', 'User-Agent': CAL_UA, Referer: 'https://wx.17u.cn/' },
    18000,
  );
  if (r.status !== 200) throw new Error('同程 HTTP ' + r.status);
  let j;
  try {
    j = JSON.parse(r.body);
  } catch (e) {
    throw new Error('同程返回非 JSON');
  }
  if (!j.success) {
    // code 5 = 渠道明确「这航线没数据」，按空表返回而不是抛错
    if (Number(j.code) === 5) return [];
    throw new Error('同程码 ' + j.code + ' ' + (j.message || ''));
  }
  const map = (j.data && j.data.flightCalendarPriceMap) || {};
  let rows = map[dep + '-' + arr];
  if (!rows && Object.keys(map).length) rows = map[Object.keys(map)[0]];
  return (Array.isArray(rows) ? rows : [])
    .filter((x) => x && x.date && Number(x.price) > 0)
    .map((x) => ({ date: x.date, price: Number(x.price), total: Number(x.price) }));
}

/**
 * 飞猪低价日历（JSONP）：**一次只给 7 天**，必须按周步进补齐整个窗口。
 * price 是裸价，tax 另加；正常返回里带可下单深链。
 * MAX_PAGES 是硬闸：飞猪对连续请求敏感，宁可少几天也不要被限流。
 *
 * 实测（2026-10-30）：短时间内连发几轮之后，它会开始对所有请求回
 * `{"success":false}`，持续好几分钟——这是 IP 级限流，不是参数错。
 * 所以加了进程内熔断：连续空窗就安静 5 分钟，别拿用户的钱和时间陪它试。
 */
let fliggyDownUntil = 0;

async function calFliggy(dep, arr, beg, end) {
  if (Date.now() < fliggyDownUntil) {
    throw new Error('飞猪暂时限流，过几分钟再看');
  }
  const MAX_PAGES = 6; // 6 × 7 = 42 天，足够覆盖小程序的 30 天默认窗口
  const seen = {};
  let cursor = beg;
  let pages = 0;
  while (cursor <= end && pages < MAX_PAGES) {
    pages += 1;
    const url =
      'https://r.fliggy.com/cheapestCalendar/pc?bizType=1&depCityCode=' + dep +
      '&arrCityCode=' + arr + '&leaveDate=' + cursor +
      '&calendarType=0&tripType=0&callback=cb';
    let r;
    try {
      r = await getText(url, {
        Referer: 'https://sijipiao.fliggy.com/ie/flight_search_result.htm',
      });
    } catch (e) {
      if (pages === 1) throw e; // 第一页就挂 → 这家当作没出数
      break;
    }
    if (r.status !== 200) {
      if (pages === 1) throw new Error('飞猪 HTTP ' + r.status);
      break;
    }
    const m = String(r.body).match(/^\s*cb\s*\(([\s\S]*)\)\s*;?\s*$/);
    if (!m) {
      if (pages === 1) throw new Error('飞猪返回不是 JSONP');
      break;
    }
    let j;
    try {
      j = JSON.parse(m[1]);
    } catch (e) {
      if (pages === 1) throw new Error('飞猪 JSONP 解析失败');
      break;
    }
    // success:false / 无 result = 这一段窗口它没有（飞猪对「明天」这天常回空），
    // 不是故障，接着翻下一页。只有网络/HTTP 错误才算这家挂了。
    if (!Array.isArray(j.result)) {
      cursor = addDays(cursor, 7);
      if (cursor <= end) await new Promise((s) => setTimeout(s, 250));
      continue;
    }
    j.result.forEach((x) => {
      if (!x || !x.leaveDate) return;
      const p = Number(x.price);
      if (!(p > 0)) return;
      const d = String(x.leaveDate).slice(0, 10);
      const tax = Number(x.tax) || 0;
      seen[d] = {
        date: d,
        price: p,
        total: p + tax,
        tax: tax,
        url: x.url ? 'https:' + String(x.url).replace(/^https?:/, '') : '',
      };
    });
    cursor = addDays(cursor, 7);
    if (cursor <= end) await new Promise((s) => setTimeout(s, 250));
  }
  const out = Object.keys(seen).sort().map((k) => seen[k]);
  if (!out.length) {
    // 整个窗口一个字都没拿到 = 已被限流，安静 5 分钟
    fliggyDownUntil = Date.now() + 5 * 60000;
    throw new Error('飞猪这次没给数据');
  }
  return out;
}

/** 途牛低价日历：只有国内，裸价不含税 */
async function calTuniu(dep, arr, beg) {
  if (isIntlRoute(dep, arr)) return []; // 该接口国际无日历，不浪费一次请求
  const payload = {
    orgCityIataCode: dep,
    dstCityIataCode: arr,
    departureDate: beg,
    backDate: '',
    type: '1',
    serverMonth: 12,
  };
  const r = await getText(
    'https://flight-api.tuniu.com/query/flight/v1/calendar?d=' +
      encodeURIComponent(JSON.stringify(payload)),
    { Accept: 'application/json, text/javascript, */*; q=0.01', Referer: 'https://m.tuniu.com/' },
  );
  if (r.status !== 200) throw new Error('途牛 HTTP ' + r.status);
  let j;
  try {
    j = JSON.parse(r.body);
  } catch (e) {
    throw new Error('途牛返回非 JSON');
  }
  if (!j.success) {
    if (String(j.msg || j.errorMsg || '').indexOf('暂无价格日历') >= 0) return [];
    throw new Error('途牛 ' + (j.msg || j.errorMsg || j.errorCode || ''));
  }
  return (Array.isArray(j.data) ? j.data : [])
    .filter((x) => x && x.date && Number(x.price) > 0)
    .map((x) => ({ date: String(x.date).slice(0, 10), price: Number(x.price), total: Number(x.price) }));
}

/**
 * 一条航线在三家平台上的逐日最低价 + 谁是当天最低。
 *
 * 返回 {
 *   plats: [{ plat, ok, count, err }],
 *   rows:  [{ date, byPlat: {同程:400, 飞猪:380}, min, minPlat, cheapestUrl }],
 *   stats: { min, cheapest, plats: [{plat, min, avg}], intl }
 * }
 * 三家全挂才抛错；只要有一家出数就照常返回（并标出哪家没出数）。
 */
async function calendarMulti(depCode, arrCode, startDate, days) {
  const dep = String(depCode || '').trim().toUpperCase();
  const arr = String(arrCode || '').trim().toUpperCase();
  if (!/^[A-Z]{3}$/.test(dep) || !/^[A-Z]{3}$/.test(arr)) {
    throw new Error('城市代码不对（要三个字母，例如上海=SHA、东京=TYO）');
  }
  if (dep === arr) throw new Error('出发地和到达地不能是一样的');

  const intl = isIntlRoute(dep, arr);
  // 国际窗口只有 90 天，别越界去拿一堆空值
  const n = Math.max(7, Math.min(intl ? 60 : 90, Number(days) || 30));
  const beg = /^\d{4}-\d{2}-\d{2}$/.test(String(startDate || '')) ? String(startDate) : bjDate(1);
  const begMs = new Date(beg + 'T00:00:00+08:00').getTime();
  const end = new Date(begMs + (n - 1) * 86400000 + 8 * 3600000).toISOString().slice(0, 10);

  const tasks = [
    { plat: '同程', fn: () => calTongcheng(dep, arr, beg, end) },
    { plat: '飞猪', fn: () => calFliggy(dep, arr, beg, end) },
    { plat: '途牛', fn: () => calTuniu(dep, arr, beg) },
  ];
  const settled = await Promise.all(
    tasks.map((t) =>
      t.fn().then(
        (rows) => ({ plat: t.plat, ok: true, rows: rows }),
        (e) => ({ plat: t.plat, ok: false, rows: [], err: e.message || '失败' }),
      ),
    ),
  );

  const plats = settled.map((s) => {
    const inWin = s.rows.filter((r) => r.date >= beg && r.date <= end);
    // 这家自己最便宜的那一天。只给一个「最低 ¥330」用户没法判断：
    // 是同程今天 330 还是下周三 330？差一天就可能白跑一趟。
    let best = null;
    for (const r of inWin) if (!best || r.total < best.total) best = r;
    return {
      plat: s.plat,
      ok: s.ok,
      count: inWin.length,
      // span = 这家实际覆盖到多少天。飞猪只给 7 天窗、途牛没有国际线，
      // 不写出来用户会以为「三家都比了」，那是骗人。
      span: inWin.length ? Math.round((new Date(inWin[inWin.length - 1].date) - new Date(inWin[0].date)) / 86400000) + 1 : 0,
      err: s.err || '',
      min: best ? best.total : 0,
      date: best ? best.date : '',
      // 途牛只给裸票价（不含机建+燃油），同程/飞猪是含税价。
      // 三家混排时必须在界面上说清，否则「途牛 ¥300 最便宜」会把人骗过去。
      taxIncluded: s.plat !== '途牛',
    };
  });
  const alive = settled.filter((s) => s.ok && s.rows.length);
  if (!alive.length) {
    const why = settled.map((s) => s.plat + '：' + (s.err || '没数据')).join('；');
    throw new Error(intl ? '三家都没出国际价（' + why + '）' : '三家都没出价（' + why + '）');
  }

  // 日期 → { 平台: 含税价 }
  const byDate = {};
  settled.forEach((s) => {
    s.rows.forEach((r) => {
      // 同程/途牛会无视请求区间、直接回整窗口（180 天），远端日期不裁掉的话
      // 走势图会从「30 天」变成「半年」，前端直接画成一片噪声
      if (!r.date || r.date < beg || r.date > end) return;
      const cell = byDate[r.date] || (byDate[r.date] = { byPlat: {}, url: {} });
      cell.byPlat[s.plat] = r.total;
      if (r.url) cell.url[s.plat] = r.url;
    });
  });

  const rows = Object.keys(byDate)
    .sort()
    .map((d) => {
      const cell = byDate[d];
      const names = Object.keys(cell.byPlat);
      let minPlat = names[0];
      names.forEach((k) => {
        if (cell.byPlat[k] < cell.byPlat[minPlat]) minPlat = k;
      });
      return {
        date: d,
        byPlat: cell.byPlat,
        min: cell.byPlat[minPlat],
        minPlat: minPlat,
        url: cell.url[minPlat] || '',
      };
    });

  if (!rows.length) throw new Error('这三家在所选区间都没有报价（可能超出预售期）');

  const min = Math.min.apply(null, rows.map((r) => r.min));
  const cheapest = rows.filter((r) => r.min === min)[0];
  const avg = Math.round(rows.reduce((a, r) => a + r.min, 0) / rows.length);
  return {
    fromCode: dep,
    toCode: arr,
    fromName: cityName(dep),
    toName: cityName(arr),
    intl: intl,
    plats: plats,
    rows: rows,
    stats: {
      min: min,
      max: Math.max.apply(null, rows.map((r) => r.min)),
      avg: avg,
      cheapest: cheapest,
    },
  };
}


// ===========================================================================
// 航班列表 + 舱位：同程机票列表页的 Nuxt SSR 内嵌状态
// ===========================================================================
// HTML 里有一段 `window.__NUXT__=(function(a,b,...){...}(...))`，执行后得到
//   state.book1.flightLists = [ { flightNo, airCompanyName, equipmentName,
//     flyOffOnlyTime, arrivalOnlyTime, originAirportShortName, offPoint,
//     arriveAirportShortName, boardPoint, spantime, stopNum, mfgd,
//     lcp(最低价), lcd(最低价描述), sts(准点率/机龄/廊桥率), cfts(舱位数组) } ]
// cfts 每项：{ rd:'4.9折经济舱', iv:'A'(舱位代码), cp:票价, adr:'4.9'折, bp:机建燃油 }
// 只执行这一小段数据表达式，sandbox 里没有任何宿主能力。

const FLIGHT_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';

/** GET 一个 HTML 页面（同程列表页不需要任何凭证） */
function getHtml(url, timeout) {
  return new Promise((resolve, reject) => {
    const req = https.get(
      url,
      {
        headers: {
          'User-Agent': FLIGHT_UA,
          Accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
          'Accept-Language': 'zh-CN,zh;q=0.9',
          Referer: 'https://www.ly.com/flights/',
        },
        timeout: timeout || 25000,
        rejectUnauthorized: false,
      },
      (res) => {
        if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
          res.resume();
          resolve(getHtml(res.headers.location, timeout));
          return;
        }
        let buf = '';
        res.setEncoding('utf8');
        res.on('data', (d) => {
          buf += d;
        });
        res.on('end', () => resolve({ status: res.statusCode, html: buf }));
      },
    );
    req.on('timeout', () => req.destroy(new Error('同程响应超时')));
    req.on('error', reject);
  });
}

/** 从 HTML 里把 window.__NUXT__ 那段表达式取出来跑一遍（只是纯数据 IIFE） */
function parseNuxtState(html) {
  const KEY = 'window.__NUXT__=';
  const i = html.indexOf(KEY);
  if (i < 0) return null;
  let expr = html.slice(i + KEY.length);
  const e = expr.indexOf('</script>');
  if (e > 0) expr = expr.slice(0, e);
  const box = { window: {} };
  try {
    vm.runInNewContext(KEY + expr, box, { timeout: 8000 });
  } catch (err) {
    return null;
  }
  return box.window.__NUXT__ || null;
}

/** 舱位归类：头等舱 / 公务舱(商务舱) / 超级经济舱 / 经济舱 */
function cabinClassOf(name) {
  const t = String(name || '');
  if (t.indexOf('头等') >= 0) return '头等舱';
  if (t.indexOf('公务') >= 0 || t.indexOf('商务') >= 0) return '公务舱';
  if (t.indexOf('超级经济') >= 0 || t.indexOf('高端经济') >= 0) return '超级经济舱';
  if (t.indexOf('经济') >= 0) return '经济舱';
  // 海航系常把最低价舱写成「2.4折超值折扣」，名字里没有舱位词 → 返回空，交给价格兜底
  return '';
}

/** 把同程的原始航班对象裁成小程序要的精简结构 */
function shapeFlight(f) {
  const num = (v) => {
    const n = Number(v);
    return isNaN(n) ? 0 : n;
  };
  const cabins = (f.cfts || [])
    .map((c) => {
      const price = num(c.cp) || num(c.ap);
      return {
        name: String(c.rd || '').trim(),
        cls: cabinClassOf(c.rd, c.iv),
        code: String(c.iv || '').trim(),
        price: price,
        discount: String(c.adr || '').trim(),
        tax: num(c.bp),
      };
    })
    .filter((c) => c.price > 0)
    .sort((a, b) => a.price - b.price);

  // 舱位名里没写「经济/公务」的（如「2.4折超值折扣」），按价格兜底：
  // 最低那档算经济舱，贵到 1.8 倍以上的算公务舱。
  const floor = cabins.length ? cabins[0].price : 0;
  cabins.forEach((c) => {
    if (!c.cls) c.cls = floor && c.price >= floor * 1.8 ? '公务舱' : '经济舱';
  });

  const sts = f.sts || {};
  // low = 这班「现在真能买到的最低价」。别再直接用 lcp：
  // 同程的 lcp 是**原价口径**（对应 cfts.ap），不是实售价（cfts.cp）。
  // 2026-11-08 实测 CZ3101：lcp=2060、cp=1530，卡片上就会一边写「同程 ¥2060」
  // 一边写「经济舱 ¥1530」，看着像两个不同的价。优先取舱位实售价。
  const low = cabins.length ? cabins[0].price : num(f.lcp);

  return {
    no: String(f.flightNo || '').trim(),
    // 比价的「平台」维度。现在只有同程能匿名取到全量航班+舱位，
    // 所以固定一个；将来接了第二家，这里改成把多渠道结果 merge 成 offers 数组。
    plat: '同程',
    air: String(f.airCompanyName || '').trim(),
    airCode: String(f.airCompanyCode || '').trim(),
    plane: String(f.equipmentName || '').trim(),
    dep: String(f.flyOffOnlyTime || '').slice(0, 5),
    arr: String(f.arrivalOnlyTime || '').slice(0, 5),
    depAirport: String(f.originAirportShortName || '').trim(),
    depTerminal: String(f.offPoint || '').trim(),
    arrAirport: String(f.arriveAirportShortName || '').trim(),
    arrTerminal: String(f.boardPoint || '').trim(),
    span: String(f.spantime || '').trim(),
    stops: num(f.stopNum),
    meal: String(f.mfgd || '').trim(),
    ontime: String(sts.fRate || '').trim(),
    age: String(sts.fYear || '').trim(),
    wifi: String(sts.wifi || '').trim(),
    low: low,
    lowDesc: String(f.lcd || '').trim(),
    cabins: cabins,
  };
}

/**
 * 某条航线在某一天的**全部航班 + 每个航班的多档舱位**。
 * 返回 { fromCode,toCode,fromName,toName,date,flights:[...],stats:{count,min,max} }
 */
async function bookList(depCode, arrCode, date) {
  const dep = String(depCode || '').trim().toUpperCase();
  const arr = String(arrCode || '').trim().toUpperCase();
  if (!dep || !arr) throw new Error('要选出发城市和到达城市');
  if (dep === arr) throw new Error('出发地和到达地不能是一样的');

  const today = bjDate(0);
  let day = /^\d{4}-\d{2}-\d{2}$/.test(String(date || '')) ? String(date) : today;
  if (day < today) day = today;

  const url =
    'https://www.ly.com/flights/itinerary/oneway/' + dep + '-' + arr + '?date=' + day;
  const r = await getHtml(url, 25000);
  if (r.status !== 200) throw new Error('同程返回 HTTP ' + r.status);

  const nx = parseNuxtState(r.html);
  if (!nx) {
    throw new Error('页面结构变了（没找到航班数据），先换个日期再试');
  }
  const book = (nx.state && nx.state.book1) || {};
  const raw = Array.isArray(book.flightLists) ? book.flightLists : [];
  const flights = raw
    .map(shapeFlight)
    .filter((f) => f.no && f.dep)
    .sort((a, b) => String(a.dep).localeCompare(String(b.dep)));

  if (!flights.length) {
    throw new Error('这一天没查到直飞航班（可能没直飞，或还没放票）。换个日期看看');
  }

  const prices = flights.map((f) => f.low).filter((p) => p > 0);
  const min = prices.length ? Math.min.apply(null, prices) : 0;
  const max = prices.length ? Math.max.apply(null, prices) : 0;

  return {
    fromCode: dep,
    toCode: arr,
    fromName: cityName(dep),
    toName: cityName(arr),
    date: day,
    flights: flights,
    stats: { count: flights.length, min: min, max: max },
  };
}

module.exports = {
  FLIGHT_CITIES,
  INTL_CITIES,
  ALL_CITIES,
  citySearch,
  cityName,
  isIntl,
  isIntlRoute,
  calendar,
  calendarMulti,
  bookList,
  bjDate,
};
