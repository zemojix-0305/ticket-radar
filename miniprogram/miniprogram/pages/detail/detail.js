// 演出详情页：云函数 detail action 拉场次×票档，
// 「选场次和票价」多选网格 → 底部「开始订阅（共订阅 N 场 M 个价格）」→ 写 radar_tasks。
import Toast from '../../vant/toast/toast';
import { request } from '../../utils/api';
import { loadTasks, saveTasks } from '../../utils/tasks';
import Dialog from '../../vant/dialog/dialog';

// 注意：额度 key 全项目统一是 wx_subscribe_credit（mine/monitor/history/inbox 同款），
// 之前这里写成 radar_credit 导致详情页永远读 0、明明有额度还弹"去领额度"。
const CREDIT_KEY = 'wx_subscribe_credit';

Page({
  data: {
    loading: true,
    error: '',
    // 降级模式：详情接口被风控拦住时，用跳转带过来的搜索信息渲染基础详情
    degraded: false,
    degradeMsg: '',
    // 有场次但拿不到票档明细（平台要登录）——按整场盯
    noTiers: false,
    adding: false,
    platform: '',
    id: '',
    label: '',
    name: '',
    poster: '',
    city: '',
    venue: '',
    date: '',
    price: '',
    // 预售/开售时间（纷玩岛匿名详情带入；形如「预售 10/15 13:00 · 开售 10/16 15:18」）
    saleNote: '',
    // 12306 查询上下文（shows 页跳转时带上；详情=单趟车次全席别余票必需）
    railFrom: '',
    railTo: '',
    railDate: '',
    // 场次×票档选择网格：每场一个勾选框，档位一格一个可多选的价位按钮
    sessions: [],
    subCount: 0,
    priceCount: 0,
    selTiers: [],
    subText: '盯这场演出',
  },

  onLoad(q) {
    this.setData({
      platform: q.platform || '',
      id: q.id || '',
      label: decodeURIComponent(q.label || ''),
      name: decodeURIComponent(q.name || ''),
      poster: decodeURIComponent(q.poster || ''),
      city: decodeURIComponent(q.city || ''),
      venue: decodeURIComponent(q.venue || ''),
      date: decodeURIComponent(q.date || ''),
      price: decodeURIComponent(q.price || ''),
      railFrom: decodeURIComponent(q.from || ''),
      railTo: decodeURIComponent(q.to || ''),
      railDate: decodeURIComponent(q.railDate || ''),
    });
    wx.setNavigationBarTitle({ title: this.data.name ? this.data.name.slice(0, 12) : '演出详情' });
    this.loadDetail();
  },

  loadDetail() {
    this.setData({ loading: true, error: '', degraded: false, degradeMsg: '', noTiers: false, saleNote: '', subText: '' });
    const req = { action: 'detail', platform: this.data.platform, id: this.data.id };
    // 12306：详情查询要站名×日期上下文（服务端按它重新拉一遍余票）
    if (this.data.platform === 'rail12306') {
      req.from = this.data.railFrom;
      req.to = this.data.railTo;
      req.date = this.data.railDate;
    }
    request(req)
      .then((res) => {
        const r = res.result || {};
        if (!r.ok) throw new Error(r.error || '未知错误');
        // 服务端降级成功：演出信息（名称/场馆/日期）拿到了，只是没有场次×票档
        if (r.degraded) {
          this.setData({
            loading: false,
            degraded: true,
            sessions: [],
            selTiers: [],
            name: r.name || this.data.name,
            venue: r.venue || this.data.venue,
            date: r.dateRange || this.data.date,
            poster: r.poster || this.data.poster,
            degradeMsg: r.degradeMsg || '场次和票档暂时拿不到，演出信息先看看',
          });
          return;
        }
        // 场次×票档：默认全选可用项（对齐竞品「共订阅 N 场 M 个价格」）
        const sessions = (r.sessions || []).map((s) => ({
          name: s.name || '场次',
          when: s.when || '',
          performId: s.performId || '',
          available: !!s.available,
          selected: true,
          tiers: (s.tiers || []).map((t) => ({
            name: t.name || '票档',
            price: t.price || '',
            status: t.status || '',
            available: t.available !== false,
            selected: t.available !== false,
          })),
        }));
        const rec = this.recount(sessions);
        this.setData(
          Object.assign(
            {
              loading: false,
              sessions,
              name: r.name || this.data.name,
              venue: r.venue || this.data.venue,
              poster: r.poster || this.data.poster,
              date: r.dateRange || this.data.date,
              city: r.city || this.data.city,
              // 大麦/淘票票匿名通道给的是整场价格区间，信息卡里亮出来
              price: r.priceRange ? '票价区间 ' + r.priceRange : this.data.price,
              saleNote: r.saleNote || '',
              noTiers: !!r.noTiers,
            },
            rec,
          ),
        );
      })
      .catch((e) => {
        // 大麦风控拦详情接口时别整页报错——搜索结果点进来时名称/城市/场馆/价格/海报
        // 都随 URL 带上了，用这些降级渲染，只是拿不到场次×票档。
        if (this.data.name) {
          this.setData({
            loading: false,
            degraded: true,
            degradeMsg: '场次和票档暂时拿不到，稍后再来',
          });
          return;
        }
        this.setData({ loading: false, error: String(e.message || e) });
      });
  },

  /** 重算「共订阅 N 场 M 个价格」，并把选中价位折算成轮询用的关键字（数字）。 */
  recount(sessions) {
    const tierKeys = [];
    let sCount = 0;
    for (const s of sessions) {
      const tiers = s.tiers || [];
      const sel = tiers.filter((t) => t.available && t.selected);
      if (s.selected && (!tiers.length || sel.length)) sCount++;
      for (const t of sel) {
        // 轮询按 label/status/price 关键字过滤，用法与平台无关：优先用价格数字
        const key = String(t.price || '').replace(/[^0-9]/g, '') || t.name;
        if (tierKeys.indexOf(key) < 0) tierKeys.push(key);
      }
    }
    const m = tierKeys.length;
    return {
      subCount: sCount,
      priceCount: m,
      selTiers: tierKeys,
      subText: sessions.length
        ? m
          ? '开始订阅（共订阅 ' + sCount + ' 场 ' + m + ' 个价格）'
          : '开始订阅（共订阅 ' + sCount + ' 场 · 整场）'
        : '盯这场演出',
    };
  },

  toggleSession(e) {
    const si = Number(e.currentTarget.dataset.si);
    const sessions = this.data.sessions.slice();
    if (!sessions[si]) return;
    sessions[si] = Object.assign({}, sessions[si], { selected: !sessions[si].selected });
    this.setData(Object.assign({ sessions }, this.recount(sessions)));
  },

  /** 点价位：可多选；不可用的档位（灰的）点了没反应。 */
  toggleTier(e) {
    const si = Number(e.currentTarget.dataset.si);
    const name = e.currentTarget.dataset.name || '';
    const sessions = this.data.sessions.slice();
    const s = sessions[si];
    if (!s) return;
    sessions[si] = Object.assign({}, s, {
      tiers: s.tiers.map((t) =>
        t.name === name && t.available ? Object.assign({}, t, { selected: !t.selected }) : t,
      ),
    });
    this.setData(Object.assign({ sessions }, this.recount(sessions)));
  },

  noop() {},

  reload() {
    this.loadDetail();
  },

  /** 开始订阅：和演出页同一套建库逻辑，参数名按平台区分 */
  addMonitor() {
    const credit = wx.getStorageSync(CREDIT_KEY) || 0;
    if (credit <= 0) {
      Dialog.confirm({
        title: '先领一下推送额度',
        message: '微信的订阅消息是「点一次允许换一条推送」。现在额度是 0，加了监控也收不到提醒。',
        confirmButtonText: '去领额度',
      }).then(() => {
        wx.switchTab({ url: '/pages/mine/mine' });
      }).catch(() => {});
      return;
    }
    const p = this.data.platform;
    let params = {};
    if (p === 'moretickets') params = { tour_id: this.data.id };
    else if (p === 'maoyan') params = { performance_id: this.data.id };
    else if (p === 'damai' || p === 'taopiaopiao') params = { item_id: this.data.id };
    else if (p === 'bilibili') params = { project_id: this.data.id };
    else if (p === 'piaoxingqiu') params = { show_id: this.data.id };
    else if (p === 'fenwandao') params = { project_id: this.data.id };
    else if (p === 'rail12306') {
      if (!this.data.railFrom || !this.data.railTo || !this.data.railDate) {
        Toast('缺站名/日期——从查票页点车次进来才能盯');
        return;
      }
      params = { from: this.data.railFrom, to: this.data.railTo, date: this.data.railDate };
      // 12306 的「价位」就是席别：勾了几个席别就只盯这几档
      if (this.data.selTiers.length) params.seat_types = this.data.selTiers.slice();
    } else {
      Toast('这个平台暂不能直接盯');
      return;
    }
    // 勾了的价位 → 只盯这些价位（服务端 poll 按关键字过滤 items）
    if (p !== 'rail12306' && this.data.selTiers.length) {
      params.price_labels = this.data.selTiers.slice();
    }
    // 勾了的场次名（服务端记录用；轮询按价位过滤）
    const selSessions = this.data.sessions.filter((s) => s.selected).map((s) => s.name);
    if (selSessions.length && selSessions.length < this.data.sessions.length) {
      params.sessions = selSessions;
    }
    let docId = p + '-' + String(this.data.id).replace(/[^a-zA-Z0-9]/g, '').slice(-12);
    // 12306 同一车次不同日期是两个任务，docId 要带上日期防串
    if (p === 'rail12306') docId += '-' + String(this.data.railDate || '').replace(/[^0-9]/g, '');
    this.setData({ adding: true });
    try {
      saveTasks([
        ...loadTasks(),
        {
          _id: docId,
          adapter: p,
          name: this.data.name || '#' + this.data.id,
          params,
          enabled: true,
          poster: this.data.poster || '',
        },
      ]);
      Toast({ type: 'success', message: '已开始盯' });
      this.setData({ adding: false });
      setTimeout(() => wx.switchTab({ url: '/pages/monitor/monitor' }), 800);
    } catch (e) {
      this.setData({ adding: false });
      Dialog.alert({ title: '添加失败', message: String(e.message || e) });
    }
  },
});
