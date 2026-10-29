/**
 * 推送分发 —— 一条消息同时走多个通道，谁通算谁
 * ---------------------------------------------------------------------------
 * 为什么要多通道：微信小程序订阅消息是「一次性订阅」，用户点一次授权只能收
 * 一条（微信的硬规则，改不了）。只靠它，用户半夜回流了却没额度，照样收不到。
 * 所以真正的做法是：主力用一条不限量的微信通道，小程序订阅消息当备用。
 *
 * 通道优先级（都是「微信里能收到」，按配置难度排）：
 *   1. pushplus   微信公众号推送。扫码登录拿 token 即可，免费额度每天 200 条，
 *                 收到的是「服务通知」里一条正经消息。最省事，推荐。
 *   2. wecom      企业微信群机器人。个人也能注册企业微信，建个群加机器人就有
 *                 webhook，条数不限，手机装企业微信就能收。
 *   3. wxsubscribe 小程序订阅消息。零配置（凭据都在 .env），但受一次性订阅额度限制，
 *                 适合当「点开就能跳进小程序」的补充。
 *   4. serverchan / bark / ntfy  其他备选，配了就用。
 *
 * 全部都只在 .env 里配好才启用，没配的直接跳过——不报错、不阻塞别的通道。
 */

'use strict';

const fs = require('fs');
const path = require('path');

function readDotEnv() {
  const out = {};
  try {
    const raw = fs.readFileSync(path.join(__dirname, '..', '.env'), 'utf8');
    for (const line of raw.split(/\r?\n/)) {
      const s = line.trim();
      if (!s || s.startsWith('#')) continue;
      const i = s.indexOf('=');
      if (i <= 0) continue;
      let v = s.slice(i + 1).trim();
      if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) {
        v = v.slice(1, -1);
      }
      if (v) out[s.slice(0, i).trim()] = v;
    }
  } catch (e) { /* 没 .env 就只认环境变量 */ }
  return out;
}

const DOTENV = readDotEnv();
const pick = (k) => process.env[k] || DOTENV[k] || '';

async function postJson(url, body, headers) {
  const res = await fetch(url, {
    method: 'POST',
    headers: Object.assign({ 'Content-Type': 'application/json' }, headers || {}),
    body: JSON.stringify(body),
  });
  const text = await res.text();
  let json = null;
  try { json = JSON.parse(text); } catch (e) { /* 有的通道回纯文本 */ }
  return { status: res.status, json, text: text.slice(0, 300) };
}

// ---------------------------------------------------------------------------
// 各通道：都返回 { channel, ok, error? }
// ---------------------------------------------------------------------------
async function viaPushPlus(msg) {
  const token = pick('PUSHPLUS_TOKEN');
  if (!token) return null;
  const r = await postJson('https://www.pushplus.plus/send', {
    token,
    title: msg.title,
    content: [msg.event, msg.org, msg.tip].filter(Boolean).join('<br/>'),
    template: 'html',
  });
  if (r.json && Number(r.json.code) === 200) return { channel: 'pushplus', ok: true };
  return { channel: 'pushplus', ok: false, error: (r.json && r.json.msg) || r.text };
}

async function viaWecom(msg) {
  const hook = pick('WECOM_WEBHOOK');
  if (!hook) return null;
  const lines = [`**${msg.title}**`, msg.event, msg.org, msg.tip].filter(Boolean);
  const r = await postJson(hook, {
    msgtype: 'markdown',
    markdown: { content: lines.join('\n') },
  });
  if (r.json && Number(r.json.errcode) === 0) return { channel: 'wecom', ok: true };
  return { channel: 'wecom', ok: false, error: (r.json && r.json.errmsg) || r.text };
}

async function viaWxSubscribe(msg) {
  const wxpush = require('./wxpush');
  if (!wxpush.ready().ok) return null;
  try {
    await wxpush.send({
      title: msg.title,
      event: msg.event,
      org: msg.org,
      tip: msg.tip,
      page: msg.page,
    });
    return { channel: 'wxsubscribe', ok: true };
  } catch (e) {
    return { channel: 'wxsubscribe', ok: false, error: e.message, errcode: e.errcode };
  }
}

async function viaServerChan(msg) {
  const key = pick('SERVERCHAN_SENDKEY');
  if (!key) return null;
  const r = await postJson(`https://sctapi.ftqq.com/${key}.send`, {
    title: msg.title,
    desp: [msg.event, msg.org, msg.tip].filter(Boolean).join('\n\n'),
  });
  if (r.json && Number(r.json.code) === 0) return { channel: 'serverchan', ok: true };
  return { channel: 'serverchan', ok: false, error: (r.json && r.json.message) || r.text };
}

async function viaBark(msg) {
  const key = pick('BARK_KEY');
  if (!key) return null;
  const base = key.startsWith('http') ? key.replace(/\/+$/, '') : `https://api.day.app/${key}`;
  const u = `${base}/${encodeURIComponent(msg.title)}/${encodeURIComponent(msg.event || '')}?group=ticket-radar`;
  const res = await fetch(u);
  if (res.ok) return { channel: 'bark', ok: true };
  return { channel: 'bark', ok: false, error: 'HTTP ' + res.status };
}

async function viaNtfy(msg) {
  const topic = pick('NTFY_TOPIC');
  if (!topic) return null;
  const res = await fetch(`https://ntfy.sh/${encodeURIComponent(topic)}`, {
    method: 'POST',
    headers: { Title: encodeURIComponent(msg.title), Tags: 'ticket' },
    body: [msg.event, msg.org, msg.tip].filter(Boolean).join('\n'),
  });
  if (res.ok) return { channel: 'ntfy', ok: true };
  return { channel: 'ntfy', ok: false, error: 'HTTP ' + res.status };
}

// ---------------------------------------------------------------------------
// 对外
// ---------------------------------------------------------------------------
/** 有哪些通道配好了（给状态接口和启动日志用） */
function channels() {
  const list = [];
  if (pick('PUSHPLUS_TOKEN')) list.push('pushplus');
  if (pick('WECOM_WEBHOOK')) list.push('wecom');
  try {
    if (require('./wxpush').ready().ok) list.push('wxsubscribe');
  } catch (e) { /* ignore */ }
  if (pick('SERVERCHAN_SENDKEY')) list.push('serverchan');
  if (pick('BARK_KEY')) list.push('bark');
  if (pick('NTFY_TOPIC')) list.push('ntfy');
  return list;
}

/**
 * 发一条。**只要有一个通道成功就算成功**——这是重点：
 * pushplus 配额满了、小程序订阅没额度了，只要企业微信能收到，用户就收到了。
 * @returns {ok, sent[], failed[]}
 */
async function send(msg) {
  const enabled = channels();
  if (!enabled.length) {
    return {
      ok: false,
      sent: [],
      failed: [],
      error: '一个推送通道都没配。在 .env 里填 PUSHPLUS_TOKEN（微信扫码即可拿）就通了',
    };
  }
  const tasks = [];
  for (const c of enabled) {
    if (c === 'pushplus') tasks.push(viaPushPlus(msg));
    else if (c === 'wecom') tasks.push(viaWecom(msg));
    else if (c === 'wxsubscribe') tasks.push(viaWxSubscribe(msg));
    else if (c === 'serverchan') tasks.push(viaServerChan(msg));
    else if (c === 'bark') tasks.push(viaBark(msg));
    else if (c === 'ntfy') tasks.push(viaNtfy(msg));
  }
  // 一个通道挂掉不该拖垮别的，所以全部 settled
  const settled = await Promise.allSettled(tasks);
  const sent = [];
  const failed = [];
  for (const s of settled) {
    if (s.status !== 'fulfilled' || !s.value) continue;
    if (s.value.ok) sent.push(s.value.channel);
    else failed.push({ channel: s.value.channel, error: s.value.error, errcode: s.value.errcode });
  }
  return { ok: sent.length > 0, sent, failed };
}

module.exports = { send, channels };
