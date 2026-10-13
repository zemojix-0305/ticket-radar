// 「场次」页：搜名字 → 点一下就开始盯。
//
// 为什么不用微信小程序的默认搜索框：它只能搜本页内的东西，
// 这里要打的是平台那边的关键词，得自己收键盘 + 防抖。
//
// 为什么不给「热门」列表：热门是个会过期的硬编码榜，刷出来一堆没人关心的
// 场次反而挤占注意力。不如空着，让用户一上来就知道该打什么字。

const HISTORY_KEY = 'radar_search_history';
const CREDIT_KEY = 'wx_subscribe_credit';

// 平台不给彩色。taste-skill 的红线："色彩是稀缺资源，全页只留一个 accent"。
// 平台辨识靠文字，彩色只留给「有票/无票/异常」三个语义状态。
// （曾经给三个平台各配了一个低饱和色，反而成了全页最吵的地方。）
const PLATFORMS = [
  { key: 'damai', label: '大麦', linkOnly: true },
  { key: 'maoyan', label: '猫眼' },
  { key: 'moretickets', label: '摩天轮' },
  { key: 'piaoxingqiu', label: '票星球', linkOnly: true },
  { key: 'rail12306', label: '12306' },
];

Page({
  data: {
    kw: '',
    searching: false,
    rows: [],
    searched: false,
    fail: '',
    history: [],
    adding: '',
    platforms: PLATFORMS,
    platformIndex: 0,
    // 12306 用「出发地/目的地/日期」，不是关键词
    from: '',
    to: '',
    date: '',
  },

  onLoad() {
    this.setData({ history: wx.getStorageSync(HISTORY_KEY) || [] });
  },

  onInput(e) {
    this.setData({ kw: e.detail.value });
  },

  onFrom(e) {
    this.setData({ from: e.detail.value });
  },

  onTo(e) {
    this.setData({ to: e.detail.value });
  },

  onDate(e) {
    this.setData({ date: e.detail.value });
  },

  onSearch() {
    const p = PLATFORMS[this.data.platformIndex];
    if (p.key === 'rail12306') return this.searchRail();
    return this.searchKeyword(p);
  },

  searchKeyword(p) {
    const kw = (this.data.kw || '').trim();
    if (!kw) return;
    if (this.data.searching) return;

    this.setData({ searching: true, fail: '', rows: [], searched: true });

    wx.cloud
      .callFunction({
        name: 'radar',
        data: { action: 'find', keyword: kw, platform: p.key, limit: 20 },
      })
      .then((res) => {
        const r = res.result || {};
        if (!r.ok) throw new Error(r.error || '未知错误');
        const rows = (r.rows || []).map((row) => this.normalize(row, p));
        this.setData({ rows, searching: false });
        if (rows.length) this.pushHistory(kw + ':' + p.key);
      })
      .catch((e) => {
        this.setData({ searching: false, fail: String(e.message || e) });
      });
  },

  searchRail() {
    const from = (this.data.from || '').trim();
    const to = (this.data.to || '').trim();
    const date = this.data.date;
    if (!from || !to) return wx.showToast({ title: '填出发地和目的地', icon: 'none' });
    if (!date) return wx.showToast({ title: '选乘车日期', icon: 'none' });
    if (this.data.searching) return;

    this.setData({ searching: true, fail: '', rows: [], searched: true });
    wx.cloud
      .callFunction({
        name: 'radar',
        data: { action: 'find', keyword: from + ' ' + to, platform: 'rail12306', date, limit: 20 },
      })
      .then((res) => {
        const r = res.result || {};
        if (!r.ok) throw new Error(r.error || '未知错误');
        const p = PLATFORMS[2];
        const rows = (r.rows || []).map((row) => this.normalize(row, p));
        this.setData({ rows, searching: false });
        if (rows.length) this.pushHistory(from + ' ' + to + ':rail12306');
      })
      .catch((e) => {
        this.setData({ searching: false, fail: String(e.message || e) });
      });
  },

  normalize(row, platform) {
    const id = row.tourId || row.performanceId || row.id || '';
    return {
      id: String(id),
      platform: platform.key,
      platformLabel: platform.label,
      name: row.name || row.title || row.showName || '未命名演出',
      city: row.city || row.location || '',
      venue: row.venue || '',
      poster: row.poster || row.imgUrl || row.verticalPic || '',
      price: row.price || '',
      dateRange: row.dateRange || row.showDate || row.showTimeRange || '',
      status: row.status || '',
      // 12306 专有
      route: row.route || '',
    };
  },

  onPlatform(e) {
    const idx = Number(e.currentTarget.dataset.idx);
    this.setData({ platformIndex: idx, rows: [], searched: false, fail: '' });
  },

  pushHistory(kw) {
    const h = [kw].concat(this.data.history.filter((x) => x !== kw)).slice(0, 8);
    wx.setStorageSync(HISTORY_KEY, h);
    this.setData({ history: h });
  },

  useHistory(e) {
    const raw = e.currentTarget.dataset.kw || '';
    const parts = raw.split(':');
    const kw = parts[0];
    const platformKey = parts[1];
    let platformIndex = this.data.platformIndex;
    if (platformKey) {
      const idx = PLATFORMS.findIndex((p) => p.key === platformKey);
      if (idx >= 0) platformIndex = idx;
    }
    if (platformKey === 'rail12306') {
      // 12306 历史存的是「广州南 长沙南」，拆回出发地/目的地
      const seg = kw.split(/\s+/).filter(Boolean);
      this.setData({ platformIndex, from: seg[0] || '', to: seg[1] || '' });
      if (!this.data.date) {
        const d = new Date(Date.now() + 86400000);
        this.setData({ date: d.toISOString().slice(0, 10) });
      }
      this.searchRail();
      return;
    }
    this.setData({ kw, platformIndex });
    this.searchKeyword(PLATFORMS[platformIndex]);
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
    const id = row.platform + '-' + String(row.id).replace(/[^a-zA-Z0-9]/g, '').slice(-12);
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

    let adapter = row.platform;
    let params = {};
    let name = row.name;
    if (row.platform === 'moretickets') {
      params = { tour_id: row.id };
    } else if (row.platform === 'maoyan') {
      params = { performance_id: row.id };
    } else if (row.platform === 'rail12306') {
      // 12306 监控必须带上出发地/目的地/日期，不然轮询时不知道查哪条线
      params = { from: this.data.from, to: this.data.to, date: this.data.date };
      name = row.name || (this.data.from + ' → ' + this.data.to);
    } else {
      wx.showToast({ title: '不支持该平台', icon: 'none' });
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
          adapter,
          name,
          params,
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
