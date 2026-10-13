'use strict';
// 探测 bilibili 会员购（show.bilibili.com）公开匿名接口 —— 2026-10 实测可用版
//
//   搜索: /api/ticket/project/listV2?version=134&platform=web&page=1&pagesize=20
//         &ptype=100104&area=0&keyword=<词>     （ptype=100104 = 演出类目）
//   详情: /api/ticket/project/getV2?version=134&id=<pid>&project_id=<pid>&requestSource=pcweb
//
//   老接口 project/get 已废弃（永远"请慢一点"）；listV2 必须带 area（0=全部）；
//   价格单位是分；票档状态用官方 sale_flag.display_name 原文。
//   云函数正式实现在 miniprogram/cloudfunctions/radar/index.js（pollBilibili/findBilibili）。
const https = require('https');

const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36';

function get(url, cookie) {
  return new Promise((resolve, reject) => {
    const h = {
      'User-Agent': UA,
      Referer: 'https://show.bilibili.com/',
      Accept: 'application/json, text/plain, */*',
    };
    if (cookie) h.Cookie = cookie;
    const r = https.request(url, { method: 'GET', headers: h, timeout: 15000, rejectUnauthorized: false }, (res) => {
      let b = '';
      res.setEncoding('utf8');
      res.on('data', (d) => (b += d));
      res.on('end', () => resolve({ status: res.statusCode, body: b, sc: res.headers['set-cookie'] }));
    });
    r.on('timeout', () => r.destroy(new Error('超时')));
    r.on('error', reject);
    r.end();
  });
}

(async () => {
  // buvid3 匿名 cookie（不带偶尔被限频"请慢一点"）
  const home = await get('https://www.bilibili.com/');
  const cookie = (home.sc || []).map((c) => c.split(';')[0]).join('; ');

  const kw = process.argv[2] || '演唱会';
  const s = await get(
    `https://show.bilibili.com/api/ticket/project/listV2?version=134&platform=web&page=1&pagesize=5&ptype=100104&area=0&keyword=${encodeURIComponent(kw)}`,
    cookie,
  );
  const rows = (JSON.parse(s.body).data || {}).result || [];
  console.log(`搜索「${kw}」命中 ${rows.length} 条`);
  for (const r of rows) {
    console.log(` - #${r.project_id || r.id} ${r.project_name || r.name} [${r.city || ''}] ${(r.sale_flag && r.sale_flag.display_name) || ''}`);
  }
  if (!rows.length) return;

  const pid = rows[0].project_id || rows[0].id;
  const d = await get(
    `https://show.bilibili.com/api/ticket/project/getV2?version=134&id=${pid}&project_id=${pid}&requestSource=pcweb`,
    cookie,
  );
  const data = JSON.parse(d.body).data || {};
  console.log(`\n详情 #${pid}：${data.name}`);
  for (const sc of data.screen_list || []) {
    for (const t of sc.ticket_list || []) {
      console.log(`  [${sc.name}] ${t.desc} ¥${t.price / 100} → ${(t.sale_flag && t.sale_flag.display_name) || ''} (is_sale=${t.is_sale} clickable=${t.clickable})`);
    }
  }
})();
