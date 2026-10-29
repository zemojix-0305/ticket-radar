/**
 * 服务端常驻监控器 —— 「关掉小程序也能收到微信消息」靠的就是它
 * ---------------------------------------------------------------------------
 * 原来的链路是死的：小程序 poll 一次 -> 页面显示，关掉小程序就什么都不发生。
 * 小程序没有后台常驻能力（微信不允许），所以想让提醒真的进微信，必须有一台
 * 机器一直在查。这个文件就是那台机器上的循环。
 *
 * 分工：
 *   小程序   负责「有哪些票要盯」——每次加/删监控把任务列表 POST 上来（pushSync）
 *   服务端   负责「一直查 + 真的发」——本文件按固定间隔跑，发现回流就调 wxpush
 *
 * 数据落盘（都在 server/state/，已 gitignore）：
 *   tasks.json   任务列表（小程序同步过来的）
 *   prev.json    上一轮的查询结果，回流判定要用它当基线
 *   push.json    推送冷却记录，避免同一张票反复推
 *
 * 合规下限：间隔不得低于 60 秒。压到秒级 + 换 IP = 自动刷票，明令禁止。
 * 「快」指的是回流后尽快通知，不是比谁手速快。
 */

'use strict';

const fs = require('fs');
const path = require('path');

const STATE_DIR = path.join(__dirname, 'state');
const TASKS_FILE = path.join(STATE_DIR, 'tasks.json');
const PREV_FILE = path.join(STATE_DIR, 'prev.json');
const PUSH_FILE = path.join(STATE_DIR, 'push.json');

/** 合规下限，别改小 */
const MIN_INTERVAL = 60;
const DEFAULT_INTERVAL = 120;
/** 同一个任务两次推送的最小间隔（毫秒），防止平台数据抖动导致连环推 */
const PUSH_COOLDOWN = 10 * 60 * 1000;

let pollFn = null; // (event) => Promise<result>，由 api.js 注入
let timer = null;
let running = false;
let lastRun = 0;
let lastError = '';
let lastResult = null;

// ---------------------------------------------------------------------------
// 落盘
// ---------------------------------------------------------------------------
function ensureDir() {
  try {
    fs.mkdirSync(STATE_DIR, { recursive: true });
  } catch (e) { /* 已存在 */ }
}

function readJson(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (e) {
    return fallback;
  }
}

function writeJson(file, data) {
  ensureDir();
  try {
    fs.writeFileSync(file, JSON.stringify(data, null, 2));
  } catch (e) {
    console.warn('[watcher] 写盘失败 ' + file + ': ' + e.message);
  }
}

function loadTasks() {
  const j = readJson(TASKS_FILE, null);
  return j && Array.isArray(j.tasks) ? j : { tasks: [], pushOn: false, updatedAt: '' };
}

// ---------------------------------------------------------------------------
// 对外：小程序同步任务
// ---------------------------------------------------------------------------
function setTasks(payload) {
  const tasks = Array.isArray(payload && payload.tasks) ? payload.tasks : [];
  const pushOn = !!(payload && payload.pushOn);
  const cur = loadTasks();

  // 只留轮询真正需要的字段：params 里可能塞了下载地址这类长字符串，
  // 全量存盘会让 tasks.json 越滚越大。
  const slim = tasks
    .filter((t) => t && t._id && t.adapter)
    .map((t) => ({
      _id: String(t._id),
      adapter: String(t.adapter),
      name: t.name || '',
      poster: t.poster || '',
      enabled: t.enabled !== false,
      params: t.params || {},
      price_labels: t.price_labels || t.price_label || [],
      session_filter: t.session_filter || '',
    }));

  writeJson(TASKS_FILE, { tasks: slim, pushOn, updatedAt: new Date().toISOString() });

  // 任务被删掉后，它的基线也一并清掉，免得 prev.json 无限增长
  const keep = new Set(slim.map((t) => t._id));
  const prev = readJson(PREV_FILE, {});
  let changed = false;
  for (const k of Object.keys(prev)) {
    if (!keep.has(k)) {
      delete prev[k];
      changed = true;
    }
  }
  if (changed) writeJson(PREV_FILE, prev);
  // 上一轮的航拍时间要对比，告诉调用方这次变化了什么
  return { ok: true, count: slim.length, pushOn, added: slim.length - cur.tasks.length };
}

// ---------------------------------------------------------------------------
// 轮询一轮
// ---------------------------------------------------------------------------
async function tick(opts) {
  if (!pollFn || running) return { skipped: running ? 'busy' : 'no_poll_fn' };
  const { pushOn, tasks } = loadTasks();
  if (!tasks.length) {
    lastRun = Date.now();
    return { ok: true, tasks: 0, pushed: 0 };
  }

  running = true;
  try {
    const prevStates = readJson(PREV_FILE, {});
    const out = await pollFn({ action: 'poll', tasks, prevStates });
    const res = out && out.results ? out.results : [];
    lastRun = Date.now();
    lastError = '';
    lastResult = { at: new Date().toISOString(), count: res.length };

    // 新的基线写盘：下一轮拿它当「之前的状态」
    // 注意 poll 返回的是「摘要 + full」两层：摘要是 {_id, ok, restock(数字), notify}，
    // 真正的 items / available / history 都在 full 里。存摘要会让下一轮 diffRestock
    // 拿不到 items，于是永远判定「没有回流」——推送一条都发不出去，而且不报错。
    const nextPrev = {};
    for (const r of res) {
      if (r && r._id) nextPrev[r._id] = r.full || r;
    }
    writeJson(PREV_FILE, nextPrev);

    // 有回流就发微信。pushOn=false（用户按了暂停）时一条都不发。
    let pushed = 0;
    const details = [];
    if (pushOn) {
      const pushLog = readJson(PUSH_FILE, {});
      for (const r of res) {
        const full = r.full || r;
        const labels = Array.isArray(full.restock) ? full.restock : [];
        if (!labels.length) continue;
        const key = r._id;
        const last = Number(pushLog[key] || 0);
        if (Date.now() - last < PUSH_COOLDOWN) {
          details.push({ id: key, name: full.name, skipped: 'cooldown' });
          continue;
        }
        try {
          // 多通道分发：小程序订阅 / pushplus / 企业微信 谁通算谁。
          // 只靠小程序订阅的话，用户没续额度就一条都收不到。
          const notify = require('./notify');
          const out = await notify.send({
            title: full.name || '你盯的票',
            event: labels.join('/') + ' 回流',
            org: PLATFORM_CN[full.platform] || full.platform || 'ticket-radar',
            tip: '回流票出现，尽快去官方渠道下单',
            page: 'pages/monitor/monitor',
          });
          if (!out.ok) {
            const why = (out.failed && out.failed[0] && out.failed[0].error) || out.error || '所有通道都没发出去';
            lastError = why;
            details.push({ id: key, name: full.name, sent: false, error: why, failed: out.failed });
            continue;
          }
          pushLog[key] = Date.now();
          pushed++;
          details.push({ id: key, name: full.name, sent: true, labels, via: out.sent });
        } catch (e) {
          lastError = String((e && e.message) || e);
          details.push({ id: key, name: full.name, sent: false, error: lastError });
        }
      }
      if (pushed) writeJson(PUSH_FILE, pushLog);
    }
    return { ok: true, tasks: res.length, pushed, details };
  } catch (e) {
    lastError = String((e && e.message) || e);
    return { ok: false, error: lastError };
  } finally {
    running = false;
  }
}

const PLATFORM_CN = {
  damai: '大麦',
  maoyan: '猫眼',
  fenwandao: '纷玩岛',
  piaoxingqiu: '票星球',
  moretickets: '摩天轮',
  rail12306: '12306',
  flight: '同程机票',
  amadeus: '机票',
};

// ---------------------------------------------------------------------------
// 生命周期
// ---------------------------------------------------------------------------
function start(opts) {
  const o = opts || {};
  if (o.poll) pollFn = o.poll;
  if (timer) return { already: true, interval: currentInterval() };
  const interval = Math.max(MIN_INTERVAL, Number(o.interval) || Number(process.env.WATCH_INTERVAL) || DEFAULT_INTERVAL);
  timer = setInterval(() => {
    tick().catch((e) => console.warn('[watcher] tick 异常: ' + e.message));
  }, interval * 1000);
  // 启动后先等 10 秒再跑第一轮，别和 HTTP 服务启动抢资源
  setTimeout(() => tick().catch(() => {}), 10 * 1000);
  console.log(`[watcher] 已启动，每 ${interval} 秒查一轮（合规下限 ${MIN_INTERVAL}s）`);
  return { ok: true, interval };
}

function currentInterval() {
  return timer && timer._idleTimeout ? Math.round(timer._idleTimeout / 1000) : 0;
}

function stop() {
  if (timer) clearInterval(timer);
  timer = null;
}

function status() {
  const { tasks, pushOn, updatedAt } = loadTasks();
  let wx = { ok: false, miss: ['未加载'], has: {} };
  let chs = [];
  try {
    wx = require('./wxpush').ready();
    chs = require('./notify').channels();
  } catch (e) { /* ignore */ }
  return {
    ok: true,
    watching: !!timer,
    interval: currentInterval(),
    tasks: tasks.length,
    pushOn,
    syncedAt: updatedAt,
    lastRun: lastRun ? new Date(lastRun).toISOString() : '',
    lastError,
    lastResult,
    channels: chs,
    wx: { ready: wx.ok, missing: wx.miss || [], openid: !!(wx.has && wx.has.openId) },
  };
}

module.exports = { start, stop, tick, setTasks, status, loadTasks, MIN_INTERVAL };
