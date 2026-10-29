#!/usr/bin/env node
// 浏览器抓包探针：打开一个页面，把它实际发的 XHR/fetch 全打出来（URL + 方法 + POST 体 + 响应前 500 字）。
//
// 为什么需要它：判断「某平台的某个功能到底有没有匿名通路」时，读压缩过的 JS
// 逆向极其低效（本轮为了找大麦的演出列表，逆向 minified bundle 花了很久仍没找到，
// 换成这个探针 30 秒就抓到了 mtop.damai.mec.aristotle.get）。**页面能匿名渲染，
// 接口就一定能匿名调**——用真浏览器点一遍比猜 API 名靠谱得多。
//
// 用法：
//   node tools/probe_xhr.mjs "https://m.damai.cn/shows/category.html"
//   node tools/probe_xhr.mjs "https://m.damai.cn/shows/search.html?keyword=周杰伦" --filter mtop
//
// 依赖：playwright-core（装在 WorkBuddy 隔离工作区）+ 系统 Edge，无需下载 Chromium。
// 需要 NODE_PATH 指向隔离工作区？不用——这里用绝对 file:// 导入，ESM 也能找到。

import { chromium } from 'file:///C:/Users/zemojix/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.mjs';

const target = process.argv[2];
if (!target) {
  console.error('用法: node tools/probe_xhr.mjs <url> [--filter 子串] [--mobile|--desktop]');
  process.exit(1);
}
const filter = (() => {
  const i = process.argv.indexOf('--filter');
  return i >= 0 ? process.argv[i + 1] : '';
})();
const mobile = !process.argv.includes('--desktop');
const wi = process.argv.indexOf('--wait');
const waitMs = wi >= 0 ? Number(process.argv[wi + 1]) || 3500 : 3500;

const hits = [];
const dumpHeaders = process.argv.includes('--headers');
const browser = await chromium.launch({
  channel: 'msedge',
  headless: !process.argv.includes('--headed'),
  args: ['--disable-blink-features=AutomationControlled'],
});
const ctx = await browser.newContext(
  mobile
    ? {
        userAgent:
          'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.53',
        viewport: { width: 390, height: 844 },
        isMobile: true,
      }
    : {
        userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36',
        viewport: { width: 1280, height: 900 },
      },
);
const page = await ctx.newPage();

page.on('response', async (res) => {
  const u = res.url();
  if (filter && !u.includes(filter)) return;
  const rt = res.request().resourceType();
  if (rt !== 'xhr' && rt !== 'fetch') return;
  let body = '';
  try {
    body = await res.text();
  } catch {}
  hits.push({ u, m: res.request().method(), post: res.request().postData() || '', body, hdrs: res.request().headers(), ck: res.request().headers()['cookie'] || '' });
});

try {
  const wu = process.argv.includes('--dc') ? 'domcontentloaded' : 'networkidle';
  await page.goto(target, { waitUntil: wu, timeout: 45000 });
} catch (e) {
  console.log('goto warn:', String(e.message).split('\n')[0]);
}
await page.waitForTimeout(waitMs);

console.log('=== 标题:', await page.title());
console.log('=== XHR 数:', hits.length);
for (const h of hits) {
  console.log('\n---', h.m, h.u.slice(0, 400));
  if (h.post) console.log('    POST:', h.post.slice(0, 600));
  if (dumpHeaders) {
    const hj = Object.assign({}, h.hdrs);
    delete hj[':authority']; delete hj[':method']; delete hj[':path']; delete hj[':scheme'];
    if (hj.cookie) hj.cookie = String(hj.cookie).slice(0, 300);
    console.log('    HDRS:', JSON.stringify(hj));
  }
  console.log('    RESP:', h.body.slice(0, 500));
}
const cks = await ctx.cookies();
console.log('\n=== cookies:', cks.map((c) => c.name + '=' + String(c.value).slice(0, 36)).join('  '));
const txt = await page.evaluate(() => document.body.innerText.slice(0, 500)).catch(() => '');
console.log('\n=== 可见文本:\n' + String(txt).replace(/\n{2,}/g, '\n'));

await browser.close();
