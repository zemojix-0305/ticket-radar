// 监控页：首屏回答三个问题——我在盯几个、现在有没有票、上次什么时候查的。
//
// 两条原则（跟 Python 那边保持一致）：
//   1. 结论先行——卡片第一眼告诉你「有票还是没票」，不是先列车次；
//   2. 不认识的状态不猜——云函数报什么就显示什么，不替它美化。
//
// 额度记账放在 storage：微信一次性订阅的额度后台查不到，
// 只能让用户自己看得见「我还剩几条」，否则推送突然不响会毫无头绪。
const PLATFORMS = [
  { key: 'maoyan', label: '猫眼', hint: '演出的 performance_id，在猫眼演出页地址栏里', ph: '例：509862' },
  { key: 'moretickets', label: '摩天轮', hint: '巡演的 tour_id，在摩天轮演出页地址栏里', ph: '例：67357b7a…' },
  { key: 'rail12306', label: '12306', hint: '填两个站名，中间空格隔开就行', ph: '广州南 长沙南' },
];

const LABEL = {
  maoyan: '猫眼',
  moretickets: '摩天轮',
  rail12306: '12306',
  amadeus: '机票',
  fenwandao: '大麦',
};

const CREDIT_KEY = 'wx_subscribe_credit';

// 把时间戳变成「12 分钟前查过」。
// 用户关心的是「这数据新不新」，不是具体几点查的。
function relTime(iso) {
  if (!iso) return '还没查过';
  const t = Date.parse(String(iso).replace(' ', 'T'));
  if (!t || isNaN(t)) return '';
  const m = Math.floor((Date.now() - t) / 60000);
  if (m < 1) return '刚刚查过';
  if (m < 60) return m + ' 分钟前查过';
  const h = Math.floor(m / 60);
  if (h < 24) return h + ' 小时前查过';
  return Math.floor(h / 24) + ' 天前查过';
}

Page({
  data: {
    states: [],
    okCount: 0,
    credit: 0,
    loading: true,
    fail: '',
    expanded: {},
    showAdd: false,
    platforms: PLATFORMS,
    platformIndex: 0,
    form: { name: '', id: '', city: '', date: '' },
  },

  onLoad() {
    this.refresh();
  },

  onShow() {
    // 从「我的」页领完额度回来要能看到数字变了
    this.setData({ credit: wx.getStorageSync(CREDIT_KEY) || 0 });
  },

  onPullDownRefresh() {
    this.refresh().then(() => wx.stopPullDownRefresh());
  },

  refresh() {
    const self = this;
    self.setData({ loading: true, fail: '' });
    return wx.cloud
      .callFunction({ name: 'radar', data: { action: 'query' } })
      .then((res) => {
        const r = res.result || {};
        if (!r.ok) throw new Error(r.error || '未知错误');
        const list = (r.states || []).map((s) => self.decorate(s));
        const okCount = list.filter((s) => s.ok && s.available > 0).length;
        self.setData({
          states: list,
          okCount,
          credit: wx.getStorageSync(CREDIT_KEY) || 0,
          loading: false,
        });
      })
      .catch((e) => {
        self.setData({
          loading: false,
          fail:
            String(e.message || e) +
            '\n\n三种常见原因：\n' +
            '· 没开通云开发，或 app.js 里的环境 ID 是空的\n' +
            '· 云函数 radar 没部署（去云开发控制台看看有没有这个函数）\n' +
            '· 云函数 IP 被平台风控了（这种只影响抓取，不影响小程序本身）',
        });
      });
  },

  // 把云函数的原始记录整理成「能给人看」的样子
  decorate(s) {
    const items = (s.items || []).filter((i) => i.available);
    const total = (s.items || []).length;
    const avail = typeof s.available === 'number' ? s.available : items.length;

    let head, cls, bar;
    if (!s.ok) {
      head = '抓取失败';
      cls = 'tag-bad';
      bar = 'task-bar-bad';
    } else if (!total) {
      head = '暂未开售';
      cls = 'tag-none';
      bar = 'task-bar-none';
    } else if (avail > 0) {
      head = '有票 ' + avail;
      cls = 'tag-ok';
      bar = 'task-bar-ok';
    } else {
      head = '无票';
      cls = 'tag-none';
      bar = 'task-bar-none';
    }

    const first = (s.items || [])[0] || {};
    return Object.assign({}, s, {
      head,
      headClass: cls,
      barClass: bar,
      platform: LABEL[s.platform] || s.platform,
      sub2: first.when || '',
      total,
      available: avail,
      relTime: relTime(s.checked_at),
      show: items.slice(0, 5),          // 明细有上限：一次放票 39 趟不能全铺开
      more: items.length > 5 ? items.length - 5 : 0,
    });
  },

  toggle(e) {
    const id = e.currentTarget.dataset.id;
    const ex = Object.assign({}, this.data.expanded);
    ex[id] = !ex[id];
    this.setData({ expanded: ex });
  },

  goShows() {
    wx.switchTab({ url: '/pages/shows/shows' });
  },

  // --- 添加任务 ---------------------------------------------------------
  openAdd() { this.setData({ showAdd: true }); },

  // 跳「我的」页：领订阅额度
  goMine() {
    wx.switchTab({ url: '/pages/mine/mine' });
  },
  closeAdd() { this.setData({ showAdd: false }); },
  onPlatform(e) { this.setData({ platformIndex: Number(e.detail.value) }); },
  onInput(e) {
    const k = e.currentTarget.dataset.k;
    this.setData({ ['form.' + k]: e.detail.value });
  },
  onDate(e) { this.setData({ 'form.date': e.detail.value }); },

  submitAdd() {
    const self = this;
    const p = PLATFORMS[this.data.platformIndex];
    const f = this.data.form;
    if (!f.id || !f.id.trim()) return wx.showToast({ title: '先把上面那栏填了', icon: 'none' });

    let params = {};
    if (p.key === 'maoyan') {
      params = { performance_id: f.id.trim() };
    } else if (p.key === 'moretickets') {
      params = { tour_id: f.id.trim() };
      if (f.city) params.city = f.city;
    } else {
      const seg = f.id.trim().split(/\s*[-到>]\s*|\s+/).filter(Boolean);
      if (seg.length < 2) return wx.showToast({ title: '要填两个站名', icon: 'none' });
      if (!f.date) return wx.showToast({ title: '12306 要选乘车日期', icon: 'none' });
      params = { from: seg[0], to: seg[1], date: f.date };
    }

    const id = p.key + '-' + f.id.replace(/[^0-9a-zA-Z]/g, '').slice(-12);
    wx.cloud
      .database()
      .collection('radar_tasks')
      .doc(id)
      .set({
        data: {
          _id: id,
          adapter: p.key,
          name:
            f.name ||
            (p.key === 'rail12306' ? params.from + ' → ' + params.to : p.label + ' 监控'),
          params: params,
          enabled: true,
        },
      })
      .then(() => {
        wx.showToast({ title: '已开始盯，稍等出结果' });
        self.setData({ showAdd: false, form: { name: '', id: '', city: '', date: '' } });
        setTimeout(() => self.refresh(), 2500);
      })
      .catch((e) =>
        wx.showModal({ title: '添加失败', content: String(e.message || e), showCancel: false })
      );
  },
});
