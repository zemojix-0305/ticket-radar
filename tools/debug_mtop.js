'use strict';
// 调试用：完整复刻 mtop H5 匿名握手，逐行打印，找出 AppKey/签名/令牌哪里不对
const https = require('https');
const crypto = require('crypto');

const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/124.0 Safari/537.36';
const APPKEY = '12574478';
const BASE = 'https://h5api.m.taobao.com/h5';

let cookie = '';

function req(url, headers) {
  return new Promise((resolve, reject) => {
    const r = https.request(
      url,
      { method: 'GET', headers: headers, timeout: 15000, rejectUnauthorized: false },
      (res) => {
        let body = '';
        res.setEncoding('utf8');
        res.on('data', (d) => (body += d));
        res.on('end', () =>
          resolve({ status: res.statusCode, headers: res.headers, body: body }),
        );
      },
    );
    r.on('timeout', () => r.destroy(new Error('超时')));
    r.on('error', reject);
    r.end();
  });
}

async function call(api, dataObj, v, label) {
  const t = Date.now().toString();
  const data = JSON.stringify(dataObj);
  const tkAt = cookie.indexOf('_m_h5_tk=');
  const token = tkAt >= 0 ? cookie.slice(tkAt + 9).split(';')[0].split('_')[0] : '';
  const sign = crypto.createHash('md5').update(`${token}&${t}&${APPKEY}&${data}`).digest('hex');
  const headers = { 'User-Agent': UA, Referer: 'https://m.damai.cn/', Accept: 'application/json' };
  if (cookie) headers.Cookie = cookie;
  const url =
    `${BASE}/${api}/${v}/?jsv=2.7.2&appKey=${APPKEY}&t=${t}` +
    `&sign=${sign}&api=${api}&v=${v}&type=originaljson&dataType=json&data=${encodeURIComponent(data)}`;
  console.log(`\n=== ${label} ===`);
  console.log('cookie sent:', cookie ? cookie.slice(0, 40) + '...' : '(none)');
  console.log('token used:', token);
  const r = await req(url, headers);
  const sc = r.headers['set-cookie'];
  if (sc) {
    cookie = sc.map((c) => c.split(';')[0]).join('; ');
    console.log('set-cookie raw:', JSON.stringify(sc));
    console.log('cookie now:', cookie.slice(0, 60) + '...');
  } else {
    console.log('set-cookie: (none)');
  }
  let j;
  try {
    j = JSON.parse(r.body);
  } catch (e) {
    console.log('body(not json):', r.body.slice(0, 200));
    return null;
  }
  console.log('ret:', JSON.stringify(j.ret));
  console.log('has data.projectInfo:', !!(j.data && Array.isArray(j.data.projectInfo)));
  return j;
}

(async () => {
  const dataObj = {
    cityId: 0, longitude: 0, latitude: 0,
    pageIndex: 1, pageSize: 3,
    keyword: '周杰伦', sourceType: 11, returnItemOption: 4,
    distanceCityId: '852', option: 434,
  };
  await call('mtop.damai.wireless.search.search', dataObj, '1.0', 'call#1 (cold)');
  await call('mtop.damai.wireless.search.search', dataObj, '1.0', 'call#2 (token set)');
  cookie = ''; // 手动清掉，模拟"重新来过"
  await call('mtop.damai.wireless.search.search', dataObj, '1.0', 'call#3 (cookie cleared)');
})();
