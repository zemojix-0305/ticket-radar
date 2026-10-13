// 「场次」页：搜名字 → 点一下就开始盯。
//
// 为什么不用微信小程序的默认搜索框：它只能搜本页内的东西，
// 这里要打的是平台那边的关键词，得自己收键盘 + 防抖。
//
// 为什么不给「热门」列表：热门是个会过期的硬编码榜，刷出来一堆没人关心的
// 场次反而挤占注意力。不如空着，让用户一上来就知道该打什么字。

const HISTORY_KEY = 'radar_search_history';
const CREDIT_KEY = 'wx_subscribe_credit';

// vant 函数式反馈（纸感主题，见 app.wxss 的 van-* 覆盖）
import Toast from '/vant/toast/toast';
import Dialog from '/vant/dialog/dialog';

// 平台不给彩色。taste-skill 的红线："色彩是稀缺资源，全页只留一个 accent"。
// 平台辨识靠文字，彩色只留给「有票/无票/异常」三个语义状态。
// （曾经给三个平台各配了一个低饱和色，反而成了全页最吵的地方。）
// 平台不给彩色。极简风全页只有一个钴蓝 accent。
// 平台辨识靠文字；彩色只留给「有票/无票/异常」语义状态。
// 猫眼放第一位：它是唯一有热门列表的平台，进页面就有内容可逛。
const PLATFORMS = [
  { key: 'rail12306', label: '12306' },
  { key: 'maoyan', label: '猫眼' },
  { key: 'moretickets', label: '摩天轮' },
  { key: 'damai', label: '大麦' },
  { key: 'bilibili', label: 'B站' },
  { key: 'piaoxingqiu', label: '票星球', linkOnly: true },
];

// 使用教程 / 常见问题（第十五轮）：新用户第一屏就能看懂怎么用。
// 文案口径和云函数能力严格对齐——不写做不到的功能。
const FAQ_LIST = [
  {
    q: '怎么搜演出？',
    a: '顶部先选平台（猫眼 / 摩天轮 / 大麦 / B站都能直接打字搜），输入演出名或艺人名，点「搜索」。票星球不给小程序自动搜，需要粘贴它的分享链接。',
  },
  {
    q: '点「盯」之后发生了什么？',
    a: '云函数每 5 分钟查一次这个演出的状态并记入历史。你不用一直开着小程序，该推的时候会推。',
  },
  {
    q: '回流票是什么？',
    a: '显示「无票 / 售罄」之后又翻回「可售」——开售后回流、别人退票、临时加座都在这个窗口。监控到这种变化会第一时间推给你。',
  },
  {
    q: '为什么收不到提醒？',
    a: '微信规定：小程序每推一条消息，都要你事先「点过一次允许」来换。去「我的」页领订阅额度（一次领 5 条更省事），有额度才会推。',
  },
  {
    q: '支持哪些平台？',
    a: '猫眼、摩天轮、大麦、B站会员购、12306 余票、票星球（贴链接）。携程没有公开接口，暂不支持。',
  },
  {
    q: '数据多久刷新？',
    a: '监控 5 分钟一轮；首页热门每次进入刷新；搜索是实时的。',
  },
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
    // 热门演出（进页面就有内容逛，不用先搜）
    hot: [],
    hotLoading: false,
    hotFail: '',
    // 12306 用「出发地/目的地/日期」，不是关键词
    from: '',
    to: '',
    date: '',
    todayStr: '',
    tomorrowStr: '',
    // 使用教程 / 常见问题（默认收起，占一行不碍事）
    faqOpen: false,
    faqList: FAQ_LIST,
  },

  onLoad() {
    const fmt = (off) => new Date(Date.now() + off * 86400000).toISOString().slice(0, 10);
    this.setData({
      history: wx.getStorageSync(HISTORY_KEY) || [],
      todayStr: fmt(0),
      tomorrowStr: fmt(1),
      date: fmt(1),
    });
    // 进页面就拉热门，让第一屏有东西可逛
    this.loadHot();
  },

  onInput(e) {
    this.setData({ kw: e.detail.value });
  },

  // 使用教程 / 常见问题：折叠面板
  toggleFaq() {
    this.setData({ faqOpen: !this.data.faqOpen });
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
    const kw = (this.data.kw || '').trim();
    if (!kw) {
      // 搜索框为空 = 回到热门
      this.setData({ rows: [], searched: false, fail: '' });
      this.loadHot();
      return;
    }
    return this.searchKeyword(p);
  },

  /** 热门演出：只有猫眼能做（实测空关键词返回在售列表），其他平台给假数据是不诚实 */
  loadHot() {
    const p = PLATFORMS[this.data.platformIndex] || PLATFORMS[0];
    // 12306 是查两站之间余票，没有「热卖榜」
    if (p.key === 'rail12306') {
      this.setData({ hot: [], hotLoading: false, hotFail: '' });
      return;
    }
    if (this.data.hotLoading) return;
    this.setData({ hotLoading: true, hotFail: '' });
    wx.cloud
      .callFunction({
        name: 'radar',
        data: { action: 'find', keyword: '', platform: p.key, limit: 10 },
      })
      .then((res) => {
        const r = res.result || {};
        if (!r.ok) throw new Error(r.error || '未知错误');
        const rows = (r.rows || []).map((row) => this.normalize(row, p));
        this.setData({ hot: rows, hotLoading: false });
      })
      .catch((e) => {
        this.setData({ hotLoading: false, hotFail: String(e.message || e) });
      });
  },

  /** 点演出卡片 → 详情页（海报 + 场次/票档 + 盯这场） */
  openDetail(e) {
    const row = e.currentTarget.dataset.row;
    if (!row || !row.id) return;
    const q = [
      'platform=' + encodeURIComponent(row.platform),
      'id=' + encodeURIComponent(row.id),
      'name=' + encodeURIComponent(row.name || ''),
      'poster=' + encodeURIComponent(row.poster || ''),
      'city=' + encodeURIComponent(row.city || ''),
      'venue=' + encodeURIComponent(row.venue || ''),
      'price=' + encodeURIComponent(row.price || ''),
      'date=' + encodeURIComponent(row.dateRange || ''),
      'label=' + encodeURIComponent(row.platformLabel || ''),
    ].join('&');
    wx.navigateTo({ url: '/pages/detail/detail?' + q });
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
    if (!from || !to) { Toast('填出发地和目的地'); return; }
    if (!date) { Toast('选乘车日期'); return; }
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
        // 别用 PLATFORMS[下标] 硬编码——平台列表一重排就会标错平台名（踩过）
        const p = PLATFORMS.find((x) => x.key === 'rail12306');
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
      // 印章颜色：可买=茶绿，关闭=朱砂，其余=墨灰
      statusCls: /有票|在售|可售|可购|预售/.test(row.status || '')
        ? 'tag-ok'
        : (/售罄|不可购|停售/.test(row.status || '') ? 'tag-bad' : 'tag-none'),
      // 12306 专有
      route: row.route || '',
    };
  },

  onPlatform(e) {
    // 兼容两种触发：van-tabs 的 change（e.detail.index）与旧点击（dataset.idx）
    const idx = e.detail && typeof e.detail.index === 'number'
      ? e.detail.index
      : Number(e.currentTarget.dataset.idx);
    this.setData({ platformIndex: idx, rows: [], searched: false, fail: '', hot: [] });
    // 切平台就换一批热卖（每平台都有自己的榜单）
    this.loadHot();
  },

  /** 携程式交换出发地/目的地 */
  swapStations() {
    this.setData({ from: this.data.to, to: this.data.from });
  },

  /** 日期快捷：今天 / 明天 */
  quickDate(e) {
    const off = Number(e.currentTarget.dataset.off) || 0;
    const d = new Date(Date.now() + off * 86400000);
    this.setData({ date: d.toISOString().slice(0, 10) });
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
    Toast('已清空');
  },

  clearAll() {
    this.setData({ kw: '', rows: [], searched: false, fail: '' });
    this.loadHot();
  },

  // 点结果卡片 = 建监控。这是最短路径：搜到、看一眼、点了就走。
  add(e) {
    const row = e.currentTarget.dataset.row;
    const id = row.platform + '-' + String(row.id).replace(/[^a-zA-Z0-9]/g, '').slice(-12);
    const credit = wx.getStorageSync(CREDIT_KEY) || 0;

    if (credit <= 0) {
      Dialog.confirm({
        title: '先领一下推送额度',
        message: '微信的订阅消息是「点一次允许换一条推送」。现在额度是 0，加了监控也收不到提醒。',
        confirmButtonText: '去领额度',
      })
        .then(() => {
          wx.switchTab({ url: '/pages/mine/mine' });
        })
        .catch(() => {});
      return;
    }

    let adapter = row.platform;
    let params = {};
    let name = row.name;
    if (row.platform === 'moretickets') {
      params = { tour_id: row.id };
    } else if (row.platform === 'maoyan') {
      params = { performance_id: row.id };
    } else if (row.platform === 'damai') {
      // 大麦：搜出来的是 itemId，轮询走详情页 SSR
      params = { item_id: row.id };
    } else if (row.platform === 'bilibili') {
      // 会员购：project_id，轮询走 getV2 接口
      params = { project_id: row.id };
    } else if (row.platform === 'piaoxingqiu') {
      // 票星球：show_id（bizShowId），轮询走 cyy_gatewayapi
      params = { show_id: row.id };
    } else if (row.platform === 'rail12306') {
      // 12306 监控必须带上出发地/目的地/日期，不然轮询时不知道查哪条线
      params = { from: this.data.from, to: this.data.to, date: this.data.date };
      name = row.name || (this.data.from + ' → ' + this.data.to);
    } else {
      Toast('不支持该平台');
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
        Toast({ type: 'success', message: '已开始盯' });
        this.setData({ adding: '' });
        setTimeout(() => wx.switchTab({ url: '/pages/monitor/monitor' }), 800);
      })
      .catch((e) => {
        this.setData({ adding: '' });
        Dialog.alert({ title: '添加失败', message: String(e.message || e) });
      });
  },
});
