// 1) dump 大麦搜索完整结构 2) 试大麦空关键词/发现页 3) 纷玩岛免登录探测
const https = require('https');
const crypto = require('crypto');
const APPKEY = '12574478';
const DM_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.53(0x18003531) NetType/WIFI Language/zh_CN';
const FWD_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/98.0.4758.102 Safari/537.36 MicroMessenger/6.8.0(0x16080000) NetType/WIFI MiniProgramEnv/Mac MacWechat/WMPF XWEB/30817';

function get(url, headers) {
  return new Promise((resolve, reject) => {
    const r = https.request(url, { method: 'GET', headers, timeout: 15000, rejectUnauthorized: false }, (res) => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', (d) => { body += d; });
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body }));
    });
    r.on('timeout', () => r.destroy(new Error('超时')));
    r.on('error', reject);
    r.end();
  });
}

let tkCookie = '';
async function mtop(api, v, dataObj, extra) {
  const t = Date.now().toString();
  const data = JSON.stringify(dataObj);
  const tkAt = tkCookie.indexOf('_m_h5_tk=');
  const token = tkAt >= 0 ? tkCookie.slice(tkAt + 9).split(';')[0].split('_')[0] : '';
  const sign = crypto.createHash('md5').update(`${token}&${t}&${APPKEY}&${data}`).digest('hex');
  const headers = { 'User-Agent': DM_UA, Accept: 'application/json', Referer: 'https://servicewechat.com/wx938b41d0d7e8def0/350/page-frame.html' };
  if (tkCookie) headers.Cookie = tkCookie;
  const url = `https://mtop.damai.cn/h5/${api}/${v}/?jsv=2.4.12&appKey=${APPKEY}&t=${t}&sign=${sign}&api=${api}&v=${v}&type=originaljson&dataType=json&data=${encodeURIComponent(data)}` + (extra || '');
  const r = await get(url, headers);
  const sc = r.headers['set-cookie'];
  if (sc) tkCookie = sc.map((c) => c.split(';')[0]).join('; ');
  try { return JSON.parse(r.body); } catch (e) { return { parseError: r.body.slice(0, 200) }; }
}

async function mtopOk(api, v, dataObj, extra) {
  let j = await mtop(api, v, dataObj, extra);
  if (j.ret && /TOKEN_EMPTY|TOKEN_EXPIRED|API_NOT_FOUNDED/.test(j.ret[0])) {
    await new Promise((s) => setTimeout(s, 300));
    j = await mtop(api, v, dataObj, extra);
  }
  return j;
}

(async () => {
  console.log('===== 大麦搜索：完整结构 =====');
  let j = await mtopOk('mtop.damai.wireless.search.search', '1.0', {
    cityId: 0, longitude: 0, latitude: 0, pageIndex: 1, pageSize: 3,
    keyword: '林忆莲', sourceType: 11, returnItemOption: 4, distanceCityId: '852', option: 434,
  });
  console.log('ret:', (j.ret || [])[0]);
  if (j.data) {
    console.log('data keys:', Object.keys(j.data).join(','));
    const dv = j.data.dataValue;
    const inner = typeof dv === 'string' ? JSON.parse(dv) : dv;
    console.log('dataValue keys:', Object.keys(inner || {}).join(','));
    const rv = inner && inner.resultValue;
    if (rv) {
      console.log('resultValue keys:', Object.keys(rv).join(','));
      const rows = rv.result || [];
      console.log('rows:', rows.length);
      if (rows[0]) {
        console.log('row[0] keys:', Object.keys(rows[0]).join(','));
        console.log('row[0] sample:', JSON.stringify(rows[0]).slice(0, 500));
      }
    }
  }

  console.log('===== 大麦空关键词（能否当热卖榜） =====');
  let h = await mtopOk('mtop.damai.wireless.search.search', '1.0', {
    cityId: 0, longitude: 0, latitude: 0, pageIndex: 1, pageSize: 5,
    keyword: '演唱会', sourceType: 11, returnItemOption: 4, distanceCityId: '852', option: 434,
  });
  console.log('ret:', (h.ret || [])[0]);
  if (h.data && h.data.dataValue) {
    const inner2 = typeof h.data.dataValue === 'string' ? JSON.parse(h.data.dataValue) : h.data.dataValue;
    const rows2 = (inner2.resultValue && inner2.resultValue.result) || [];
    console.log('rows:', rows2.length, '| 首条:', rows2[0] && rows2[0].name);
  }

  console.log('===== 纷玩岛：无鉴权 / 换鉴权探测 =====');
  const fh = { 'User-Agent': FWD_UA, Accept: '*/*', 'Content-Type': 'application/json', 'Accept-Language': 'zh-CN,zh', Referer: 'https://servicewechat.com/wx5a8f481d967649eb/70/page-frame.html' };
  // 无 authorization
  let r = await get('https://api.livelab.com.cn/performance/app/project/get_performs?project_id=1073&v=' + Date.now() + '&retry=false', fh);
  console.log('get_performs 无auth:', r.status, r.body.slice(0, 120).replace(/\n/g, ' '));
  // 带 auth
  r = await get('https://api.livelab.com.cn/performance/app/project/get_performs?project_id=1073&v=' + Date.now() + '&retry=false', Object.assign({}, fh, { authorization: 'Bearer eyJhbGciOiJIUzUxMiJ9.eyJjdCI6MTcxMzI1MTY0Nzg1NCwibWlkIjoxNzU4ODA4NTcxMzc3NDI2MzMzLCJ0eXBlIjoiYXBwIiwiZGlkIjoiZDVhNGUwMDg0ODc0NTY4OCJ9.Q1ltCrRG48TfjAWaAnfuLHo0oX2NQKsSMNalffwncztBjYVb38LnbLYOCgXhf7hK5vGOHy_xf11qBe5Xk9hqhA' }));
  console.log('get_performs 带旧auth:', r.status, r.body.slice(0, 150).replace(/\n/g, ' '));
})();
