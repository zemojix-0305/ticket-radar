/**
 * ticket-radar 数据层封装（第二十轮：无状态 HTTP）
 * ---------------------------------------------------------------------------
 * 之前所有页面用 wx.cloud.callFunction('radar', {action,...}) 调云函数；
 * 迁移到本地 HTTP 服务后，这个模块统一做代理，业务代码不用变调用接口。
 *
 * 环境变量（Vercel 免费部署后填，本地跑用 dev 回退）：
 *   API_BASE  - 生产环境填真实域名，例如 https://ticket-radar-api.vercel.app
 *   本地开发  - 留空或 undef，自动回退 http://localhost:3000
 *
 * 小程序要配 request 合法域名：在后台「开发设置 → 服务器域名」加上面那个域名
 * （不加的话模拟器里能通，真机预览会被拦截——这是小程序的硬约束）。
 */

const API_BASE = typeof ENV !== 'undefined' ? ENV : '';

function base() {
  return API_BASE || 'http://localhost:3000';
}

/**
 * 统一请求包装：wx.request 返回和 callFunction 一样的结构 { result: ... }
 * 为了和业务代码最小改动对接，这里直接透传 res.data（即服务端 JSON body）
 * 作为 result 字段。
 */
// 纷玩岛 token 存本地（「我的」页填写）。全局随每个请求带上，服务端
// 只在纷玩岛相关的 find/detail/poll 里用它，别的平台无视这个字段。
const FWD_TOKEN_KEY = 'radar_fwd_token';
// 大麦 Cookie（可选）：登录大麦网页版后粘 Cookie 进来，过 mtop 风控最稳。
// 同样全局随请求带上，服务端只在大麦相关调用里用。
const DM_COOKIE_KEY = 'radar_dm_cookie';
// 猫眼登录 token（可选）：猫眼小程序的用户凭证，票档级库存（remainingStock）
// 只有登录通道有。服务端只在猫眼相关调用里用。
const MY_TOKEN_KEY = 'radar_my_token';

function request(params) {
  return new Promise((resolve, reject) => {
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

/**
 * 把「要盯哪些票」同步给服务端。
 * ---------------------------------------------------------------------------
 * 为什么必须同步：小程序关掉就停了，微信又不允许小程序后台常驻。想让提醒
 * 真的进微信，只能由服务端一直查——服务端得先知道要查什么。
 * 所以每次任务列表变化（加/删/改）都往这里推一份，服务端落盘后自己跑。
 *
 * 失败不抛错：同步不上不该挡住用户加监控这件正事，静默记一笔即可。
 */
function pushSync(tasks, pushOn) {
  return request({ action: 'pushSync', tasks: tasks || [], pushOn: !!pushOn }).catch(() => null);
}

/** 服务端现在盯得怎么样（任务数 / 上次查询 / 上次报错 / openid 绑没绑） */
function pushStatus() {
  return request({ action: 'pushStatus' });
}

/** 立刻发一条测试消息，验证手机上到底能不能收到 */
function pushTest(title) {
  return request({ action: 'pushTest', title: title || '推送测试' });
}

/**
 * 用 wx.login 的 code 换 openid 并存到服务端。
 * 用户全程无感：不弹窗、不需要复制粘贴。
 */
function bindOpenId() {
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

module.exports = {
  request, base, pushSync, pushStatus, pushTest, bindOpenId,
  FWD_TOKEN_KEY, DM_COOKIE_KEY,
};
