/**
 * ticket-radar 数据层
 * ---------------------------------------------------------------------------
 * 所有页面的数据请求都从这里出去。业务代码只认一个约定：
 *
 *     request({ action: 'xxx', ... }) → Promise<{ result: 服务端JSON }>
 *
 * **注意 resolve 出来的是 { result: ... }，不是服务端 JSON 本身。**
 * 拿数据永远要先 `const r = (res && res.result) || {}`。
 * 少解这一层包的表现是「后端明明是好的，页面一律报拿不到数据」——
 * 机票面板踩过一次，别再踩。
 *
 * 通道：
 *   config.USE_CLOUD = true  → wx.cloud.callFunction('radar')
 *   config.USE_CLOUD = false → HTTP（本地 server/api.js 或 Vercel）
 *
 * 不做「云函数失败就偷偷回落 HTTP」：那会让真机上的错误变成
 * 「连不上 localhost」，把真正的原因（云函数没部署）盖掉。
 */

const cfg = require('../config');

const API_BASE = cfg.API_BASE || '';

function base() {
  return API_BASE || 'http://localhost:3000';
}

// 纷玩岛 token 存本地（「我的」页填写）。全局随每个请求带上，服务端
// 只在纷玩岛相关的 find/detail/poll 里用它，别的平台无视这个字段。
const FWD_TOKEN_KEY = 'radar_fwd_token';
// 大麦 Cookie（可选）：登录大麦网页版后粘 Cookie 进来，过 mtop 风控最稳。
// 同样全局随请求带上，服务端只在大麦相关调用里用。
const DM_COOKIE_KEY = 'radar_dm_cookie';
// 猫眼登录 token（可选）：猫眼小程序的用户凭证，票档级库存（remainingStock）
// 只有登录通道有。服务端只在猫眼相关调用里用。
const MY_TOKEN_KEY = 'radar_my_token';

/** 把本地存的凭证贴到请求体上（用户不填就不带） */
function withCredentials(params) {
  const payload = Object.assign({}, params);
  if (!payload.fwdToken) {
    const t = wx.getStorageSync(FWD_TOKEN_KEY);
    if (t) payload.fwdToken = t;
  }
  if (!payload.damaiCookie) {
    const ck = wx.getStorageSync(DM_COOKIE_KEY);
    if (ck) payload.damaiCookie = ck;
  }
  if (!payload.myToken) {
    const tk = wx.getStorageSync(MY_TOKEN_KEY);
    if (tk) payload.myToken = tk;
  }
  return payload;
}

// ---------------------------------------------------------------------------
// 通道一：云函数
// ---------------------------------------------------------------------------
function cloudUsable() {
  return !!(cfg.USE_CLOUD && wx.cloud);
}

/**
 * 把 wx.cloud.callFunction 的 errMsg 翻成人话。
 * 这些错误码第一次见会很懵，写清楚能省掉半天排查。
 */
function friendlyCloudError(raw) {
  const s = String(raw || '');
  if (/-501000|FunctionName|could not be found|not found/i.test(s)) {
    return '云函数 radar 还没部署。在开发者工具里右键 cloudfunctions/radar → 上传并部署（云端安装依赖）。';
  }
  if (/-501001|-601001|env.*not exist|INVALID_ENV|环境不存在/i.test(s)) {
    return '云环境不存在：去云开发控制台看一眼环境 ID，把 miniprogram/config.js 里的 CLOUD_ENV 填上。';
  }
  if (/-404011|resource exhausted|QuotaExceed/i.test(s)) {
    return '云开发本月免费额度用完了，下个月会恢复；急用可以在控制台买资源包。';
  }
  if (/-504002/i.test(s)) {
    return '云函数执行超时（查询平台太慢），稍后再试。';
  }
  if (/network|timeout|request:fail|fail/i.test(s)) {
    return '网络不通，检查手机网络后重试。';
  }
  return s || '云函数调用失败';
}

function cloudRequest(payload) {
  return new Promise((resolve, reject) => {
    wx.cloud.callFunction({
      name: cfg.CLOUD_FN,
      data: payload,
      success(res) {
        // 云函数的 return 值在 res.result，这里包一层 result 对齐 HTTP 通道
        resolve({ result: (res && res.result) || {} });
      },
      fail(err) {
        const raw = String((err && err.errMsg) || err);
        const e = new Error(friendlyCloudError(raw));
        e.raw = raw;
        reject(e);
      },
    });
  });
}

// ---------------------------------------------------------------------------
// 通道二：HTTP（本地调试 / 自建服务）
// ---------------------------------------------------------------------------
function httpRequest(payload) {
  return new Promise((resolve, reject) => {
    wx.request({
      url: base() + '/api',
      method: 'POST',
      header: { 'Content-Type': 'application/json' },
      data: payload,
      timeout: 25000,
      success(res) {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          // 服务端已经做了海报规范化与 ok 字段，直接透传
          resolve({ result: res.data });
        } else {
          reject(new Error('http status ' + res.statusCode));
        }
      },
      fail(err) {
        // 把 wx.request 的原始 errMsg（request:fail url not in domain list 之类）
        // 翻成人话。最常见的坑就是「本机上 server 没跑」——以前一律显示
        // 「拿不到价格」，看着像数据源挂了，其实只是 127.0.0.1:3000 没监听。
        const raw = (err && err.errMsg) || String(err);
        let msg = raw;
        if (/fail|timeout|refused|abort/i.test(raw)) {
          msg = API_BASE
            ? '连不上服务器（' + base() + '）。确认域名已加到小程序后台「服务器域名」的 request 白名单。'
            : '连不上本地服务（' + base() + '）。在电脑上先启动：cd server && node api.js';
        }
        reject(new Error(msg));
      },
    });
  });
}

// ---------------------------------------------------------------------------
// 统一出口
// ---------------------------------------------------------------------------
function request(params) {
  const payload = withCredentials(params);
  if (!cloudUsable()) return httpRequest(payload);
  return cloudRequest(payload);
}

/**
 * 把「要盯哪些票」同步给服务端。
 * ---------------------------------------------------------------------------
 * 为什么必须同步：小程序关掉就停了，微信又不允许小程序后台常驻。想让提醒
 * 真的进微信，只能由服务端一直查——服务端得先知道要查什么。
 * 所以每次任务列表变化（加/删/改）都往这里推一份，云端落库后定时器自己跑。
 *
 * 失败不抛错：同步不上不该挡住用户加监控这件正事，静默记一笔即可。
 */
function pushSync(tasks, pushOn) {
  return request({ action: 'pushSync', tasks: tasks || [], pushOn: !!pushOn }).catch(() => null);
}

/** 服务端现在盯得怎么样（任务数 / 上次查询 / 额度 / 是不是云端在跑） */
function pushStatus() {
  return request({ action: 'pushStatus' });
}

/** 立刻发一条测试消息，验证手机上到底能不能收到 */
function pushTest(title) {
  return request({ action: 'pushTest', title: title || '推送测试' });
}

/**
 * 用户授权成功一次 = 云端多一次下发机会。
 * 不记账的话云函数那边 quota 永远是 0，表现是「明明点了允许，出事却收不到」。
 */
function grantSub(n) {
  return request({ action: 'grantSub', n: Math.max(1, Number(n) || 1) }).catch(() => null);
}

/**
 * 外部提醒通道（企业微信机器人 / Server酱 / ntfy）。
 * 这三个不占微信订阅额度、不限条数，是「有变化就提醒」真正的底座。
 * 前端只需要一个输入框：粘什么地址都行，云函数自己认格式。
 */
function extGet() {
  return request({ action: 'extGet' });
}

function extSet(target) {
  return request({ action: 'extSet', target: String(target || '') });
}

function extClear() {
  return request({ action: 'extClear' });
}

/**
 * 确认当前用户身份。
 * 云端不需要 wx.login 换 openid（云函数从上下文直接拿），
 * 所以这里只是「能不能拿到 openid」的自检；HTTP 通道才真的走 code 换。
 */
function bindOpenId() {
  if (cloudUsable()) {
    return request({ action: 'whoami' }).then((res) => {
      const r = (res && res.result) || {};
      return { result: { ok: !!r.openid, openid: r.openid || '' } };
    });
  }
  return new Promise((resolve, reject) => {
    wx.login({
      success: (res) => {
        if (!res.code) {
          reject(new Error('wx.login 没返回 code，检查 AppID 是否填对'));
          return;
        }
        request({ action: 'login', code: res.code }).then(resolve, reject);
      },
      fail: (err) => reject(new Error('wx.login 失败：' + (err.errMsg || '未知'))),
    });
  });
}

// ---------------------------------------------------------------------------
// 社区
// ---------------------------------------------------------------------------
/** 帖子列表。opts: { keyword, type, offset, limit, mine } */
function listPosts(opts) {
  const o = opts || {};
  return request({
    action: 'listPosts',
    keyword: o.keyword || '',
    type: o.type || '',
    offset: o.offset || 0,
    limit: o.limit || 20,
    mine: !!o.mine,
  });
}

/** 发帖。post: { content, tags, type, sticker, nickname, avatar } */
function createPost(post) {
  const p = post || {};
  return request({
    action: 'createPost',
    content: p.content || '',
    tags: p.tags || [],
    type: p.type || '',
    sticker: p.sticker || '',
    nickname: p.nickname || '',
    avatar: p.avatar || '',
  });
}

/** 删自己的帖子（云端按 openid 判权） */
function delPost(id) {
  return request({ action: 'postDel', id: id || '' });
}

module.exports = {
  request,
  base,
  pushSync,
  pushStatus,
  pushTest,
  grantSub,
  extGet,
  extSet,
  extClear,
  bindOpenId,
  listPosts,
  createPost,
  delPost,
  FWD_TOKEN_KEY,
  DM_COOKIE_KEY,
};
