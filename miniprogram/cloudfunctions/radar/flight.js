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

// 只放国内城市：同程的 getpricecalendar 国际线要登录（QueryType=2 直接回
// "EndPort is invalid"，QueryType=3 回「请登录」），留着只会让用户点出一堆死路。
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

/** 城市检索：三字码 / 中文 / 拼音。空关键词给热门前 12 个。 */
function citySearch(kw) {
  const q = String(kw || '').trim().toLowerCase();
  if (!q) return FLIGHT_CITIES.slice(0, 12);
  const hit = FLIGHT_CITIES.filter(
    (c) => c.code.toLowerCase() === q || c.name.indexOf(q) >= 0 || c.py.indexOf(q) >= 0,
  );
  return hit.length ? hit : [];
}

function cityName(code) {
  const c = FLIGHT_CITIES.filter((x) => x.code === String(code || '').toUpperCase())[0];
  return c ? c.name : String(code || '');
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
  const low = num(f.lcp) || (cabins.length ? cabins[0].price : 0);

  return {
    no: String(f.flightNo || '').trim(),
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
  citySearch,
  cityName,
  calendar,
  bookList,
  bjDate,
};
