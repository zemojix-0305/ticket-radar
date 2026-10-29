#!/usr/bin/env node
/**
 * verify.mjs —— 开发↔测试闭环的总入口（AI 改完代码跑这个，结果直接决定下一步）
 *
 *   node tools/verify.mjs             # 全量：语法 + 离线断言 + 活体探针（需本地服务）
 *   node tools/verify.mjs --offline   # 只跑前两段（改纯逻辑时用，秒回）
 *   node tools/verify.mjs --json      # 机器可读输出（给 AI/CI 消费）
 *
 * 设计原则：
 * - FAIL 只代表「行为和预期不符」，凭据过期这类配置问题只 WARN 不 FAIL，
 *   这样 AI 不会被「不是代码的错」带偏去乱改代码。
 * - 每条断言输出「现象 + 最可能的下一步」，而不是干巴巴的布尔值。
 * - 预期写在 EXPECT 配置里，平台接口变了改这里，不改探针逻辑。
 */

'use strict';

const API = process.env.RADAR_API || 'http://127.0.0.1:3000/api';
const args = process.argv.slice(2);
const JSON_MODE = args.includes('--json');
const OFFLINE = args.includes('--offline');

const results = [];
function record(section, name, ok, detail, next) {
  results.push({ section, name, ok: !!ok, level: ok ? 'pass' : 'fail', detail: detail || '', next: next || '' });
}
function warn(section, name, detail, next) {
  results.push({ section, name, ok: false, level: 'warn', detail: detail || '', next: next || '' });
}

// ---------------------------------------------------------------------------
// 1. 语法检查
// ---------------------------------------------------------------------------
async function syntaxCheck() {
  const { spawnSync } = await import('node:child_process');
  const files = [
    'miniprogram/cloudfunctions/radar/index.js',
    'miniprogram/cloudfunctions/radar/smoke.js',
    'miniprogram/miniprogram/pages/detail/detail.js',
    'miniprogram/miniprogram/pages/monitor/monitor.js',
    'miniprogram/miniprogram/pages/shows/shows.js',
    'server/api.js',
  ];
  for (const f of files) {
    const r = spawnSync(process.execPath, ['--check', f], { encoding: 'utf8' });
    record('语法', f, r.status === 0, r.status === 0 ? '' : String(r.stderr).split('\n')[0],
      r.status === 0 ? '' : '修语法错误后重跑');
  }
}

// ---------------------------------------------------------------------------
// 2. 离线断言（云函数纯函数，不联网）
// ---------------------------------------------------------------------------
async function offlineChecks() {
  global.cloud = {
    getStorage: async () => null,
    setStorage: async () => {},
    database: () => ({ collection: () => ({ where: () => ({ get: async () => ({ data: [] }) }) }) }),
  };
  const { createRequire } = await import('node:module');
  const require = createRequire(import.meta.url);
  let I;
  try {
    I = require('../miniprogram/cloudfunctions/radar/index.js')._internals;
    record('离线', '云函数可加载(_internals)', !!I, '', '检查 index.js 是否有顶层语法/引用错误');
  } catch (e) {
    record('离线', '云函数可加载(_internals)', false, String(e).slice(0, 120), '检查 index.js 顶层代码');
    return;
  }
  const { filterItemsByTiers, wantedTiers, filterItemsBySessions, wantedSessions } = I;
  const FIX = [
    { label: '看台 399', price: '399' },
    { label: '看台 1399', price: '1399' },
    { label: '内场 1699', price: '1699' },
    { label: 'VIP 699', price: '699' },
  ];
  const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const t = (name, ok, next) => record('离线', name, ok, '', next);

  const o = filterItemsByTiers(FIX, { price_labels: ['399', '1699'] });
  t('价位多选=并集', o.length === 2 && o[0].price === '399' && o[1].price === '1699', '查 filterItemsByTiers');
  t('数字整体相等(399不命中1399)', filterItemsByTiers(FIX, { price_labels: ['399'] }).length === 1, '查 tierHit');
  t('单数 price_label 兼容', filterItemsByTiers(FIX, { price_label: '699' }).length === 1, '查 wantedTiers 兜底');
  t('非数字走子串', filterItemsByTiers(FIX, { price_labels: ['看台'] }).length === 2, '查 tierHit 非数字分支');
  t('空勾=盯整场', filterItemsByTiers(FIX, {}).length === FIX.length, '查 wantedTiers 空值');
  t('price_labels 优先于 price_label', eq(wantedTiers({ price_labels: ['1'], price_label: '2' }), ['1']), '');

  const S = [
    { label: '2027-06-08 19:00 A', when: '2027-06-08 19:00' },
    { label: '2027-06-09 19:00 A', when: '2027-06-09 19:00' },
    { label: '2027-06-11 19:00 A', when: '2027-06-11 19:00' },
  ];
  t('场次勾一场', filterItemsBySessions(S, { sessions: ['2027-06-09 19:00 A'] }).length === 1, '查 filterItemsBySessions');
  t('场次多选=并集', filterItemsBySessions(S, { sessions: ['2027-06-09 19:00 A', '2027-06-11 19:00 A'] }).length === 2, '');
  t('场次用 when 也能命中', filterItemsBySessions(S, { sessions: ['2027-06-08 19:00'] }).length === 1, '查 hitSession');
  t('场次空勾=全选', filterItemsBySessions(S, {}).length === S.length, '');
  t('sessions 优先级', eq(wantedSessions({ sessions: ['x'] }), ['x']), '');
}

// ---------------------------------------------------------------------------
// 3. 活体探针（打真实本地服务）
// ---------------------------------------------------------------------------
async function post(payload, timeout = 45000) {
  const res = await fetch(API, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(timeout),
  });
  return res.json();
}

// 每平台的预期。id 缺省时从 find 第一行取。
// fenwandao 的 id 是加密 base64（从微博分享短链解出），匿名 find/detail 均可用；
// get_performs（票档级）仍要凭证，属预期 WARN。
const FWD_LINK = 'https://m.livelab.com.cn/hppreview/pages/concert/detail?id=' +
  encodeURIComponent('6Z7dDOzIH3/BkhVHWLmiq1mzAtxijo4DkGC+p/cwsBo=') + '&type=1&channel=weibo';
const EXPECT = [
  { platform: 'maoyan', find: 'ok', detailId: '498506', detail: 'ok', detailNoTiers: true },
  { platform: 'moretickets', find: 'ok', detail: 'ok', sessionsAsItems: true },
  { platform: 'bilibili', find: 'ok', detail: 'ok' },
  { platform: 'piaoxingqiu', find: 'ok', detail: 'ok', wantRealTiers: true },
  { platform: 'damai', find: 'ok', flakyFind: true, detailId: '1086111708947', detail: 'ok-or-degraded' },
  // 淘票票与大麦同一套 itemId/通道；flakyFind：关键词搜索有滑块风控，限频归 WARN
  { platform: 'taopiaopiao', find: 'ok', flakyFind: true, detailId: '1086111708947', detail: 'ok-or-degraded' },
  { platform: 'fenwandao', find: 'ok', findKw: FWD_LINK, detailId: decodeURIComponent(FWD_LINK.match(/id=([^&]+)/)[1]), detail: 'ok' },
  { platform: 'rail12306', find: 'ok', findKw: '广州南 长沙南', findDate: new Date(Date.now() + 8*3600000 + 3*86400000).toISOString().slice(0, 10), detail: 'ok', rail: true },
];

function idOf(row) {
  return String(row?.tourId ?? row?.performanceId ?? row?.projectId ?? row?.showId ?? row?.id ?? '');
}

/** detail 响应的结构校验（所有平台共用， invariant 集中在一处） */
function checkDetailShape(exp, d, sec) {
  const tag = `${exp.platform}/detail`;
  if (d.ok === false && d.degraded) {
    warn(sec, tag + ' 结构', 'ok=false 但带 degraded，服务端应有 r.ok 才对', '查 detail 分支返回值');
    return;
  }
  record(sec, tag + ' ok=true', d.ok === true, `ok=${d.ok} err=${String(d.error || '').slice(0, 60)}`, '看服务端日志定位该平台详情失败原因');
  const sess = d.sessions || [];
  record(sec, tag + ' sessions 是数组', Array.isArray(sess), `type=${typeof sess}`, '');
  if (!Array.isArray(sess)) return;

  // 假档位不变量：档位名 == 场次名 ⇒ 服务端又在造假档
  let fake = null;
  for (const s of sess) {
    for (const t of s.tiers || []) {
      if (!String(t.name || '').trim() || String(t.name).trim() === String(s.name || '').trim()) { fake = t.name; break; }
    }
    if (fake !== null) break;
  }
  record(sec, tag + ' 无假档位(档名≠场名)', fake === null, fake === null ? '' : `出现档位名 ${JSON.stringify(fake)}`, '通用兜底又在把行标签当票档，查 generic 分支');

  // 猫眼预期 noTiers=true（匿名只有项目级）；其他平台至少有信息
  if (exp.detailNoTiers) {
    record(sec, `${exp.platform}/detail 明确 noTiers`, d.noTiers === true, `noTiers=${d.noTiers}`, '猫眼匿名本就无票档，别返回假档');
  }
  if (exp.wantRealTiers) {
    const tiers = sess.flatMap((s) => s.tiers || []);
    record(sec, `${exp.platform}/detail 有真实票档`, tiers.length > 0 && tiers.some((t) => String(t.price || '').trim()), `sessions=${sess.length} tiers=${tiers.length}`, '该平台匿名本应给分档，接口可能变了，抓包核对');
  }
}

async function liveChecks() {
  // 服务可达性
  let pong;
  try {
    pong = await post({ action: 'find', platform: 'maoyan', keyword: '', limit: 1 }, 8000);
  } catch (e) {
    warn('活体', '本地服务可达', `连不上 ${API}：${String(e).slice(0, 80)}`,
      'cd server && node api.js 启动后再跑活体段');
    return;
  }
  record('活体', '本地服务可达', !!pong, '', '');

  // 大麦/淘票票热卖榜 + 关键词搜索：2026-10-14 打通匿名通道（新版 H5 的
  // CMS 分类页 + search.search 带 H5Request 开关）。
  //   · 热卖榜（CMS 分类通道）对匿名宽容 → 硬断言必须有行。
  //   · 关键词搜索（search.search）有滑块风控 FAIL_SYS_USER_VALIDATE/RGV587，
  //     密集测试会按 IP 冷却 → 被限频归 WARN（平台限频不是代码问题）。
  // 反例防线：绝不能再落到 12306 的站站查询报错文案（淘票票 tab 曾显示它）。
  for (const p of ['damai', 'taopiaopiao']) {
    try {
      const h = await post({ action: 'find', platform: p, keyword: '', limit: 3 });
      const hrows = h.rows || [];
      record('活体', `${p}/热卖榜返回真实行`, h.ok === true && hrows.length > 0,
        `ok=${h.ok} rows=${hrows.length} err=${String(h.error || '').slice(0, 40)}`,
        '大麦 CMS 分类通道可能被网关改了；抓包 m.damai.cn/shows/category.html 核对');
      record('活体', `${p}/热卖榜未落到12306文案`, !/两站之间|出发地/.test(String(h.error || '')), '', '');

      const s = await post({ action: 'find', platform: p, keyword: '周杰伦', limit: 3 });
      const srows = s.rows || [];
      if (s.ok === true && srows.length > 0) {
        record('活体', `${p}/关键词搜索返回真实行`, true, `rows=${srows.length}`, '');
      } else if (s.ok === false && /风控|RGV587|USER_VALIDATE|挤爆|排起了长队/.test(String(s.error || ''))) {
        warn('活体', `${p}/关键词搜索`, '匿名搜索触发滑块风控（IP 冷却中）',
          '密集测试触发的平台限频，等几分钟重跑即绿；或在「我的」页填大麦 Cookie 后更稳');
      } else {
        record('活体', `${p}/关键词搜索返回真实行`, false,
          `ok=${s.ok} rows=${srows.length} err=${String(s.error || '').slice(0, 50)}`,
          '可能是搜索关键词无结果（换「音乐节」验证），或匿名通道被改了');
      }
      record('活体', `${p}/关键词搜索未落到12306文案`, !/两站之间|出发地/.test(String(s.error || '')), '', '');
    } catch (e) {
      warn('活体', `${p}/大麦通道`, '请求异常 ' + String(e).slice(0, 60), '');
    }
  }

  // 12306 过期日期（2026-10-19 钉死）：过去日期 12306 回 HTML 错误页，
  // 曾被冒充成「风控拦截/云 IP 被拒」。必须在服务端本地拦截、说人话。
  try {
    const pastDay = new Date(Date.now() - 3 * 86400000).toISOString().slice(0, 10);
    const pr = await post({ action: 'find', platform: 'rail12306', keyword: '广州南 长沙南', date: pastDay, limit: 1 });
    record('活体', 'rail12306/过期日期给友好提示',
      pr.ok === false && /已经过期|只能查/.test(String(pr.error || '')),
      `ok=${pr.ok} err=${String(pr.error || '').slice(0, 50)}`,
      '过期日期要本地拦截说人话，不能让 12306 的 HTML 错误页冒充风控报错');
  } catch (e) {
    warn('活体', 'rail12306/过期日期', '请求异常 ' + String(e).slice(0, 60), '');
  }


  for (const exp of EXPECT) {
    const sec = '活体';
    // ---- find ----
    const body = { action: 'find', platform: exp.platform, keyword: exp.findKw || '演唱会', limit: 3 };
    if (exp.findDate) body.date = exp.findDate;
    let f;
    try {
      f = await post(body);
    } catch (e) {
      warn(sec, `${exp.platform}/find`, '请求异常 ' + String(e).slice(0, 60), '看服务是否被平台风控');
      continue;
    }
    if (exp.find === 'warn-token' && f.ok === false) {
      warn(sec, `${exp.platform}/find`, `凭据问题：${String(f.error).slice(0, 60)}`, '这是配置问题不是代码问题：刷新 .env 里对应 Cookie/Token');
      // 凭据挂了就不continue detail——用兜底 id 试 detail，仍可能降级可用
    } else {
      const rows = f.rows || [];
      const flaky = exp.flakyFind && f.ok === false && /长队|排队|RGV587|风控|稍后再试/.test(String(f.error || ''));
      if (flaky) {
        warn(sec, `${exp.platform}/find`, `匿名通道被限频（${String(f.error).slice(0, 40)}）`,
          '密集测试触发的 IP 限频，等几分钟重跑即绿；填 Cookie 后更稳');
      } else {
        record(sec, `${exp.platform}/find ok+有行`, f.ok === true && rows.length > 0,
          `ok=${f.ok} rows=${rows.length} err=${String(f.error || '').slice(0, 50)}`,
          f.ok ? '关键词太偏，换常用词再试' : '查该平台 find 分支');
      }
    }

    // ---- detail ----
    let did = exp.detailId;
    if (!did && (f.rows || []).length) did = idOf(f.rows[0]);
    if (!did) { warn(sec, `${exp.platform}/detail`, '没有可用 id（find 没回行且未配 detailId）', '在 EXPECT 里补 detailId 或修 find'); continue; }
    const dbody = { action: 'detail', platform: exp.platform, id: did };
    if (exp.rail) { dbody.from = '广州南'; dbody.to = '长沙南'; dbody.date = exp.findDate; }
    let d;
    try {
      d = await post(dbody, 60000);
    } catch (e) {
      warn(sec, `${exp.platform}/detail`, '请求异常 ' + String(e).slice(0, 60), '');
      continue;
    }
    if (exp.detail === 'warn-token') {
      const okShape = d.ok === true || (d.ok === false && d.degraded);
      if (!okShape) {
        warn(sec, `${exp.platform}/detail`, `需要凭据：${String(d.error || '').slice(0, 60)}`,
          '配置问题：填好 .env 凭据后重跑即变绿');
      } else {
        record(sec, `${exp.platform}/detail ok或degraded`, true, '');
      }
    } else if (exp.detail === 'ok-or-degraded') {
      const okShape = d.ok === true || (d.ok === false && d.degraded);
      record(sec, `${exp.platform}/detail ok或degraded`, okShape, `ok=${d.ok} degraded=${d.degraded} err=${String(d.error || '').slice(0, 50)}`, '');
      if (d.ok === true) checkDetailShape({ platform: exp.platform }, d, sec);
    } else {
      checkDetailShape(exp, d, sec);
    }

    // ---- poll（价位/场次过滤的端到端）----
    if (exp.platform === 'maoyan' && d.ok) {
      const p = await post({ action: 'poll', tasks: [{ _id: 'v', adapter: 'maoyan', name: 'verify', params: { performance_id: did, price_labels: ['480'] } }], prevStates: {} });
      const r0 = (p.results || [])[0] || {};
      const full = r0.full || r0;
      record(sec, 'poll 价位过滤端到端', full.tier_filter?.[0] === '480' && (full.items || []).every((i) => String(i.price || i.label || '').includes('480')),
        `items=${(full.items || []).length} tier_filter=${JSON.stringify(full.tier_filter)}`, '查 poll 分支 filterItemsByTiers 接线');
    }
    if (exp.platform === 'moretickets' && d.ok) {
      const names = (d.sessions || []).map((s) => s.name).filter(Boolean);
      const sel = names.slice(0, 1);
      const p = await post({ action: 'poll', tasks: [{ _id: 'v', adapter: 'moretickets', name: 'verify', params: sel.length ? { tour_id: did, sessions: sel } : { tour_id: did } }], prevStates: {} });
      const r0 = (p.results || [])[0] || {};
      const full = r0.full || r0;
      const all = (full.items || []).length;
      record(sec, 'poll 场次过滤端到端', !sel.length || (all <= 1 && full.session_filter?.length === 1),
        `全部=${all} 勾1场后=${all} filter=${JSON.stringify(full.session_filter)}`, '查 poll 分支 filterItemsBySessions 接线');
    }
  }

  // ---- 详情容错：分享链接直接当 id 传（2026-10-14 加）----
  // 场景：首页监控卡片 / 「我的-正在盯」跳详情时，本地任务里存的可能是链接而不是
  // 裸 id（用户在「加监控」弹层里粘的就是链接）。云函数必须自己把 id 抠出来，
  // 否则用户点卡片看到的是「缺少 platform 或 id」。
  // 这里只钉「抠出来的 id 对不对」——平台抖动不该让这条变红。
  const sec = '活体';
  const URL_ID_CASES = [
    ['damai', 'https://m.damai.cn/shows/item.html?itemId=1086111708947', '1086111708947'],
    ['taopiaopiao', 'https://m.taopiaopiao.com/damai/detail/item.html?id=1086111708947', '1086111708947'],
    ['maoyan', 'https://show.maoyan.com/detail?performanceId=509495', '509495'],
    ['bilibili', 'https://show.bilibili.com/platform/detail.html?id=1006686', '1006686'],
    ['piaoxingqiu', 'https://m.piaoxingqiu.com/show/6aaca4dc697ae400017bf647', '6aaca4dc697ae400017bf647'],
    ['fenwandao', FWD_LINK, decodeURIComponent(FWD_LINK.match(/id=([^&]+)/)[1])],
  ];
  for (const [p, url, want] of URL_ID_CASES) {
    try {
      const r = await post({ action: 'detail', platform: p, id: url }, 45000);
      record(sec, `${p}/分享链接抠出id`, String(r.id) === want,
        `id=${r.id} 期望=${want} ok=${r.ok} err=${String(r.error || '').slice(0, 40)}`,
        'detailShow 开头的「链接→id」归一化分支被改坏了（或该平台的 extract 规则换了）');
    } catch (e) {
      warn(sec, `${p}/分享链接抠出id`, '请求异常 ' + String(e).slice(0, 50), '');
    }
  }
}

// ---------------------------------------------------------------------------
// 输出与退出码
// ---------------------------------------------------------------------------
function printPretty() {
  const bySec = new Map();
  for (const r of results) {
    if (!bySec.has(r.section)) bySec.set(r.section, []);
    bySec.get(r.section).push(r);
  }
  let failN = 0, warnN = 0;
  for (const [sec, rs] of bySec) {
    console.log(`\n== ${sec} ==`);
    for (const r of rs) {
      const mark = r.level === 'pass' ? '✅' : r.level === 'warn' ? '⚠️ ' : '❌';
      if (r.level === 'fail') failN++;
      if (r.level === 'warn') warnN++;
      console.log(`  ${mark} ${r.name}${r.detail ? '  —— ' + r.detail : ''}`);
      if (r.level !== 'pass' && r.next) console.log(`      ↳ 下一步: ${r.next}`);
    }
  }
  console.log(`\n合计: ${results.length - failN - warnN} 过 / ${failN} 败 / ${warnN} 警告`);
  return failN;
}

function printJson() {
  const fails = results.filter((r) => r.level === 'fail');
  const warns = results.filter((r) => r.level === 'warn');
  console.log(JSON.stringify({
    ok: fails.length === 0,
    summary: { pass: results.length - fails.length - warns.length, fail: fails.length, warn: warns.length },
    fails, warns,
    all: results,
  }, null, 2));
  return fails.length;
}

const t0 = Date.now();
await syntaxCheck();
await offlineChecks();
if (!OFFLINE) await liveChecks();
const failN = JSON_MODE ? printJson() : printPretty();
if (!JSON_MODE) console.log(`(${((Date.now() - t0) / 1000).toFixed(1)}s)`);
process.exit(failN > 0 ? 1 : 0);
