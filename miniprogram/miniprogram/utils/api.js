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
function request(params) {
  return new Promise((resolve, reject) => {
    wx.request({
      url: base() + '/api',
      method: 'POST',
      header: { 'Content-Type': 'application/json' },
      data: params,
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
        reject(new Error((err && err.errMsg) || String(err)));
      },
    });
  });
}

module.exports = { request, base };
