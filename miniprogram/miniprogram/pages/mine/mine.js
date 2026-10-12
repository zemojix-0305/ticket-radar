// 「我的」页：拿登录 code（换 openid 用）+ 申请订阅通知额度。
//
// 为什么页面上显示的是 code 而不是 openid：换 openid 需要 AppSecret，
// 而 AppSecret 绝不能打进小程序包里（任何人反编译都能拿到）。
// 所以这里只负责把 code 显示出来，由用户在电脑上跑 `radar wxbind <code>` 去换。
// 这样既不需要开通云开发，也不泄露密钥。
const TMPL_ID = 'YnMbqU_UqlWb1eUOSMOp1RwxfGJozlrlXYtSaXFnsF4';

Page({
  data: {
    code: '',
    err: '',
    authorized: false,
    authMsg: '',
  },

  onLoad() {
    this.refreshCode();
  },

  onPullDownRefresh() {
    this.refreshCode(() => wx.stopPullDownRefresh());
  },

  // 每次点都拿一个新的 code：它只能用一次、5 分钟过期
  refreshCode(done) {
    wx.login({
      success: (res) => {
        if (!res.code) {
          this.setData({ err: 'wx.login 没返回 code，检查 AppID 是否填对' });
          if (done) done();
          return;
        }
        this.setData({ code: res.code, err: '' });
        if (done) done();
      },
      fail: (err) => {
        this.setData({ err: `wx.login 失败：${err.errMsg || '未知错误'}` });
        if (done) done();
      },
    });
  },

  copyCode() {
    if (!this.data.code) return;
    wx.setClipboardData({
      data: this.data.code,
      success: () => wx.showToast({ title: '已复制', icon: 'success' }),
    });
  },

  // 一次性订阅：点一次「允许」= 换来一条推送额度。
  // 用完再发会报 43101，所以额度见底时回来再点一次就行。
  requestSubscribe() {
    if (!wx.requestSubscribeMessage) {
      this.setData({ authMsg: '微信版本过低，不支持订阅消息' });
      return;
    }
    wx.requestSubscribeMessage({
      tmplIds: [TMPL_ID],
      success: (res) => {
        const state = res[TMPL_ID];
        if (state === 'accept') {
          this.setData({ authorized: true, authMsg: '已授权，可以收到 1 条推送了' });
        } else if (state === 'reject') {
          this.setData({ authMsg: '你拒绝了这次授权，不会收到推送' });
        } else if (state === 'ban') {
          this.setData({ authMsg: '该模板已被后台禁用' });
        } else {
          this.setData({ authMsg: `授权结果：${state}` });
        }
      },
      fail: (err) => {
        this.setData({ authMsg: `拉起授权失败：${err.errMsg || '未知错误'}` });
      },
    });
  },
});
