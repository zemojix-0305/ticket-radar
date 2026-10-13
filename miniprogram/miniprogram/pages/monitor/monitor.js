// 监控页：首屏回答三个问题——我在盯几个、现在有没有票、上次什么时候查的。
//
// 两条原则（跟 Python 那边保持一致）：
//   1. 结论先行——卡片第一眼告诉你「有票还是没票」，不是先列车次；
//   2. 不认识的状态不猜——云函数报什么就显示什么，不替它美化。
//
// 额度记账放在 storage：微信一次性订阅的额度后台查不到，
// 只能让用户自己看得见「我还剩几条」，否则推送突然不响会毫无头绪。
const PLATFORMS = [
  { key: 'damai', label: '大麦', hint: '把大麦 App 里该演出的分享链接粘贴进来', ph: '粘贴大麦分享链接' },
  { key: 'maoyan', label: '猫眼', hint: '演出的 performance_id，在猫眼演出页地址栏里', ph: '例：509862' },
  { key: 'moretickets', label: '摩天轮', hint: '巡演的 tour_id，在摩天轮演出页地址栏里', ph: '例：67357b7a…' },
  { key: 'piaoxingqiu', label: '票星球', hint: '把票星球分享的演出链接粘贴进来', ph: '粘贴票星球演出链接' },
  { key: 'rail12306', label: '12306', hint: '填两个站名，中间空格隔开就行', ph: '广州南 长沙南' },
];

const BANNER_CLOSED_KEY = 'home_banner_closed';
const FAQ_KEY = 'home_faq_open';

const FAQ_LIST = [
  {
    q: '这个小程序怎么提醒我？',
    a: '微信的「一次性订阅」机制：你每点一次「允许通知」，就能收到一条推送。有票变化时我们会消耗一条额度给你发消息。',
  },
  {
    q: '为什么有票了没收到推送？',
    a: '两种最常见原因：① 推送额度用完了，去「我的」页再领；② 云开发定时器还没配好，监控没真正跑起来。',
  },
  {
    q: '大麦能监控吗？',
    a: '能，但方式特殊：大麦不让小程序自动搜（搜索接口有风控），请到大麦 App 打开想盯的演出 → 分享 → 复制链接，粘进来就能盯「开售状态」。大麦网页渠道不卖票，我们盯的是官方按钮状态和开售时间的变化。',
  },
  {
    q: '票星球 / 纷玩岛呢？',
    a: '票星球：粘贴演出链接可以试（接口无公开文档，能不能通取决于部署环境，失败会如实报错）。纷玩岛：官网是静态下载页，没有网页端接口，接不了。',
  },
  {
    q: '会不会帮我抢票或付款？',
    a: '不会。我们只「盯着」有票变化时喊你一声，买票还是要你自己去官方 App 操作。',
  },
  {
    q: '二维码怎么用？',
    a: '演出详情页的分享二维码、或票务平台的链接二维码，扫完后小程序会尝试提取演出信息并创建监控。',
  },
];

const LABEL = {
  maoyan: '猫眼',
  moretickets: '摩天轮',
  rail12306: '12306',
  damai: '大麦',
  piaoxingqiu: '票星球',
  fenwandao: '纷玩岛',
};

const CREDIT_KEY = 'wx_subscribe_credit';
const SUPPORT_VIEWED_KEY = 'support_viewed';

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
    filtered: [],
    okCount: 0,
    credit: 0,
    loading: true,
    fail: '',
    expanded: {},
    showAdd: false,
    platforms: PLATFORMS,
    platformIndex: 0,
    form: { name: '', id: '', city: '', date: '' },
    bannerClosed: false,
    bannerText: '只监控、不下单。购票请认准官方渠道。',
    faqOpen: false,
    faqList: FAQ_LIST,
    usageOpen: false,
    filter: 'all',
  },

  onLoad() {
    this.setData({
      bannerClosed: wx.getStorageSync(BANNER_CLOSED_KEY) || false,
    });
    this.refresh();
  },

  onShow() {
    // 从「我的」页领完额度回来要能看到数字变了
    this.setData({ credit: wx.getStorageSync(CREDIT_KEY) || 0 });
    // 首次进入且未关闭过，显示支持作者
    if (!this.data.supportTipShown) {
      const viewed = wx.getStorageSync(SUPPORT_VIEWED_KEY);
      if (!viewed) {
        this.setData({ showSupport: true });
      }
    }
  },

  closeSupport() {
    this.setData({ showSupport: false });
    wx.setStorageSync(SUPPORT_VIEWED_KEY, true);
  },

  onStar() {
    wx.setStorageSync(SUPPORT_VIEWED_KEY, true);
    wx.setClipboardData({
      data: 'https://github.com/zemojix-0305/ticket-radar',
      success: () => wx.showToast({ title: '链接已复制，快去 Star', icon: 'none' }),
    });
  },

  onCoffee() {
    wx.setStorageSync(SUPPORT_VIEWED_KEY, true);
    wx.showToast({ title: '感谢支持！', icon: 'none' });
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
        self.applyFilter();
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

  // --- 筛选（全部 / 有票 / 无票）------------------------------------------
  // 任务多起来之后，用户只想看「现在能买的那几个」。
  // 纯本地过滤，不请求云函数——切筛选不该有等待感。
  onFilter(e) {
    const f = e.currentTarget.dataset.f;
    if (!f || f === this.data.filter) return;
    this.setData({ filter: f });
    this.applyFilter();
  },

  applyFilter() {
    const f = this.data.filter;
    const all = this.data.states;
    let out = all;
    if (f === 'ok') out = all.filter((s) => s.ok && s.available > 0);
    else if (f === 'none') out = all.filter((s) => !s.ok || s.available === 0);
    this.setData({ filtered: out });
  },

  // --- 分享卡片 -------------------------------------------------------------
  // 把一条监控画成图：好友之间传「这场有票」比转发小程序链接直接。
  // canvas 2d + 离屏渲染，生成后存相册或走微信转发。
  shareTask(e) {
    const id = e.currentTarget.dataset.id;
    const s = this.data.states.find((x) => x._id === id);
    if (!s) return;

    const W = 300;
    const H = 180;
    const query = wx.createSelectorQuery().select('#shareCanvas');
    query
      .fields({ node: true, size: true })
      .exec((res) => {
        if (!res || !res[0] || !res[0].node) {
          wx.showToast({ title: '当前版本不支持分享', icon: 'none' });
          return;
        }
        const canvas = res[0].node;
        const ctx = canvas.getContext('2d');
        const dpr = 3;
        canvas.width = W * dpr;
        canvas.height = H * dpr;
        ctx.scale(dpr, dpr);

        // 底：ins 风纯白底
        ctx.fillStyle = '#FFFFFF';
        ctx.fillRect(0, 0, W, H);

        // 状态药丸
        const ok = s.ok && s.available > 0;
        ctx.font = '12px -apple-system, "PingFang SC", sans-serif';
        const pill = s.head;
        const pw = ctx.measureText(pill).width + 22;
        ctx.fillStyle = ok ? '#E3F5EA' : '#F1F1F4';
        ctx.beginPath();
        ctx.arc(16 + 11, 30, 11, 0, Math.PI * 2);
        ctx.arc(16 + pw - 11, 30, 11, 0, Math.PI * 2);
        ctx.rect(16 + 11, 19, pw - 22, 22);
        ctx.fill();
        ctx.fillStyle = ok ? '#1E7A46' : '#6E6E73';
        ctx.fillText(pill, 16 + 11, 34.5);

        // 任务名
        ctx.fillStyle = '#1A1A1A';
        ctx.font = '600 17px -apple-system, "PingFang SC", sans-serif';
        let name = s.name || '监控';
        while (ctx.measureText(name).width > W - 40 && name.length > 2) {
          name = name.slice(0, -1);
        }
        if (name !== (s.name || '监控')) name += '…';
        ctx.fillText(name, 16, 84);

        // 平台 + 时间
        ctx.fillStyle = '#6E6E73';
        ctx.font = '12px -apple-system, "PingFang SC", sans-serif';
        const sub = [s.platform, s.relTime].filter(Boolean).join(' · ');
        ctx.fillText(sub, 16, 108);

        // 余票柱状条
        const bars = Math.min(12, s.total || 6);
        const bw = 10;
        const gap = 5;
        const x0 = 16;
        const y0 = 158;
        const hMax = 30;
        for (let i = 0; i < bars; i += 1) {
          const h = ok ? hMax * (0.45 + 0.55 * ((i % 3) / 2)) : hMax * 0.18;
          ctx.fillStyle = ok ? '#1A1A1A' : '#E5E5EA';
          ctx.beginPath();
          ctx.rect(x0 + i * (bw + gap), y0 - h, bw, h);
          ctx.fill();
        }

        // 品牌行
        ctx.fillStyle = '#1A1A1A';
        ctx.font = '600 12px -apple-system, "PingFang SC", sans-serif';
        ctx.fillText('余票监控', 16, 172);
        ctx.fillStyle = '#8E8E93';
        ctx.font = '10px -apple-system, "PingFang SC", sans-serif';
        ctx.fillText('长按识别 · 查看实时余票', 74, 172);

        wx.canvasToTempFilePath({
          canvas,
          success: (out2) => {
            wx.showActionSheet({
              itemList: ['保存到相册', '发给微信好友'],
              success: (act) => {
                if (act.tapIndex === 0) {
                  wx.saveImageToPhotosAlbum({
                    filePath: out2.tempFilePath,
                    success: () => wx.showToast({ title: '已存相册' }),
                    fail: (err) =>
                      wx.showToast({ title: '保存失败：相册权限', icon: 'none' }),
                  });
                } else {
                  // 微信没有"直接转发图片"的开放接口，退回到分享小程序
                  wx.showToast({ title: '点右上角 ··· 转发给好友', icon: 'none' });
                }
              },
            });
          },
          fail: () => wx.showToast({ title: '生成图片失败', icon: 'none' }),
        });
      });
  },

  goShows() {
    wx.switchTab({ url: '/pages/shows/shows' });
  },

  goHistory() {
    wx.switchTab({ url: '/pages/history/history' });
  },

  // --- 添加任务 ---------------------------------------------------------
  openAdd() { this.setData({ showAdd: true }); },

  // 跳「我的」页：领订阅额度
  goMine() {
    wx.switchTab({ url: '/pages/mine/mine' });
  },

  // --- 首页新增：公告 / 使用说明 / FAQ / 扫一扫 ---------------------------
  closeBanner() {
    wx.setStorageSync(BANNER_CLOSED_KEY, true);
    this.setData({ bannerClosed: true });
  },

  toggleUsage() {
    this.setData({ usageOpen: !this.data.usageOpen });
  },

  toggleFaq() {
    const open = !this.data.faqOpen;
    wx.setStorageSync(FAQ_KEY, open);
    this.setData({ faqOpen: open });
  },

  onFaqItem(e) {
    const idx = e.currentTarget.dataset.idx;
    const list = this.data.faqList.map((item, i) => {
      if (i === idx) item.open = !item.open;
      return item;
    });
    this.setData({ faqList: list });
  },

  scanQr() {
    wx.scanCode({
      onlyFromCamera: false,
      scanType: ['qrCode'],
      success: (res) => {
        this.handleScanned(res.result || '');
      },
      fail: (err) => {
        if (err.errMsg && err.errMsg.indexOf('cancel') >= 0) return;
        wx.showModal({ title: '扫码失败', content: err.errMsg || '未知错误', showCancel: false });
      },
    });
  },

  handleScanned(text) {
    if (!text) return wx.showToast({ title: '没扫到内容', icon: 'none' });

    // 1) 大麦链接：detail.damai.cn/item.htm?id=xxx 或 m.damai.cn
    if (/damai\.cn/i.test(text)) {
      return this.prefillAdd('damai', text);
    }

    // 2) 票星球链接：piaoxingqiu.com/show/xxx
    if (/piaoxingqiu\.com/i.test(text)) {
      return this.prefillAdd('piaoxingqiu', text);
    }

    // 3) 猫眼演出详情页：https://show.maoyan.com/detail?performanceId=xxx
    const maoyanMatch = text.match(/[?&]performanceId=([0-9]+)/i);
    if (maoyanMatch) {
      return this.prefillAdd('maoyan', maoyanMatch[1]);
    }

    // 2) 摩天轮链接：含 tourId 或 tour/xxx
    const moreMatch = text.match(/tour(?:Id)?[=\/]([a-f0-9]+)/i);
    if (moreMatch) {
      return this.prefillAdd('moretickets', moreMatch[1]);
    }

    // 3) 12306 车票链接 / 纯文本 "广州南 长沙南"
    const railMatch = text.match(/(\S+)\s*[-到>]\s*(\S+)/);
    if (railMatch && text.length < 50) {
      return this.prefillAdd('rail12306', railMatch[1] + ' ' + railMatch[2]);
    }

    wx.showModal({
      title: '识别不了这个二维码',
      content: '目前只支持猫眼/摩天轮/12306 的链接或纯文本车次。扫到内容：' + text.slice(0, 80),
      showCancel: false,
    });
  },

  prefillAdd(platformKey, idValue) {
    const idx = PLATFORMS.findIndex((p) => p.key === platformKey);
    if (idx < 0) return;
    this.setData({
      platformIndex: idx,
      'form.id': idValue,
      showAdd: true,
    });
    wx.showToast({ title: '已填入，检查下再点开始盯', icon: 'none' });
  },

  quickSubscribe() {
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
    } else if (p.key === 'damai' || p.key === 'piaoxingqiu') {
      // 大麦/票星球：粘贴的就是分享链接，云函数自己抠 id
      params = { url: f.id.trim() };
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
