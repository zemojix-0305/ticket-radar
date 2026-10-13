// 「我的」页：拿登录 code（换 openid 用）+ 申请订阅通知额度。
//
// 为什么页面上显示的是 code 而不是 openid：换 openid 需要 AppSecret，
// 而 AppSecret 绝不能打进小程序包里（任何人反编译都能拿到）。
// 所以这里只负责把 code 显示出来，由用户在电脑上跑 `radar wxbind <code>` 去换。
// 这样既不需要开通云开发，也不泄露密钥。
const TMPL_ID = 'YnMbqU_UqlWb1eUOSMOp1RwxfGJozlrlXYtSaXFnsF4';

// 本地攒的额度条数。真正在微信后台存着多少条我们查不到（要 AppSecret），
// 所以这里只是给用户一个「我大概领过几次」的记账本，够用了。
const CREDIT_KEY = 'wx_subscribe_credit';

Page({
  data: {
    code: '',
    err: '',
    authorized: false,
    authMsg: '',
    credit: 0,
    batching: false,
    batchGot: 0,
    batchTotal: 0,
  },

  onLoad() {
    this.refreshCode();
    this.setData({ credit: wx.getStorageSync(CREDIT_KEY) || 0 });
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
          const n = (wx.getStorageSync(CREDIT_KEY) || 0) + 1;
          wx.setStorageSync(CREDIT_KEY, n);
          this.setData({
            authorized: true,
            credit: n,
            authMsg: `已领到第 ${n} 条额度`,
          });
        } else if (state === 'reject') {
          this.setData({ authMsg: '你拒绝了这次授权，不会收到推送' });
        } else if (state === 'ban') {
          this.setData({ authMsg: '该模板已被后台禁用' });
        } else if (state === 'filter') {
          this.setData({ authMsg: '该模板被后台过滤了' });
        } else {
          this.setData({ authMsg: `授权结果：${state}` });
        }
      },
      fail: (err) => {
        this.setData({ authMsg: `拉起授权失败：${err.errMsg || '未知错误'}` });
      },
    });
  },

  // 连领 5 条：省得一次一次点。
  //
  // 为什么要排队而不是并发：wx.requestSubscribeMessage 同一时间只能弹一个窗，
  // 并发调用直接 fail。
  //
  // 为什么失败不立刻放弃：微信对连续调用有节流，间隔太短会被拒，
  // 但那只是「等一下再来」而不是「不能领」。所以失败后退避重试，
  // 真试不出来再让人手动点——批量只是省事，不该比手动更不可靠。
  requestSubscribeBatch() {
    if (this.data.batching) return;
    this.setData({ batching: true, authMsg: '正在领取…', batchGot: 0, batchTotal: 5 });

    let got = 0;
    let retry = 0;
    const total = 5;

    const done = (why) => {
      this.setData({
        batching: false,
        batchGot: 0,
        batchTotal: 0,
        authMsg:
          got > 0
            ? `${why}已领 ${got} 条，累计 ${this.data.credit} 条。剩下的手动点「再领一条额度」也一样。`
            : `${why}没领到，请点「允许通知」手动领一次。`,
      });
      if (got > 0) wx.showToast({ title: `已领 ${got} 条`, icon: 'success' });
    };

    const next = () => {
      if (got >= total) {
        done('');
        return;
      }
      // 失败重试用递增间隔（1s → 2s → 3s），三次还不行就交给人
      const wait = retry === 0 ? 1000 : retry * 1000;

      if (retry >= 3) {
        done('微信挡住了连领（模拟器里尤其容易这样）。');
        return;
      }

      wx.requestSubscribeMessage({
        tmplIds: [TMPL_ID],
        success: (res) => {
          if (res[TMPL_ID] === 'accept') {
            got += 1;
            retry = 0;
            const n = (wx.getStorageSync(CREDIT_KEY) || 0) + 1;
            wx.setStorageSync(CREDIT_KEY, n);
            this.setData({ authorized: true, credit: n, batchGot: got });
          } else {
            // 用户点了拒绝：重试也没用，直接收工
            retry = 3;
          }
          setTimeout(next, 1000);
        },
        fail: () => {
          retry += 1;
          setTimeout(next, wait);
        },
      });
    };

    next();
  },

  resetCredit() {
    wx.removeStorageSync(CREDIT_KEY);
    this.setData({ credit: 0, authMsg: '计数已清零（只是本地记账，不影响微信后台的真实额度）' });
  },
});
