/**
 * 微信小程序订阅消息 —— 真正把提醒发进微信「服务通知」
 * ---------------------------------------------------------------------------
 * 这个文件存在的唯一理由：server/api.js 原本把 cloud.openapi.subscribeMessage
 * 整个 mock 掉了（无状态模式没有 openid、也不该在查询接口里发消息），结果是
 * 「小程序里显示推送已开启，手机上从来收不到」——功能等于摆设。
 *
 * 真机推送必须凑齐四样东西，缺一样都发不出去：
 *   1. WX_APPID      小程序 AppID
 *   2. WX_APPSECRET  小程序 AppSecret（只能放服务端，绝不能进小程序代码）
 *   3. WX_OPEN_ID    收消息的人（你自己的 openid，`radar wxbind` 或本文件
 *                    bindByCode() 都能取）
 *   4. WX_TEMPLATE_ID 订阅消息模板的 priTmplId（不是模板标题！）
 *
 * 还有一条微信的硬规则绕不过去：**一次性订阅 = 授权一次只能收一条**。
 * 额度用完后发送返回 43101，这不是 bug。所以：
 *   - 小程序端每次打开/加监控都静默续一次授权（勾了「总是保持以上选择」
 *     就不再弹窗），额度攒着；
 *   - 服务端发出去一条就少一条，收到 43101 不要重试，直接等用户下次续。
 *
 * access_token 有效期 7200s，且每天获取次数有限，所以内存 + 磁盘双缓存，
 * 进程重启也不用重新取。
 */

'use strict';

const fs = require('fs');
const path = require('path');

const TOKEN_URL = 'https://api.weixin.qq.com/cgi-bin/token';
const SEND_URL = 'https://api.weixin.qq.com/cgi-bin/message/subscribe/send';
const SESSION_URL = 'https://api.weixin.qq.com/sns/jscode2session';

/** thing / character 类字段硬上限 20 字，超长微信直接拒发（47003），必须主动剪 */
const THING_MAX = 20;
/** 取不到值时用这个占位，微信不接受空字符串字段 */
const EMPTY = '—';

const TOKEN_CACHE = path.join(__dirname, '.wx_token.json');

// errcode → 人话。出错时直接把排查方向写进日志，别让人对着数字猜。
const CODE_HINTS = {
  40001: 'app_secret 不对或已被重置，去 mp.weixin.qq.com「开发设置」重新生成',
  40013: 'app_id 不对',
  40037: 'template_id 不对，要填「我的模板」里的 priTmplId，不是模板标题',
  41030: 'page 路径不存在，必须是小程序里真实存在的页面',
  43101: '没有订阅额度了：一次性订阅授权一次只能收一条，让用户重新点一次「恢复推送」',
  47003: '模板字段对不上：检查字段名（thing6/time7）与内容长度、time 是否 YYYY-MM-DD HH:MM:SS',
  45009: '调用频率超限，等几分钟',
};

// ---------------------------------------------------------------------------
// 配置
// ---------------------------------------------------------------------------
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
  } catch (e) { /* 没有 .env 就走环境变量 */ }
  return out;
}

const DOTENV = readDotEnv();
const pick = (k) => process.env[k] || DOTENV[k] || '';

function config() {
  return {
    appId: pick('WX_APPID'),
    appSecret: pick('WX_APPSECRET'),
    openId: pick('WX_OPEN_ID'),
    templateId: pick('WX_TEMPLATE_ID'),
  };
}

/** 四要素齐不齐。缺哪个直接说，不要等发送时才报错。 */
function ready() {
  const c = config();
  const miss = [];
  if (!c.appId) miss.push('WX_APPID');
  if (!c.appSecret) miss.push('WX_APPSECRET');
  if (!c.openId) miss.push('WX_OPEN_ID');
  if (!c.templateId) miss.push('WX_TEMPLATE_ID');
  return { ok: miss.length === 0, miss, has: { appId: !!c.appId, openId: !!c.openId } };
}

// ---------------------------------------------------------------------------
// access_token（内存 + 磁盘缓存）
// ---------------------------------------------------------------------------
let _token = '';
let _expiresAt = 0;

function loadCachedToken() {
  if (_token && Date.now() < _expiresAt) return _token;
  try {
    const j = JSON.parse(fs.readFileSync(TOKEN_CACHE, 'utf8'));
    if (j.token && j.expiresAt > Date.now() + 60000) {
      _token = j.token;
      _expiresAt = j.expiresAt;
    }
  } catch (e) { /* 没缓存或坏了，重新取 */ }
  return _token && Date.now() < _expiresAt ? _token : '';
}

async function getToken(force) {
  if (!force) {
    const c = loadCachedToken();
    if (c) return c;
  }
  const { appId, appSecret } = config();
  if (!appId || !appSecret) throw new Error('缺 WX_APPID / WX_APPSECRET，填进 .env 再试');

  const u = `${TOKEN_URL}?grant_type=client_credential&appid=${encodeURIComponent(appId)}&secret=${encodeURIComponent(appSecret)}`;
  const res = await fetch(u);
  const data = await res.json().catch(() => ({}));
  if (!data.access_token) {
    const code = Number(data.errcode || -1);
    throw new Error(`取 access_token 失败（errcode=${code}）：${CODE_HINTS[code] || data.errmsg || '未知'}`);
  }
  _token = data.access_token;
  // 提前 5 分钟过期，避免卡在临界点
  _expiresAt = Date.now() + (Number(data.expires_in || 7200) - 300) * 1000;
  try {
    fs.writeFileSync(TOKEN_CACHE, JSON.stringify({ token: _token, expiresAt: _expiresAt }));
  } catch (e) { /* 写不进去也不影响运行 */ }
  return _token;
}

// ---------------------------------------------------------------------------
// 取 openid（用小程序端 wx.login 拿到的 code 换，免去手工 wxbind）
// ---------------------------------------------------------------------------
async function bindByCode(code) {
  const { appId, appSecret } = config();
  if (!appId || !appSecret) throw new Error('缺 WX_APPID / WX_APPSECRET');
  const u =
    `${SESSION_URL}?appid=${encodeURIComponent(appId)}&secret=${encodeURIComponent(appSecret)}` +
    `&js_code=${encodeURIComponent(code)}&grant_type=authorization_code`;
  const res = await fetch(u);
  const data = await res.json().catch(() => ({}));
  if (!data.openid) {
    const code2 = Number(data.errcode || -1);
    throw new Error(`code 换 openid 失败（errcode=${code2}）：${data.errmsg || '服务端没返回 openid'}`);
  }
  return { openid: data.openid, sessionKey: data.session_key || '' };
}

/** 把新 openid 写回 .env，下次启动直接可用（只改这一行，不动别的） */
function persistOpenId(openid) {
  const p = path.join(__dirname, '..', '.env');
  try {
    const raw = fs.readFileSync(p, 'utf8');
    const eol = raw.includes('\r\n') ? '\r\n' : '\n';
    const lines = raw.split(/\r?\n/);
    let hit = false;
    for (let i = 0; i < lines.length; i++) {
      if (lines[i].startsWith('WX_OPEN_ID=')) {
        lines[i] = `WX_OPEN_ID=${openid}`;
        hit = true;
      }
    }
    if (!hit) lines.push(`WX_OPEN_ID=${openid}`);
    fs.writeFileSync(p, lines.join(eol));
    DOTENV.WX_OPEN_ID = openid;
    return true;
  } catch (e) {
    return false;
  }
}

// ---------------------------------------------------------------------------
// 发送
// ---------------------------------------------------------------------------
function clip(v, limit = THING_MAX) {
  const s = String(v == null ? '' : v).replace(/\s+/g, ' ').trim();
  if (!s) return EMPTY;
  return s.length <= limit ? s : s.slice(0, limit - 1) + '…';
}

function nowStr() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/**
 * 发一条订阅消息。
 * @param {object} o
 *   title  演出/航线名（thing6，20 字内）
 *   event  场次/价格摘要（thing8）
 *   org    放票机构/平台（thing5）
 *   tip    温馨提示（thing4）
 *   page   点开卡片跳的页面
 *   openid 收件人，默认用 .env 里的 WX_OPEN_ID
 * 返回 {ok, errcode, hint, msgid}
 */
async function send(o) {
  const opt = o || {};
  const c = config();
  const touser = opt.openid || c.openId;
  if (!c.appId || !c.appSecret) throw new Error('缺 WX_APPID / WX_APPSECRET，填进 .env');
  if (!c.templateId) throw new Error('缺 WX_TEMPLATE_ID（要 priTmplId，不是模板标题）');
  if (!touser) throw new Error('缺 WX_OPEN_ID：先在小程序里走一次登录绑定');

  const payload = {
    touser,
    template_id: c.templateId,
    page: opt.page || 'pages/monitor/monitor',
    // 字段名必须与申请模板时勾的完全一致（thing6/time7 这种，不是中文标题）
    data: {
      thing6: { value: clip(opt.title) },
      thing8: { value: clip(opt.event) },
      time7: { value: nowStr() }, // time 类型不能截断，格式必须 YYYY-MM-DD HH:MM:SS
      thing5: { value: clip(opt.org || 'ticket-radar') },
      thing4: { value: clip(opt.tip || '有票了，尽快去官方渠道下单') },
    },
  };

  let token = await getToken();
  let out = await post(token, payload);
  // token 可能被服务端提前作废（40001），刷新一次再试，别让用户白丢一条额度
  if (Number(out.errcode) === 40001) {
    token = await getToken(true);
    out = await post(token, payload);
  }
  const errcode = Number(out.errcode || 0);
  if (errcode !== 0) {
    const hint = CODE_HINTS[errcode] || out.errmsg || '未知错误';
    const e = new Error(`微信拒发（errcode=${errcode}）：${hint}`);
    e.errcode = errcode;
    throw e;
  }
  return { ok: true, errcode: 0, msgid: out.msgid || '', openid: touser };
}

async function post(token, payload) {
  const res = await fetch(`${SEND_URL}?access_token=${encodeURIComponent(token)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const text = await res.text();
  try {
    return JSON.parse(text);
  } catch (e) {
    return { errcode: -1, errmsg: `响应不是 JSON：${text.slice(0, 200)}` };
  }
}

module.exports = { ready, config, getToken, bindByCode, persistOpenId, send, CODE_HINTS };
