// 机票页（第 X 轮）：价格浮动 + 特价发现
// ---------------------------------------------------------------------------
// 数据源是同程「价格日历」（后端 flight.js，免 Cookie 免签名）。
// 产品口径：
//   · 不假装能列出「当天全部航班」——匿名拿不到，硬凑就是假数据；
//   · 给的是「这条航线未来 N 天每天的最低票价」：正好回答「哪天飞最便宜」，
//     也就是价格浮动 + 特价监控真正要的东西；
//   · 每条都带航班号与起降时刻，用户能直接判断这班要不要。
const { request } = require('../../utils/api');

const WATCH_KEY = 'radar_flight_watch';
const CACHE_KEY = 'radar_flight_cache';

function ymd(d) {
  const t = d instanceof Date ? d : new Date(d);
  const p = (n) => (n < 10 ? '0' + n : '' + n);
  return t.getFullYear() + '-' + p(t.getMonth() + 1) + '-' + p(t.getDate());
}

Page({
  data: {
    today: '',
    from: null,
    to: null,
    startDate: '',
    days: 30,
    loading: false,
    res: null,
    bars: [],
    deals: [],
    savePct: 0,
    err: '',
    // 城市选择弹层
    cityOpen: false,
    citySide: 'from',
    cityKw: '',
    cityList: [],
    // 监控（本地）
    watches: [],
  },

  onLoad() {
    const t = new Date();
    const start = ymd(new Date(t.getTime() + 86400000));
    this.setData({
      today: ymd(t),
      startDate: start,
      watches: wx.getStorageSync(WATCH_KEY) || [],
    });
    // 恢复上次航线，省得每次重选
    const last = wx.getStorageSync(CACHE_KEY);
    if (last && last.from && last.to) {
      this.setData({ from: last.from, to: last.to });
    } else {
      // 首次进：给一组最常见航线，直接能看
      this.setData({
        from: { code: 'SHA', name: '上海' },
        to: { code: 'BJS', name: '北京' },
      });
    }
  },

  onShow() {
    this.setData({ watches: wx.getStorageSync(WATCH_KEY) || [] });
  },

  // ---------------- 城市选择 ----------------
  openCity(e) {
    const side = e.currentTarget.dataset.side || 'from';
    this.setData({ cityOpen: true, citySide: side, cityKw: '' });
    this.loadCities('');
  },

  closeCity() {
    this.setData({ cityOpen: false });
  },

  onCityKw(e) {
    const kw = e.detail.value || '';
    this.setData({ cityKw: kw });
    this.loadCities(kw);
  },

  loadCities(kw) {
    request({ action: 'flightCities', keyword: kw })
      .then((r) => {
        this.setData({ cityList: (r && r.cities) || [] });
      })
      .catch(() => {
        this.setData({ cityList: [] });
      });
  },

  pickCity(e) {
    const c = e.currentTarget.dataset.city;
    if (!c) return;
    const key = this.data.citySide;
    const patch = {};
    patch[key] = { code: c.code, name: c.name };
    patch.cityOpen = false;
    this.setData(patch);
    // 城市一变，旧结果立刻失效，避免"上海→北京"的结果挂在"上海→广州"下面
    this.setData({ res: null, bars: [], deals: [], err: '' });
  },

  swap() {
    const f = this.data.from;
    this.setData({ from: this.data.to, to: f, res: null, bars: [], deals: [], err: '' });
  },

  onDate(e) {
    this.setData({ startDate: e.detail.value, res: null, bars: [], deals: [], err: '' });
  },

  onDays(e) {
    this.setData({ days: Number(e.currentTarget.dataset.d) || 30 });
  },

  // ---------------- 查询 ----------------
  query() {
    const { from, to, startDate, days, loading } = this.data;
    if (loading) return;
    if (!from || !to) {
      wx.showToast({ title: '先选出发和到达城市', icon: 'none' });
      return;
    }
    if (from.code === to.code) {
      wx.showToast({ title: '出发和到达不能一样', icon: 'none' });
      return;
    }
    this.setData({ loading: true, err: '' });
    request({ action: 'flight', from: from.code, to: to.code, date: startDate, days })
      .then((r) => {
        if (!r || !r.ok) throw new Error((r && r.error) || '拿不到价格');
        this.applyRes(r);
        wx.setStorageSync(CACHE_KEY, {
          from: { code: r.fromCode, name: from.name },
          to: { code: r.toCode, name: to.name },
        });
        this.setData({ loading: false });
      })
      .catch((e) => {
        this.setData({
          loading: false,
          res: null,
          bars: [],
          deals: [],
          err: (e && e.message) || '网络错误，稍后再试',
        });
      });
  },

  applyRes(r) {
    const rows = r.rows || [];
    const max = Math.max.apply(null, rows.map((x) => x.price).concat([1]));
    const min = r.stats.min;
    // 柱高：以最高价为 96px 基准，最低价至少留 8px 可见
    const bars = rows.map((x) => ({
      date: x.date,
      dd: x.date.slice(5),
      price: x.price,
      h: Math.max(8, Math.round((x.price / max) * 96)),
      low: x.price === min,
    }));
    // 捡漏日：低于均价 20% 的，按价格升序，最多 6 条
    const avg = r.stats.avg;
    const deals = rows
      .filter((x) => x.price <= avg * 0.8)
      .sort((a, b) => a.price - b.price)
      .slice(0, 6)
      .map((x) =>
        Object.assign({}, x, {
          save: Math.round(((avg - x.price) / avg) * 100),
        }),
      );
    this.setData({
      res: r,
      bars,
      deals,
      savePct: Math.round(((avg - min) / avg) * 100),
    });
  },

  // ---------------- 监控（本地航线簿）----------------
  addWatch() {
    const { from, to, res } = this.data;
    if (!res) {
      wx.showToast({ title: '先查一次价格', icon: 'none' });
      return;
    }
    const list = (wx.getStorageSync(WATCH_KEY) || []).slice();
    const code = from.code + '-' + to.code;
    if (list.filter((x) => x.id === code).length) {
      wx.showToast({ title: '这条已经在盯了', icon: 'none' });
      return;
    }
    list.unshift({
      id: code,
      from: from.code,
      to: to.code,
      label: (res.fromName || from.name) + ' → ' + (res.toName || to.name),
      best: res.stats.min,
      bestDate: res.stats.cheapest ? res.stats.cheapest.date : '',
      at: Date.now(),
    });
    wx.setStorageSync(WATCH_KEY, list);
    this.setData({ watches: list });
    wx.showToast({ title: '已加入监控', icon: 'none' });
  },

  removeWatch(e) {
    const id = e.currentTarget.dataset.id;
    const list = (wx.getStorageSync(WATCH_KEY) || []).filter((x) => x.id !== id);
    wx.setStorageSync(WATCH_KEY, list);
    this.setData({ watches: list });
  },

  useWatch(e) {
    const w = e.currentTarget.dataset.w;
    if (!w) return;
    this.setData({
      from: { code: w.from, name: w.label.split(' → ')[0] },
      to: { code: w.to, name: w.label.split(' → ')[1] },
      res: null,
      bars: [],
      deals: [],
    });
    this.query();
  },
});
