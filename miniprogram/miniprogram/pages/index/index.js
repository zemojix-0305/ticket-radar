// 看板页：拉状态、展示、按需加任务
//
// 两条原则（跟 Python 那边保持一致）：
//   1. 结论先行——卡片第一眼告诉你「有票还是没票」，不是先列车次；
//   2. 不认识的状态不猜——云函数报什么就显示什么，不替它美化。
const PLATFORMS = [
  { key: 'maoyan', label: '猫眼', hint: 'performance_id，如 501675' },
  { key: 'moretickets', label: '摩天轮', hint: 'tour_id，如 6a2a300f941d1b00014a8828' },
  { key: 'rail12306', label: '12306', hint: '站码，如 IZQ / 站名如 北京南' },
];

Page({
  data: {
    states: [],
    at: '',
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
    // 云函数 poll 是定时器在跑；这里只在进来时主动查一次。
    // 不在前端触发轮询——小程序切后台就停，做不到常驻。
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
        self.setData({ states: list, at: r.at, loading: false });
      })
      .catch((e) => {
        self.setData({
          loading: false,
          fail:
            '拿不到数据：' +
            (e.message || e) +
            '\n\n如果是第一次用，去云开发控制台把 radar 云函数部署一下。',
        });
      });
  },

  // 把云函数的原始记录整理成「能给人看」的样子
  decorate(s) {
    const items = (s.items || []).filter((i) => i.available);
    const total = (s.items || []).length;
    let head, cls;
    if (!s.ok) {
      head = '抓取失败';
      cls = 'bad';
    } else if (!total) {
      head = '暂无数据';
      cls = 'dim';
    } else if (items.length) {
      head = '有票 ' + items.length + ' 条';
      cls = 'ok';
    } else {
      head = '暂无余票（' + total + ' 项全无票）';
      cls = 'dim';
    }
    return Object.assign({}, s, {
      head: head,
      headClass: cls,
      show: items.slice(0, 5),          // 明细有上限：一次放票 39 趟不能全铺开
      more: items.length > 5 ? items.length - 5 : 0,
      when: (s.checked_at || '').replace('T', ' ').slice(0, 16),
    });
  },

  toggle(e) {
    const id = e.currentTarget.dataset.id;
    const ex = Object.assign({}, this.data.expanded);
    ex[id] = !ex[id];
    this.setData({ expanded: ex });
  },

  // --- 添加任务 ---------------------------------------------------------
  openAdd() { this.setData({ showAdd: true }); },
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
    if (!f.id) return wx.showToast({ title: '先填 id', icon: 'none' });

    let params = {};
    if (p.key === 'maoyan') params = { performance_id: f.id };
    else if (p.key === 'moretickets') {
      params = { tour_id: f.id };
      if (f.city) params.city = f.city;
    } else {
      if (!f.date) return wx.showToast({ title: '12306 要填日期', icon: 'none' });
      params = { from: f.id.split(/\s*[-到>]\s*/)[0], to: (f.id.split(/\s*[-到>]\s*/)[1] || ''), date: f.date };
    }

    const id = p.key + '-' + String(f.id).replace(/[^0-9a-zA-Z]/g, '').slice(-12);
    wx.cloud
      .database()
      .collection('radar_tasks')
      .doc(id)
      .set({
        data: {
          _id: id,
          adapter: p.key,
          name: f.name || p.label + ' ' + f.id,
          params: params,
          enabled: true,
        },
      })
      .then(() => {
        wx.showToast({ title: '已添加，正在抓' });
        self.setData({ showAdd: false, form: { name: '', id: '', city: '', date: '' } });
        return self.refresh();
      })
      .catch((e) => wx.showModal({ title: '添加失败', content: String(e.message || e), showCancel: false }));
  },
});
