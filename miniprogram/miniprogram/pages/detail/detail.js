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
    // 巡演分站（大麦/淘票票父项目：真实 itemId + 城市 + 档期，点了进分站详情）
    stops: [],
    // 票档价格标签（只展示不勾选：猫眼匿名能读到档位，但逐档库存要凭证）
    priceTiers: [],
    subCount: 0,
    priceCount: 0,
    selTiers: [],
    subText: '盯这场演出',
  },

  onLoad(q) {
    // 解码保护（2026-10-19）：decodeURIComponent 遇到裸 % 会抛 URIError，
    // onLoad 一炸整个页面实例就废了——用户看到的就是一片全白（连骨架屏
    // 都不渲染，Console 也未必红字醒目）。逐字段 try/catch，坏参数降级为空串。
    const safe = (v) => {
      try {
        return decodeURIComponent(v == null ? '' : v);
      } catch (e) {
        return String(v == null ? '' : v);
      }
    };
    this.setData({
      platform: q.platform || '',
      id: q.id || '',
      label: safe(q.label),
      name: safe(q.name),
      poster: safe(q.poster),
      city: safe(q.city),
      venue: safe(q.venue),
      date: safe(q.date),
      price: safe(q.price),
      railFrom: safe(q.from),
      railTo: safe(q.to),
      railDate: safe(q.railDate),
    });
    wx.setNavigationBarTitle({ title: this.data.name ? this.data.name.slice(0, 12) : '演出详情' });
    this.loadDetail();
    // 兜底闹钟：wx.request 偶发在 devtools 里 success/fail 都不回调（网络栈
    // 抽风），页面就会永远 loading。30 秒还没出结果就强制亮报错卡，
    // 让用户至少能点「重试」——绝不许整页无声无息地白着。
    if (this._loadWatchdog) clearTimeout(this._loadWatchdog);
    this._loadWatchdog = setTimeout(() => {
      if (this.data.loading && !this.data.error && !this.data.degraded) {
        this.setData({
          loading: false,
          error: '详情请求一直没有回来（可能是开发者工具网络抽了）——点重试或下拉刷新',
        });
      }
    }, 30000);
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
    return request(req)
      .then((res) => {
        if (this._loadWatchdog) clearTimeout(this._loadWatchdog);
        const r = res.result || {};
        if (!r.ok) throw new Error(r.error || '未知错误');
        // 场次×票档：默认全选可用项（对齐竞品「共订阅 N 场 M 个价格」）
        const sessions = this.mapSessions(r.sessions);
        const rec = this.recount(sessions);
        // 一条真档位都没有 = 只能按整场盯（猫眼/大麦/摩天轮的匿名通道都到这）。
        // 服务端给了 noTiers 就用它的；没给但筛完确实没档位，也照实说。
        const noTiers = !!r.noTiers || (sessions.length > 0 && sessions.every((s) => !s.tiers.length));
        // 服务端降级：只有基础信息、没有场次票档（大麦风控 / 平台要登录）。
        // 关键点：**降级不等于死路**——一条都拿不到时也得能按整场盯，
        // 否则用户点进来看到「没有数据」又退不出去，等于白跑一趟。
        if (r.degraded) {
          const stops = r.stops || [];
          // 巡演总条目没有自己的场次，但分站是真实的——别让页面看起来像坏了
          const dm = stops.length
            ? '这是巡回演出的总条目——点下面的分站看具体场次和票价'
            : (r.degradeMsg || '场次和票档暂时拿不到，先按整场盯——有动静会提醒你');
          this.setData(
            Object.assign(
              {
                loading: false,
                degraded: true,
                sessions,
                stops,
                name: r.name || this.data.name,
                venue: r.venue || this.data.venue,
                date: r.dateRange || this.data.date,
                poster: r.poster || this.data.poster,
                city: r.city || this.data.city,
                price: r.priceRange ? '票价区间 ' + r.priceRange : this.data.price,
                priceTiers: r.priceTiers || [],
                degradeMsg: dm,
                noTiers,
              },
              rec,
              sessions.length ? {} : { subText: '盯整场（有动静就提醒）' },
            ),
          );
          return;
        }
        this.setData(
          Object.assign(
            {
              loading: false,
              sessions,
              stops: r.stops || [],
              priceTiers: r.priceTiers || [],
              name: r.name || this.data.name,
              venue: r.venue || this.data.venue,
              poster: r.poster || this.data.poster,
              date: r.dateRange || this.data.date,
              city: r.city || this.data.city,
              // 大麦/淘票票匿名通道给的是整场价格区间，信息卡里亮出来
              price: r.priceRange ? '票价区间 ' + r.priceRange : this.data.price,
              saleNote: r.saleNote || '',
              noTiers,
            },
            rec,
          ),
        );
      })
      .catch((e) => {
        if (this._loadWatchdog) clearTimeout(this._loadWatchdog);
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

  /** 离开页面清掉兜底闹钟，别让它留着打空炮 */
  onUnload() {
    if (this._loadWatchdog) clearTimeout(this._loadWatchdog);
  },

  /** 服务端 sessions → 页面态。默认全选可用项（不可用的档位灰掉、不参与轮询）。 */
  mapSessions(raw) {
    return (raw || []).map((s) => {
      const name = s.name || '场次';
      // 假档位过滤：平台只给「项目级价格区间」时，上游可能把演出/场次全名当成
      // 唯一票档塞过来——名字跟场次一模一样就不是真档位（选了也没法按它过滤），
      // 直接丢掉，让这一场走「整场盯」。猫眼匿名通道就是这样，见用户截图反馈。
      const tiers = (s.tiers || [])
        .filter((t) => {
          const tn = String(t.name == null ? '' : t.name).trim();
          return !!tn && tn !== name;
        })
        .map((t) => ({
          name: t.name || '票档',
          price: t.price || '',
          status: t.status || '',
          available: t.available !== false,
          selected: t.available !== false,
        }));
      return {
        name,
        when: s.when || '',
        performId: s.performId || '',
        available: !!s.available,
        selected: true,
        tiers,
      };
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

  /** 巡演分站：点一站进那一站的详情（同平台、同 itemId 命名空间） */
  openStop(e) {
    const s = e.currentTarget.dataset.stop || {};
    if (!s.itemId) return;
    const q = [
      'platform=' + encodeURIComponent(this.data.platform),
      'id=' + encodeURIComponent(s.itemId),
      'name=' + encodeURIComponent(s.name ? this.data.name + ' ' + s.name : this.data.name),
      'label=' + encodeURIComponent(this.data.label),
      'city=' + encodeURIComponent(s.name || ''),
      'date=' + encodeURIComponent(s.when || ''),
    ];
    wx.navigateTo({ url: '/pages/detail/detail?' + q.join('&') });
  },

  /** 要按票档/库存盯 = 得填凭证；这里只负责把用户送到「我的」页 */
  gotoMine() {
    wx.switchTab({ url: '/pages/mine/mine' });
  },

  // 下拉刷新 = 重新拉一遍详情（场次/票档/巡演分站会跟着平台实时变化）
  onPullDownRefresh() {
    Promise.resolve(this.loadDetail()).then(() => wx.stopPullDownRefresh());
  },

  reload() {
    return this.loadDetail();
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
