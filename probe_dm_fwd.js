// 探测大麦 mtop.damai.cn 网关 & 纷玩岛 livelab API
const https = require('https');
const crypto = require('crypto');
const APPKEY = '12574478';
const DM_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.53(0x18003531) NetType/WIFI Language/zh_CN';
const FWD_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/98.0.4758.102 Safari/537.36 MicroMessenger/6.8.0(0x16080000) NetType/WIFI MiniProgramEnv/Mac MacWechat/WMPF XWEB/30817';
const FWD_AUTH = 'Bearer eyJhbGciOiJIUzUxMiJ9.eyJjdCI6MTcxMzI1MTY0Nzg1NCwibWlkIjoxNzU4ODA4NTcxMzc3NDI2MzMzLCJ0eXBlIjoiYXBwIiwiZGlkIjoiZDVhNGUwMDg0ODc0NTY4OCJ9.Q1ltCrRG48TfjAWaAnfuLHo0oX2NQKsSMNalffwncztBjYVb38LnbLYOCgXhf7hK5vGOHy_xf11qBe5Xk9hqhA';

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

// mtop 签名调用（带令牌自动重试）
let tkCookie = '';
async function mtop(host, api, v, dataObj, extra) {
  const t = Date.now().toString();
  const data = JSON.stringify(dataObj);
  const tkAt = tkCookie.indexOf('_m_h5_tk=');
  const token = tkAt >= 0 ? tkCookie.slice(tkAt + 9).split(';')[0].split('_')[0] : '';
  const sign = crypto.createHash('md5').update(`${token}&${t}&${APPKEY}&${data}`).digest('hex');
  const headers = {
    'User-Agent': DM_UA,
    Accept: 'application/json',
    Referer: 'https://servicewechat.com/wx938b41d0d7e8def0/350/page-frame.html',
  };
  if (tkCookie) headers.Cookie = tkCookie;
  const qs = `jsv=2.4.12&appKey=${APPKEY}&t=${t}&sign=${sign}&api=${api}&v=${v}&type=originaljson&dataType=json&data=${encodeURIComponent(data)}` + (extra || '');
  const url = `https://${host}/h5/${api}/${v}/?` + qs;
  const r = await get(url, headers);
  const sc = r.headers['set-cookie'];
  if (sc) tkCookie = sc.map((c) => c.split(';')[0]).join('; ');
  let j;
  try { j = JSON.parse(r.body); } catch (e) { return { parseError: r.body.slice(0, 120) }; }
  return j;
}

(async () => {
  console.log('===== A. 大麦 mtop.damai.cn =====');
  // A1: 搜索 API
  let j = await mtop('mtop.damai.cn', 'mtop.damai.wireless.search.search', '1.0', {
    cityId: 0, longitude: 0, latitude: 0, pageIndex: 1, pageSize: 3,
    keyword: '林忆莲', sourceType: 11, returnItemOption: 4,
    distanceCityId: '852', option: 434,
  });
  console.log('search try1:', JSON.stringify((j.ret || [])[0] || j.parseError || Object.keys(j)));
  if (j.ret && !/SUCCESS/.test(j.ret[0])) {
    j = await mtop('mtop.damai.cn', 'mtop.damai.wireless.search.search', '1.0', {
      cityId: 0, longitude: 0, latitude: 0, pageIndex: 1, pageSize: 3,
      keyword: '林忆莲', sourceType: 11, returnItemOption: 4,
      distanceCityId: '852', option: 434,
    });
    console.log('search try2:', JSON.stringify((j.ret || [])[0] || j.parseError || Object.keys(j)));
  }
  if (j.data && j.data.dataValue) {
    // 搜索结果一般包在 dataValue 里
    const inner = typeof j.data.dataValue === 'string' ? JSON.parse(j.data.dataValue) : j.data.dataValue;
    const rows = (inner.resultValue && inner.resultValue.result) || inner.projectInfo || [];
    const first = rows[0] || {};
    console.log('search rows:', rows.length, '| first:', first.name, '| id:', first.id || (first.projectInfo && first.projectInfo.id));
  }

  // A2: detail API（用一个已知大麦 itemId；先随便搜到的或经典测试 id）
  const testId = (j.data && (() => {
    try {
      const inner = typeof j.data.dataValue === 'string' ? JSON.parse(j.data.dataValue) : j.data.dataValue;
      const rows = (inner.resultValue && inner.resultValue.result) || inner.projectInfo || [];
      return String((rows[0] && (rows[0].id || rows[0].projectId)) || '');
    } catch (e) { return ''; }
  })()) || '852534623942';
  console.log('--- detail itemId:', testId);
  let d = await mtop('mtop.damai.cn', 'mtop.damai.item.detail.getdetail', '1.0', {
    dmChannel: 'damai@weixin_weapp', itemId: testId, lat: 39.93722786511198, lng: 116.43680040115234,
  }, '&type=json&AntiCreep=true&AntiFlood=true&env=m&valueType=string');
  console.log('detail try1:', JSON.stringify((d.ret || [])[0] || d.parseError || Object.keys(d)));
  if (d.ret && !/SUCCESS/.test(d.ret[0])) {
    d = await mtop('mtop.damai.cn', 'mtop.damai.item.detail.getdetail', '1.0', {
      dmChannel: 'damai@weixin_weapp', itemId: testId, lat: 39.93722786511198, lng: 116.43680040115234,
    }, '&type=json&AntiCreep=true&AntiFlood=true&env=m&valueType=string');
    console.log('detail try2:', JSON.stringify((d.ret || [])[0] || d.parseError || Object.keys(d)));
  }
  if (d.data) {
    console.log('detail data keys:', Object.keys(d.data).join(','));
    const legacy = typeof d.data.legacy === 'string' ? JSON.parse(d.data.legacy) : d.data.legacy;
    if (legacy) {
      const item = legacy.detailViewComponentMap && legacy.detailViewComponentMap.item;
      if (item && item.item) {
        const pbs = item.item.performBases || [];
        console.log('performBases:', pbs.length, '| 首场:', pbs[0] && pbs[0].performs && pbs[0].performs[0] && pbs[0].performs[0].performId, pbs[0] && pbs[0].performs && pbs[0].performs[0] && pbs[0].performs[0].performName);
        const sid = pbs[0] && pbs[0].performs && pbs[0].performs[0] && pbs[0].performs[0].performId;
        if (sid) {
          // A3: sku（价位级）
          const skuData = { itemId: testId, bizCode: 'ali.china.damai', scenario: 'itemsku', exParams: JSON.stringify({ dataType: 2, dataId: sid }), platform: '8', comboChannel: '2', dmChannel: 'damai@damaih5_h5' };
          let s = await mtop('mtop.damai.cn', 'mtop.alibaba.detail.subpage.getdetail', '2.0', skuData, '&H5Request=true&timeout=10000&valueType=original&forceAntiCreep=true&AntiCreep=true&useH5=true');
          console.log('sku try1:', JSON.stringify((s.ret || [])[0] || s.parseError || Object.keys(s)));
          if (s.ret && !/SUCCESS/.test(s.ret[0])) {
            s = await mtop('mtop.damai.cn', 'mtop.alibaba.detail.subpage.getdetail', '2.0', skuData, '&H5Request=true&timeout=10000&valueType=original&forceAntiCreep=true&AntiCreep=true&useH5=true');
            console.log('sku try2:', JSON.stringify((s.ret || [])[0] || s.parseError || Object.keys(s)));
          }
          if (s.data && s.data.result) {
            const res2 = typeof s.data.result === 'string' ? JSON.parse(s.data.result) : s.data.result;
            const skus = res2.perform && res2.perform.skuList || [];
            console.log('skuList:', skus.length, skus.slice(0, 4).map((x) => x.priceName + ':' + x.skuSalable).join(' | '));
          }
        }
      } else {
        console.log('legacy 结构里没找到 detailViewComponentMap.item.item');
      }
    }
  }

  console.log('===== B. 纷玩岛 livelab =====');
  const fh = {
    'User-Agent': FWD_UA,
    Accept: '*/*',
    'Content-Type': 'application/json',
    'Accept-Language': 'zh-CN,zh',
    Referer: 'https://servicewechat.com/wx5a8f481d967649eb/70/page-frame.html',
    authorization: FWD_AUTH,
  };
  // B1: 搜索（猜端点，依次试）
  const candidates = [
    'https://api.livelab.com.cn/performance/app/search/project?keyword=' + encodeURIComponent('林忆莲'),
    'https://api.livelab.com.cn/performance/app/search/project?keyword=' + encodeURIComponent('林忆莲') + '&page=1&pagesize=10',
    'https://api.livelab.com.cn/performance/app/project/search?keyword=' + encodeURIComponent('林忆莲'),
  ];
  for (const u of candidates) {
    try {
      const r = await get(u, fh);
      console.log('try', u.slice(40, 80), '→', r.status, r.body.slice(0, 150).replace(/\n/g, ' '));
      if (r.status === 200 && r.body.includes('"code"')) break;
    } catch (e) { console.log('try', u.slice(40, 80), 'ERR', e.message); }
  }
})();
