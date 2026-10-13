// 用系统 Edge 截图参考网站，输出多张滚动截图
const { chromium } = require('playwright-core');

(async () => {
  const browser = await chromium.launch({
    channel: 'msedge',
    headless: true,
    args: ['--disable-blink-features=AutomationControlled', '--no-sandbox']
  });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1
  });
  await page.goto('https://hijessie.space/', { waitUntil: 'networkidle', timeout: 60000 }).catch(e => console.log('goto warn:', e.message));
  await page.waitForTimeout(4000);

  // 整页高度
  const h = await page.evaluate(() => document.body.scrollHeight);
  console.log('PAGE_HEIGHT:', h);

  // 提取设计令牌：颜色/字体/背景
  const tokens = await page.evaluate(() => {
    const out = { colors: {}, fonts: {}, bg: null, bodyFont: null, headings: [] };
    const body = getComputedStyle(document.body);
    out.bg = body.backgroundColor;
    out.bodyFont = body.fontFamily;
    out.bodyColor = body.color;
    // 抓取所有用到的背景色
    const bgSet = {}, fontSet = {}, sizeSet = {};
    document.querySelectorAll('*').forEach(el => {
      const s = getComputedStyle(el);
      if (s.backgroundColor && s.backgroundColor !== 'rgba(0, 0, 0, 0)') bgSet[s.backgroundColor] = (bgSet[s.backgroundColor] || 0) + 1;
      if (s.color) fontSet[s.color] = (fontSet[s.color] || 0) + 1;
      if (s.fontFamily) fontSet['F:' + s.fontFamily] = 1;
      if (el.tagName.match(/^H[1-3]$/) || el.tagName === 'P') {
        if (out.headings.length < 30) out.headings.push({ tag: el.tagName, text: (el.textContent || '').trim().slice(0, 60), font: s.fontFamily, size: s.fontSize, weight: s.fontWeight, color: s.color });
      }
    });
    out.colors = { bg: bgSet, fg: fontSet };
    return out;
  });
  console.log('TOKENS:', JSON.stringify(tokens, null, 1).slice(0, 3000));

  // 分段截图（每 900px 一张，最多 6 张）
  const shots = Math.min(6, Math.ceil(h / 900));
  for (let i = 0; i < shots; i++) {
    await page.evaluate(y => window.scrollTo(0, y), i * 900);
    await page.waitForTimeout(800);
    await page.screenshot({ path: `D:\\Amyapp\\ticket-radar\\jessie_ref_${i + 1}.png` });
  }
  // 整页截图
  await page.screenshot({ path: 'D:\\Amyapp\\ticket-radar\\jessie_full.png', fullPage: true });
  console.log('SHOTS_DONE:', shots);
  await browser.close();
})().catch(e => { console.error('FATAL:', e.message); process.exit(1); });
