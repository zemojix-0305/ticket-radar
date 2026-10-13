// 「场次」页：搜名字 → 点一下就开始盯。
//
// 为什么不用微信小程序的默认搜索框：它只能搜本页内的东西，
// 这里要打的是平台那边的关键词，得自己收键盘 + 防抖。
//
// 为什么不给「热门」列表：热门是个会过期的硬编码榜，刷出来一堆没人关心的
// 场次反而挤占注意力。不如空着，让用户一上来就知道该打什么字。

const HISTORY_KEY = 'radar_search_history';
const CREDIT_KEY = 'wx_subscribe_credit';

Page({
  data: {
    kw: '',
    searching: false,
    rows: [],
    searched: false,
    fail: '',
    history: [],
    adding: '',
  },

  onLoad() {
    this.setData({ history: wx.getStorageSync(HISTORY_KEY) || [] });
  },

  onInput(e) {
    this.setData({ kw: e.detail.value });
  },

  onSearch() {
    const kw = (this.data.kw || '').trim();
    if (!kw) return;
    if (this.data.searching) return;

    this.setData({ searching: true, fail: '', rows: [], searched: true });

    wx.cloud
      .callFunction({ name: 'radar', data: { action: 'find', keyword: kw, limit: 20 } })
      .then((res) => {
        const r = res.result || {};
        if (!r.ok) throw new Error(r.error || '未知错误');
        this.setData({ rows: r.rows || [], searching: false });
        if ((r.rows || []).length) this.pushHistory(kw);
      })
      .catch((e) => {
        this.setData({ searching: false, fail: String(e.message || e) });
      });
  },

  pushHistory(kw) {
    const h = [kw].concat(this.data.history.filter((x) => x !== kw)).slice(0, 8);
    wx.setStorageSync(HISTORY_KEY, h);
    this.setData({ history: h });
  },

  useHistory(e) {
    this.setData({ kw: e.currentTarget.dataset.kw });
    this.onSearch();
  },

  clearHistory() {
    wx.removeStorageSync(HISTORY_KEY);
    this.setData({ history: [] });
  },

  clearAll() {
    this.setData({ kw: '', rows: [], searched: false, fail: '' });
  },

  // 点结果卡片 = 建监控。这是最短路径：搜到、看一眼、点了就走。
  add(e) {
    const row = e.currentTarget.dataset.row;
    const id = 'moretickets-' + String(row.tourId).slice(-12);
    const credit = wx.getStorageSync(CREDIT_KEY) || 0;

    if (credit <= 0) {
      wx.showModal({
        title: '先领一下推送额度',
        content: '微信的订阅消息是「点一次允许换一条推送」。现在额度是 0，加了监控也收不到提醒。',
        confirmText: '去领额度',
        success: (r) => {
          if (r.confirm) wx.switchTab({ url: '/pages/mine/mine' });
        },
      });
      return;
    }

    this.setData({ adding: id });
    wx.cloud
      .database()
      .collection('radar_tasks')
      .doc(id)
      .set({
        data: {
          _id: id,
          adapter: 'moretickets',
          name: row.name,
          params: { tour_id: row.tourId },
          enabled: true,
          poster: row.poster || '',
        },
      })
      .then(() => {
        wx.showToast({ title: '已开始盯', icon: 'success' });
        this.setData({ adding: '' });
        setTimeout(() => wx.switchTab({ url: '/pages/monitor/monitor' }), 800);
      })
      .catch((e) => {
        this.setData({ adding: '' });
        wx.showModal({ title: '添加失败', content: String(e.message || e), showCancel: false });
      });
  },
});
