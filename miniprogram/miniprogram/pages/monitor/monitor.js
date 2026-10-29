// 监控页：首屏回答三个问题——我在盯几个、现在有没有票、上次什么时候查的。
import Toast from '../../vant/toast/toast';
import { request } from '../../utils/api';
import Dialog from '../../vant/dialog/dialog';
import {
  isOn as pushIsOn,
  renew as pushRenew,
  off as pushOff,
  renewAfterAdd as pushRenewAfterAdd,
} from '../../utils/push';
import { loadTasks, removeTask } from '../../utils/tasks';
//
// 两条原则（跟 Python 那边保持一致）：
//   1. 结论先行——卡片第一眼告诉你「有票还是没票」，不是先列车次；
//   2. 不认识的状态不猜——云函数报什么就显示什么，不替它美化。
const PLATFORMS = [
  { key: 'damai', label: '大麦', hint: '粘贴大麦分享链接；或去「找票」直接搜名字', ph: '粘贴大麦分享链接' },
  { key: 'taopiaopiao', label: '淘票票', hint: '粘贴淘票票演出链接（与大麦同一演出编号）；或去「找票」', ph: '粘贴淘票票演出链接' },
  { key: 'bilibili', label: 'B站会员购', hint: '粘贴会员购演出链接；或去「找票」直接搜', ph: '粘贴会员购演出链接' },
  { key: 'maoyan', label: '猫眼', hint: '演出的 performance_id，在猫眼演出页地址栏里', ph: '例：509862' },
  { key: 'moretickets', label: '摩天轮', hint: '巡演的 tour_id，在摩天轮演出页地址栏里', ph: '例：67357b7a…' },
  { key: 'piaoxingqiu', label: '票星球', hint: '粘贴票星球分享链接；或去「找票」直接搜名字', ph: '粘贴票星球演出链接' },
  { key: 'fenwandao', label: '纷玩岛', hint: '粘贴纷玩岛分享链接（能看信息；按票档盯要先去「我的」填凭证）', ph: '粘贴纷玩岛分享链接' },
  { key: 'rail12306', label: '12306', hint: '填两个站名，中间空格隔开就行', ph: '广州南 长沙南' },
];

// 12306 的「价位」就是席别。默认勾二等座——和服务端 pollRail 的兜底一致，
// 不改既有行为；用户想盯别的席别自己增减。
const RAIL_SEATS = ['二等座', '一等座', '商务座', '特等座', '无座', '硬座', '硬卧', '软卧', '动卧', '高级软卧'];

const BANNER_CLOSED_KEY = 'home_banner_closed';
const FAQ_KEY = 'home_faq_open';
const PREV_KEY = 'radar_prev_states';          // 上一轮轮询快照（回流判定基线）
const LAST_STATES_KEY = 'radar_last_states';   // 最近一轮结果（「我的」页展示用）

const FAQ_LIST = [
  {
    q: '盯的是什么？会怎么提醒我？',
    a: '盯的是「回流票」：售罄的场次因为退票、锁票释放、分批放库存，票会突然回来一瞬间。我们每五分钟查一次，一旦从「无票」翻回「有票」，立刻微信推消息喊你。你只需要在第一次授权时勾上「总是保持以上选择」，之后加监控会自动续期，不用再手动领。',
  },
  {
    q: '什么叫回流票？',
    a: '票售罄 ≠ 再也没有票。平台会退票、释放锁座、分批放出库存，界面会从「无票」跳回「可购」——这就是回流票，通常持续几分钟到几小时，拼手速就是拼这个窗口。',
  },
  {
    q: '为什么有票了没收到推送？',
    a: '① 推送关了：首页那条点「恢复推送」；② 监控没在跑：打开小程序才会查。',
  },
  {
    q: '大麦能监控吗？',
    a: '能，而且在「找票」页直接搜名字就行，和猫眼/摩天轮一样——不需要去大麦 App 翻链接（也支持粘贴分享链接）。盯的信号和别的工具一样：场次状态从「无票」翻回「可售」的回流瞬间，状态判定用大麦官方数据，不做假。',
  },
  {
    q: '票星球 / 纷玩岛呢？',
    a: '票星球：粘贴演出链接就能盯，接口公开。纷玩岛：把演出的分享链接粘进来，能直接看到名称/城市/场馆/日期/票价区间和场次——这一步不用登录；想按具体票档盯票，要在「我的」页填一次纷玩岛凭证。',
  },
  {
    q: '会不会帮我抢票或付款？',
    a: '不会。我们只「盯着」回流票出现时喊你一声，买票还是要你自己去官方 App 操作。',
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
  taopiaopiao: '淘票票',
  bilibili: 'B站会员购',
  piaoxingqiu: '票星球',
  fenwandao: '纷玩岛',
  flight: '同程机票',
};

// 任务参数里哪个字段是这个平台的主 id。加监控弹层存的是 `url`（可能是链接），
// 找票页存的是裸 id —— 详情页两样都收，链接由云函数负责抠 id。
const ID_KEY = {
  maoyan: 'performance_id',
  moretickets: 'tour_id',
  damai: 'item_id',
  taopiaopiao: 'item_id',
  bilibili: 'project_id',
  piaoxingqiu: 'show_id',
  fenwandao: 'project_id',
};

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
    // 推送开关（只有开 / 关，不再有「还剩几条」）
    pushOn: false,
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
    // 建任务第二步：勾场次与价位（不勾＝整场任何价位回流都提醒）
    step: 1,
    dsessions: [],
    dLoading: false,
    dNote: '',
    dSub: '',
    dTiers: [],
    dSeats: [],
  },

  onLoad() {
    this.setData({
      bannerClosed: wx.getStorageSync(BANNER_CLOSED_KEY) || false,
    });
    this.refresh();
  },

  onShow() {
    // 从「找票/详情」盯完回来要立刻刷出新任务
    this.setData({ pushOn: pushIsOn() });
    if (typeof this.refreshedOnce !== 'undefined') this.refresh();
    this.refreshedOnce = true;
  },

  onPullDownRefresh() {
    this.refresh().then(() => wx.stopPullDownRefresh());
  },

  refresh() {
    const self = this;
    self.setData({ loading: true, fail: '' });
    const tasks = loadTasks().filter((t) => t && t._id && t.adapter);
    if (!tasks.length) {
      // 一个任务都没有：直接给空态，不打接口
      self.setData({ loading: false, states: [], okCount: 0 });
      self.applyFilter();
      return Promise.resolve();
    }
    // 上一轮快照作为回流判定基线（只留 diffRestock 需要的字段，控制体积）
    const prevStates = wx.getStorageSync(PREV_KEY) || {};
    const payload = tasks.map((t) => ({
      _id: t._id, adapter: t.adapter, name: t.name,
      params: t.params, poster: t.poster || '',
    }));
    return request({ action: 'poll', tasks: payload, prevStates })
      .then((res) => {
        const r = res.result || {};
        if (!r.ok) throw new Error(r.error || '未知错误');
        // 轮询结果里没有 params（那是客户端本地任务的字段）。点卡片要跳详情，
        // 需要知道这条盯的是哪个 id，所以在这里把本地任务并进状态里。
        const taskMap = {};
        for (const t of tasks) taskMap[t._id] = t;
        const list = (r.results || []).map((row) => {
          const f = row.full || row;
          const t = taskMap[f._id] || {};
          return self.decorate(
            Object.assign({}, f, {
              params: t.params || {},
              adapter: t.adapter || f.platform,
            }),
          );
        });
        const okCount = list.filter((s) => s.ok && s.available > 0).length;
        // 存下一轮基线：只留 ok + items 的 label/available
        const next = {};
        for (const row of r.results || []) {
          const f = row.full || {};
          next[row._id] = {
            ok: !!f.ok,
            items: (f.items || []).map((it) => ({ label: it.label, available: !!it.available })),
          };
        }
        wx.setStorageSync(PREV_KEY, next);
        wx.setStorageSync(LAST_STATES_KEY, list);
        self.setData({
          states: list,
          okCount,
          pushOn: pushIsOn(),
          loading: false,
        });
        self.applyFilter();
      })
      .catch((e) => {
        self.setData({
          loading: false,
          fail:
            String(e.message || e) +
            '\n\n两种常见原因：\n' +
            '· 本地服务没开：在 server/ 目录跑 node api.js\n' +
            '· 平台接口超时或被风控（只影响抓取那一项，其他项正常）',
        });
      });
  },

  // 把云函数的原始记录整理成「能给人看」的样子
  decorate(s) {
    const items = (s.items || []).filter((i) => i.available);
    const total = (s.items || []).length;
    const avail = typeof s.available === 'number' ? s.available : items.length;
    // 回流 = 这一轮有场次从「无票」翻回「有票」，是全场最值得高兴的事
    const restock = Array.isArray(s.restock) && s.restock.length > 0;

    let head, cls, bar;
    const isFlight = s.adapter === 'flight' || s.platform === 'flight';
    const offline = !s.ok && /404|不存在|已下架|下线/.test(String(s.error || ''));
    if (offline) {
      head = '已下架';
      cls = 'tag-off';
      bar = 'task-bar-none';
    } else if (!s.ok) {
      head = '抓取失败';
      cls = 'tag-bad';
      bar = 'task-bar-bad';
    } else if (!total) {
      head = '暂未开售';
      cls = 'tag-none';
      bar = 'task-bar-none';
    } else if (isFlight) {
      // 机票没有「有没有票」，只有「有没有跌到目标价」，标签别照搬演出那套
      const tgt = Number((s.params || {}).budget) || 0;
      if (avail > 0) {
        head = '到价了';
        cls = 'tag-ok';
        bar = 'task-bar-ok';
      } else if (tgt) {
        head = '未到价';
        cls = 'tag-none';
        bar = 'task-bar-none';
      } else {
        head = '只看价';
        cls = 'tag-none';
        bar = 'task-bar-none';
      }
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
    // 抢票时机建议：采样足够多时，基于历史数据给提示
    const hist = (s.history || []).filter((h) => h && h.t);
    let tip = '';
    // 机票的 history 是「到价/没到价」的 0-1，拿它算「回流次数」是胡说，直接跳过
    if (!isFlight && hist.length >= 5) {
      const recent = hist.slice(-10);
      const hadTicket = recent.some((h) => h.n > 0);
      const drops = recent.filter((h) => h.n > 0).length;
      if (hadTicket && drops >= 2) {
        tip = '💡 近期有 ' + drops + ' 次回流，蹲守有机会';
      } else if (hadTicket) {
        tip = '💡 曾放过票，注意观察';
      }
    }

    return Object.assign({}, s, {
      head,
      headClass: cls,
      barClass: bar,
      platform: LABEL[s.platform] || s.platform,
      // 机票的 when 只是一个日期，把价格摘要放上去更有用
      sub2: isFlight ? first.status || '' : first.when || '',
      total,
      available: avail,
      restock: restock,
      relTime: relTime(s.checked_at),
      show: items.slice(0, 5),          // 明细有上限：一次放票 39 趟不能全铺开
      more: items.length > 5 ? items.length - 5 : 0,
      smartTip: tip,
    });
  },

  toggle(e) {
    const id = e.currentTarget.dataset.id;
    const ex = Object.assign({}, this.data.expanded);
    ex[id] = !ex[id];
    this.setData({ expanded: ex });
  },

  // 点卡片进详情：盯着的这条本身就能点开看场次/票档。
  // 12306 是站站查询，没有「演出详情」，回查票页并预填站名。
  openDetail(e) {
    const id = e.currentTarget.dataset.id;
    const s = this.data.states.find((x) => x._id === id);
    if (!s) return;

    if (s.adapter === 'rail12306' || s.platform === '12306') {
      const p = s.params || {};
      wx.setStorageSync('shows_prefill_rail', {
        from: p.from || '', to: p.to || '', date: p.date || '',
      });
      wx.switchTab({ url: '/pages/shows/shows' });
      return;
    }

    // 机票也没有「演出详情」：回「找票」页，把这条航线填进机票面板。
    // 组件靠 radar_flight_route 读航线，attached 时自动查一次价。
    if (s.adapter === 'flight') {
      const p = s.params || {};
      wx.setStorageSync('shows_prefill_flight', {
        from: p.from || '', to: p.to || '',
        fromName: p.fromName || '', toName: p.toName || '',
      });
      wx.switchTab({ url: '/pages/shows/shows' });
      return;
    }

    const p = s.params || {};
    const key = ID_KEY[s.adapter] || '';
    const raw = (key && p[key]) || p.id || p.url || '';
    if (!raw) {
      Toast('这条任务里没存演出 id，跳不了详情');
      return;
    }
    const q = [
      'platform=' + encodeURIComponent(s.adapter),
      'id=' + encodeURIComponent(raw),
      'label=' + encodeURIComponent(LABEL[s.adapter] || s.adapter),
      'name=' + encodeURIComponent(s.name || ''),
      'poster=' + encodeURIComponent(s.poster || ''),
      'date=' + encodeURIComponent(s.sub2 || ''),
    ];
    wx.navigateTo({ url: '/pages/detail/detail?' + q.join('&') });
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
          Toast('当前版本不支持分享');
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
                    success: () => Toast({ type: 'success', message: '已存相册' }),
                    fail: (err) =>
                      Toast('保存失败：相册权限'),
                  });
                } else {
                  // 微信没有"直接转发图片"的开放接口，退回到分享小程序
                  Toast('点右上角 ··· 转发给好友');
                }
              },
            });
          },
          fail: () => Toast('生成图片失败'),
        });
      });
  },

  goShows() {
    wx.switchTab({ url: '/pages/shows/shows' });
  },

  goCompare() {
    wx.navigateTo({ url: '/pages/compare/compare' });
  },

  goHistory() {
    wx.switchTab({ url: '/pages/history/history' });
  },

  // --- 添加任务 ---------------------------------------------------------
  openAdd() { this.setData({ showAdd: true, step: 1, dsessions: [], dSeats: [], dNote: '', dSub: '', dLoading: false }); },

  // 点推送状态条 = 切换推送：开着就关，关着就申请一次授权打开。
  tapPush() {
    if (this.data.pushOn) {
      pushOff();
      this.setData({ pushOn: false });
      Toast({ type: 'text', message: '已暂停推送' });
      return;
    }
    pushRenew({ silent: false, force: true }).then((ok) => {
      this.setData({ pushOn: pushIsOn() });
      if (ok) Toast({ type: 'success', message: '推送已恢复' });
    });
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
        Dialog.alert({ title: '扫码失败', message: err.errMsg || '未知错误' });
      },
    });
  },

  handleScanned(text) {
    if (!text) return Toast('没扫到内容');

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

    Dialog.alert({
      title: '识别不了这个二维码',
      message: '目前只支持猫眼/摩天轮/12306 的链接或纯文本车次。扫到内容：' + text.slice(0, 80),
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
    Toast('已填入，检查下再点开始盯');
  },

  quickSubscribe() {
    wx.switchTab({ url: '/pages/mine/mine' });
  },

  closeAdd() { this.setData({ showAdd: false, step: 1, dsessions: [], dSeats: [], dNote: '', dSub: '', dLoading: false }); },
  onPlatform(e) { this.setData({ platformIndex: Number(e.detail.value) }); },
  onInput(e) {
    const k = e.currentTarget.dataset.k;
    this.setData({ ['form.' + k]: e.detail.value });
  },
  onDate(e) { this.setData({ 'form.date': e.detail.value }); },

  /** 按平台拼轮询参数。返回 null 表示参数不全（已提示）。 */
  buildParams(p, f) {
    const v = String(f.id || '').trim();
    if (p.key === 'maoyan') return { performance_id: v };
    if (p.key === 'moretickets') {
      const o = { tour_id: v };
      if (f.city) o.city = f.city;
      return o;
    }
    if (['damai', 'taopiaopiao', 'piaoxingqiu', 'fenwandao'].indexOf(p.key) >= 0) {
      // 这些平台的输入既可能是分享链接、也可能是裸 id；统一按「url」传，
      // 云函数侧各适配器自己抠 id（findFenwandao / 大麦 / 票星球都支持）。
      return { url: v };
    }
    if (p.key === 'bilibili') {
      return /^\d{5,}$/.test(v) ? { project_id: v } : { url: v };
    }
    // 12306：填两个站名 + 日期
    const seg = v.split(/\s*[-到>]\s*|\s+/).filter(Boolean);
    if (seg.length < 2) { Toast('要填两个站名'); return null; }
    if (!f.date) { Toast('12306 要选乘车日期'); return null; }
    return { from: seg[0], to: seg[1], date: f.date };
  },

  /** 输入既可能是链接也可能是 id：裸 id 直接当 id 用，链接先走一次 find 解析。 */
  resolveId(p, raw, extra) {
    const v = String(raw || '').trim();
    if (p.key !== 'rail12306' && /^[0-9A-Za-z_-]{4,}$/.test(v)) return Promise.resolve(v);
    const ev = { action: 'find', keyword: v, platform: p.key, limit: 3 };
    if (extra) Object.assign(ev, extra);
    return request(ev).then((res) => {
      const r = (res && res.result) || {};
      if (!r.ok) throw new Error(r.error || '没找到这个演出');
      const row = (r.rows || [])[0];
      if (!row) throw new Error('没找到这个演出，检查链接或 id');
      return String(row.performanceId || row.projectId || row.id || '');
    });
  },

  /** 第一步：校验 → 取这场的场次×票档 → 有票档就走第二步勾选；没有就直接建，不假装能选。 */
  submitAdd() {
    const self = this;
    const p = PLATFORMS[this.data.platformIndex];
    const f = this.data.form;
    if (!f.id || !f.id.trim()) return Toast('先把上面那栏填了');

    const params = this.buildParams(p, f);
    if (!params) return;

    // 12306 盯的是整条线路（不知道具体车次），「价位」＝席别，走一张扁平多选表。
    if (p.key === 'rail12306') {
      this._pending = { params, p, rail: true };
      this.setData({
        step: 2,
        dSeats: RAIL_SEATS.map((n) => ({ name: n, selected: n === '二等座' })),
      });
      this.recountD([]);
      return;
    }

    this.setData({ dLoading: true, dNote: '' });
    this.resolveId(p, f.id)
      .then((id) => request({ action: 'detail', platform: p.key, id }))
      .then((res) => {
        const r = (res && res.result) || {};
        if (!r.ok) throw new Error(r.error || '取场次失败');
        const sessions = (r.sessions || []).map((s) => ({
          name: s.name || '场次',
          when: s.when || '',
          selected: true,
          tiers: (s.tiers || []).map((t) => ({
            name: t.name || '票档',
            price: t.price || '',
            status: t.status || '',
            available: t.available !== false,
            selected: t.available !== false,
          })),
        }));
        if (!sessions.some((s) => s.tiers.length)) {
          // 没有票档粒度（平台要登录 / 该场没挂票档）→ 直接建，别让用户白点
          self.setData({ dLoading: false });
          self.createTask(params, p, r.noTiers ? '这个平台拿不到票档明细，按整场盯' : '');
          return;
        }
        self._pending = { params, p };
        self.setData({ dLoading: false, step: 2, dsessions: sessions });
        self.recountD(sessions);
      })
      .catch((e) => {
        // 详情拿不到别把用户卡死：按整场建，并如实说明为什么
        self.setData({ dLoading: false });
        self.createTask(params, p, '没取到场次明细（' + String(e.message || e) + '），先按整场盯');
      });
  },

  /** 重算第二步文案。价位 key 用价格数字（与详情页一致）；12306 用席别名。 */
  recountD(sessions) {
    const pend = this._pending || {};
    if (pend.rail) {
      const seats = (this.data.dSeats || []).filter((s) => s.selected).map((s) => s.name);
      this.setData({
        dTiers: seats,
        dSub: seats.length ? '开始盯（' + seats.length + ' 个席别）' : '开始盯（所有席别）',
      });
      return;
    }
    const keys = [];
    let sCount = 0;
    for (const s of sessions) {
      const tiers = s.tiers || [];
      const sel = tiers.filter((t) => t.available && t.selected);
      if (s.selected && (!tiers.length || sel.length)) sCount++;
      for (const t of sel) {
        const key = String(t.price || '').replace(/[^0-9]/g, '') || t.name;
        if (keys.indexOf(key) < 0) keys.push(key);
      }
    }
    this.setData({
      dTiers: keys,
      dSub: '开始盯（共 ' + sCount + ' 场 ' + (keys.length ? keys.length + ' 个价格' : '· 整场') + '）',
    });
  },

  toggleDSeat(e) {
    const name = e.currentTarget.dataset.name || '';
    const seats = (this.data.dSeats || []).map((s) =>
      s.name === name ? Object.assign({}, s, { selected: !s.selected }) : s,
    );
    this.setData({ dSeats: seats });
    this.recountD([]);
  },

  toggleDSession(e) {
    const si = Number(e.currentTarget.dataset.si);
    const sessions = this.data.dsessions.slice();
    if (!sessions[si]) return;
    sessions[si] = Object.assign({}, sessions[si], { selected: !sessions[si].selected });
    this.setData({ dsessions: sessions });
    this.recountD(sessions);
  },

  /** 点价位：可多选；灰的（不可用）点了没反应。 */
  toggleDTier(e) {
    const si = Number(e.currentTarget.dataset.si);
    const name = e.currentTarget.dataset.name || '';
    const sessions = this.data.dsessions.slice();
    const s = sessions[si];
    if (!s) return;
    sessions[si] = Object.assign({}, s, {
      tiers: s.tiers.map((t) =>
        t.name === name && t.available ? Object.assign({}, t, { selected: !t.selected }) : t,
      ),
    });
    this.setData({ dsessions: sessions });
    this.recountD(sessions);
  },

  noop() {},

  backStep() {
    this.setData({ step: 1, dsessions: [], dSeats: [], dNote: '', dSub: '' });
  },

  /** 第二步确认：把勾选的价位/席别/场次写进任务参数，轮询按它过滤。 */
  confirmAdd() {
    const pend = this._pending;
    if (!pend) return this.backStep();
    const params = Object.assign({}, pend.params);
    const tiers = this.data.dTiers || [];
    if (pend.rail) {
      // 12306：席别就是它的「价位」。不勾＝所有席别（服务端 pollRail 的语义）
      if (tiers.length) params.seat_types = tiers.slice();
      return this.createTask(params, pend.p);
    }
    if (tiers.length) params.price_labels = tiers.slice();
    const all = this.data.dsessions || [];
    const sel = all.filter((s) => s.selected).map((s) => s.name);
    if (sel.length && sel.length < all.length) params.sessions = sel;
    this.createTask(params, pend.p);
  },

  /** 落任务：写本地任务表，随后刷一轮看结果。 */
  createTask(params, p, note) {
    const self = this;
    const f = this.data.form;
    const id = p.key + '-' + String(f.id).replace(/[^0-9a-zA-Z]/g, '').slice(-12);
    const task = {
      _id: id,
      adapter: p.key,
      name:
        f.name ||
        (p.key === 'rail12306' ? params.from + ' → ' + params.to : p.label + ' 监控'),
      params: params,
      enabled: true,
    };
    try {
      const tasks = require('../../utils/tasks');
      const existing = tasks.loadTasks();
      const idx = existing.findIndex((t) => t._id === id);
      if (idx >= 0) existing[idx] = task;
      else existing.push(task);
      tasks.saveTasks(existing);
      if (note) Toast(note);
      self.setData({
        showAdd: false,
        step: 1,
        dsessions: [],
        dSeats: [],
        dNote: '',
        dSub: '',
        form: { name: '', id: '', city: '', date: '' },
      });
      self._pending = null;
      // 加监控成功后静默续一条（推送关着的话不会扰动用户）
      pushRenewAfterAdd().then((ok) => {
        if (ok) self.setData({ pushOn: pushIsOn() });
      });
      setTimeout(() => self.refresh(), 2500);
    } catch (e) {
      Dialog.alert({ title: '添加失败', message: String(e.message || e) });
    }
  },
});
