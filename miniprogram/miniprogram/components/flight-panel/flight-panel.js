// 机票面板（内联在 shows 页的「机票」tab 里，不再跳独立页）
// ---------------------------------------------------------------------------
// 数据源：同程「价格日历」（后端 cloudfunctions/radar/flight.js）
//   · 匿名、免 Cookie、免签名，一次给整条航线 30/60 天逐日最低价
//   · 每条带航班号 + 起降时刻 + 折扣，用户能直接判断这班要不要
// 产品口径（不吹）：
//   · 「每条航线每天的最低价」是我们真能拿到的；「当天全部航班列表」匿名拿不到，
//     不硬凑假数据；
//   · 平台比价：同程可匿名取价，携程/去哪儿/飞猪/途牛都要登录态，
//     所以这里给「一键复制查询链接」让你去对应平台核对，而不是假装能比价。
const { request } = require('../../utils/api');
const { loadTasks, saveTasks } = require('../../utils/tasks');
const { renewAfterAdd } = require('../../utils/push');

// 旧版本把盯的航线单独存在 radar_flight_watch 里，只能在本页面看到、也不会被轮询。
// 现在统一进 utils/tasks 的监控任务列表（monitor 首页那一批），下面这个 key 只留着
// 做一次性迁移，不再写入。
const WATCH_KEY = 'radar_flight_watch';
const LAST_KEY = 'radar_flight_last';
const ROUTE_KEY = 'radar_flight_route';

/** 机票任务 id 前缀：monitor 页和这里靠 adapter === 'flight' 认，前缀只是好读 */
const TASK_PREFIX = 'flight-';

/** 从监控任务里挑出机票航线 */
function flightTasks() {
  const list = loadTasks();
  return Array.isArray(list) ? list.filter((t) => t && t.adapter === 'flight' && t._id) : [];
}

/** 任务 → 面板里「在盯的航线」那一行 */
function toWatch(t) {
  const p = t.params || {};
  return {
    id: t._id,
    from: p.from,
    to: p.to,
    fromName: p.fromName || p.from,
    toName: p.toName || p.to,
    budget: Number(p.budget) || 0, // 0 = 只看不提醒
    lastMin: Number(p.lastMin) || 0,
    bestDate: p.lastDate || '',
  };
}

function ymd(d) {
  const t = d instanceof Date ? d : new Date(d);
  const p = (n) => (n < 10 ? '0' + n : '' + n);
  return t.getFullYear() + '-' + p(t.getMonth() + 1) + '-' + p(t.getDate());
}

function offsetDay(off) {
  return ymd(new Date(Date.now() + (Number(off) || 0) * 86400000));
}

// 反算：某个日期离今天几天（用来点走势柱后同步「今天/明天/后天」高亮）
function diffDays(dateStr) {
  const t = new Date(String(dateStr).replace(/-/g, '/') + ' 00:00:00').getTime();
  if (!t) return -1;
  const b = new Date(ymd(new Date()).replace(/-/g, '/') + ' 00:00:00').getTime();
  return Math.round((t - b) / 86400000);
}

// 各平台的「查这条航线」链接（给复制用，不做假比价）
function platformLinks(from, to, date) {
  const d = date || ymd(new Date());
  const f = from || 'SHA';
  const t = to || 'BJS';
  return [
    {
      key: 'ly',
      name: '同程',
      note: '当前价格来源',
      url: 'https://www.ly.com/flights/itinerary/oneway/' + f + '-' + t + '?date=' + d,
    },
    {
      key: 'ctrip',
      name: '携程',
      note: '需登录才返价',
      url: 'https://flights.ctrip.com/online/list/oneway-' + f.toLowerCase() + '-' + t.toLowerCase() + '?depdate=' + d,
    },
    {
      key: 'qunar',
      name: '去哪儿',
      note: '需登录才返价',
      url: 'https://m.flight.qunar.com/ncs/page/flightlist?depCity=' + f + '&arrCity=' + t + '&goDate=' + d,
    },
  ];
}

Component({
  // 默认样式隔离：shows.wxss 里也有 .chip，apply-shared 会把两边混掉。
  // 全局 CSS 变量（--ink/--card/--r…）在 page 上定义，组件照样能继承。
  options: { styleIsolation: 'isolated' },

  data: {
    ready: false,
    from: null,
    to: null,
    today: '',
    startDate: '',
    dayOff: 1,
    days: 30,
    loading: false,
    err: '',
    errTitle: '没拿到价格',
    res: null,
    bars: [],
    deals: [],
    rows: [],
    rowsTop: [],
    listOpen: false,
    savePct: 0,
    // 城市选择
    cityOpen: false,
    citySide: 'from',
    cityKw: '',
    cityList: [],
    // 当天航班（含舱位）：与价格日历并行拉，互不阻塞
    flDate: '',
    flLoading: false,
    flErr: '',
    flights: [],
    flView: [],
    // 航班卡默认只铺前 5 班，否则 10 张卡把页面拖得很长，滑到底要看半天
    flShow: [],
    flMore: 0,
    flOpen: false,
    cabinFilter: 'all',
    sortBy: 'time',
    // 监控
    watches: [],
    // 当前这条航线有没有在盯、目标价多少（按钮上直接显示，不用滑到底看列表）
    watching: false,
    watchBudget: 0,
    watchText: '盯这条航线 · 到价提醒你',
    dropTip: null,
    platforms: [],
  },

  lifetimes: {
    attached() {
      const start = offsetDay(1);
      const saved = wx.getStorageSync(ROUTE_KEY);
      const from = (saved && saved.from) || { code: 'SHA', name: '上海' };
      const to = (saved && saved.to) || { code: 'BJS', name: '北京' };
      this.migrateLegacyWatches();
      this.setData({
        ready: true,
        today: offsetDay(0),
        startDate: start,
        dayOff: 1,
        from: from,
        to: to,
        watches: flightTasks().map(toWatch),
        platforms: platformLinks(from.code, to.code, start),
      });
      this.setWatches();
      // 关键修复：进场就自动查一次。上一版只设了默认城市、从不发请求，
      // 所以用户点进来永远是空白 —— 这就是「名存实亡」的根因。
      this.query();
    },
  },

  methods: {
    // ---------------- 城市 ----------------
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
      // utils/api.js 的 request() resolve 出的是 { result: <服务端JSON> }，
      // 不是服务端 JSON 本身。必须解包 res.result，否则 r.cities 永远是
      // undefined —— 表现就是不管输什么都「没匹配到」。
      request({ action: 'flightCities', keyword: kw })
        .then((res) => {
          const r = (res && res.result) || {};
          this.setData({ cityList: r.cities || [] });
        })
        .catch(() => this.setData({ cityList: [] }));
    },
    pickCity(e) {
      const c = e.currentTarget.dataset.city;
      if (!c) return;
      const patch = { cityOpen: false, res: null, bars: [], deals: [], rows: [], err: '' };
      patch[this.data.citySide] = { code: c.code, name: c.name };
      this.setData(patch);
      this.syncPlatforms();
      this.query();
    },
    swap() {
      this.setData({
        from: this.data.to,
        to: this.data.from,
        res: null,
        bars: [],
        deals: [],
        rows: [],
        err: '',
      });
      this.syncPlatforms();
      this.query();
    },
    /** 把「在盯的航线」和按钮状态一起刷新，避免两处各写一遍 */
    setWatches() {
      const list = flightTasks().map(toWatch);
      const { from, to } = this.data;
      const id = TASK_PREFIX + (from && from.code) + '-' + (to && to.code);
      const cur = list.filter((w) => w.id === id)[0];
      const budget = cur ? Number(cur.budget) || 0 : 0;
      // 按钮文案在 js 里拼好：多层三元 + 字符串拼接塞进 wxml 在一些基础库上会解析歪
      let watchText = '盯这条航线 · 到价提醒你';
      if (cur) {
        watchText = budget
          ? '已在盯 · 低于 ¥' + budget + ' 提醒你 · 点这里改'
          : '已在盯 · 只记价不提醒 · 点这里改';
      }
      this.setData({
        watches: list,
        watching: !!cur,
        watchBudget: budget,
        watchText: watchText,
      });
      return list;
    },

    syncPlatforms() {
      this.setWatches();
      const { from, to, startDate, res } = this.data;
      // from/to 还没填时用结果里的城市码兜底（首屏只走 query、没走 pickCity 的情况）
      const f = from || (res && { code: res.fromCode, name: res.fromName });
      const t = to || (res && { code: res.toCode, name: res.toName });
      if (!f || !t || !f.code || !t.code) return;
      this.setData({ platforms: platformLinks(f.code, t.code, startDate) });
    },

    // ---------------- 时间 ----------------
    setDay(e) {
      const off = Number(e.currentTarget.dataset.off) || 0;
      this.setData({ startDate: offsetDay(off), dayOff: off, res: null, bars: [], deals: [], rows: [] });
      this.syncPlatforms();
      this.query();
    },
    onDate(e) {
      this.setData({ startDate: e.detail.value, dayOff: -1, res: null, bars: [], deals: [], rows: [] });
      this.syncPlatforms();
      // 原来这里只 setData 不发请求，选了日期页面还是旧数据，等于白选
      this.query();
    },
    onDays(e) {
      const d = Number(e.currentTarget.dataset.d) || 30;
      if (d === this.data.days) return;
      this.setData({ days: d, res: null, bars: [], deals: [], rows: [] });
      this.query();
    },

    // ---------------- 查询 ----------------
    query() {
      const { from, to, startDate, days, loading } = this.data;
      if (loading || !from || !to) return;
      if (from.code === to.code) {
        this.setData({ err: '出发和到达不能是同一个城市' });
        return;
      }
      this.setData({ loading: true, err: '', errTitle: '没拿到价格' });
      // 价格日历和「当天航班」是两条独立请求，各走各的，谁先回来先渲染
      this.queryFlights();
      request({ action: 'flight', from: from.code, to: to.code, date: startDate, days })
        .then((res) => {
          // 同 loadCities：先取 res.result。不取的话 r.ok 是 undefined，
          // 会被误判成「拿不到价格」——数据其实是好的，白报错。
          const r = (res && res.result) || {};
          if (!r.ok) throw new Error(r.error || '拿不到价格');
          this.applyRes(r, from, to);
          // 降价提示在 applyRes -> recordLast 里已经做过了，这里不要再调
          // checkDrop —— 那个方法从来就没定义过，调用只会抛 TypeError，
          // 被下面的 catch 接住后把刚拿到的 bars/deals/rows 全清空。
          this.setData({ loading: false });
        })
        .catch((e) => {
          const msg = (e && e.message) || '网络错误，稍后再试';
          this.setData({
            loading: false,
            res: null,
            bars: [],
            deals: [],
            rows: [],
            err: msg,
            // 连不上服务（本地 server 没跑 / 域名没进白名单）和数据源挂了
            // 是两回事，标题分开写，不然每次都得猜。
            errTitle: /连不上/.test(msg) ? '连不上服务' : '没拿到价格',
          });
        });
    },

    applyRes(r, from, to) {
      const rows = r.rows || [];
      const min = r.stats.min;
      const avg = r.stats.avg;
      // 柱高：以 P90 为上限锚点，最低价 22rpx、最高 88rpx（≈11~44px）。
      // 为什么不用 max 直接归一：机票常有「今天飞 ¥1600、下个月 ¥360」这种
      // 异常值，用 max 归一会把 29 根柱子压成一样高，走势就看不出来了。
      // 柱高本身放回 88rpx——「太长」不是柱子的问题，是白卡被 scroll-view
      // 撑到了整屏（见 wxss 的 .chart）。卡片高度写死后，柱子可以留高一点。
      const sorted = rows.map((x) => x.price).sort((a, b) => a - b);
      const p90 = sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.9))] || min;
      const span = Math.max(1, p90 - min);
      const bars = rows.map((x) => ({
        date: x.date,
        dd: x.date.slice(5),
        w: x.week,
        price: x.price,
        h: Math.max(22, Math.min(88, Math.round(22 + ((x.price - min) / span) * 66))),
        low: x.price === min,
      }));
      // 捡漏日：低于均价 12%（原先是 20%，太严——实测 SHA-BJS 最低只比均价
      // 低 19%，结果一条都筛不出来，页面上就是空的）。按价格升序最多 6 条。
      let deals = rows
        .filter((x) => x.price <= avg * 0.88)
        .sort((a, b) => a.price - b.price)
        .slice(0, 6)
        .map((x) => Object.assign({}, x, { save: Math.round(((avg - x.price) / avg) * 100) }));
      // 兜底：整条航线价格很平的时候（没有一天低于均价 12%），
      // 至少把最便宜的那天拿出来，不让「捡漏日」整块空着。
      if (!deals.length && r.stats.cheapest) {
        const c = r.stats.cheapest;
        deals = [Object.assign({}, c, { save: Math.round(((avg - c.price) / avg) * 100) })];
      }

      // 记住这条航线（存用户选的城市名，不存后端机场全名，否则第二次进来
      // 卡片上会顶出「上海虹桥国际机场」这种超长标题）
      const code = from.code + '-' + to.code;
      wx.setStorageSync(ROUTE_KEY, {
        from: { code: from.code, name: from.name },
        to: { code: to.code, name: to.name },
      });
      // 后端给的是机场全名（"上海虹桥国际机场"），展示时收成城市名
      this.setData({
        res: r,
        bars: bars,
        deals: deals,
        rows: rows,
        rowsTop: rows.slice(0, 6),
        savePct: Math.round(((avg - min) / avg) * 100),
        fromName: r.fromName || from.name,
        toName: r.toName || to.name,
      });
      this.recordLast(code, min, r.stats.cheapest);
      this.syncPlatforms();
    },

    // ---------------- 当天航班 + 舱位 ----------------
    // 数据源：同程列表页的 Nuxt SSR（后端 action=flightList）
    // 每个航班自带 cfts 舱位数组：经济舱 / 公务舱 / 头等舱，各带价格与折扣
    queryFlights() {
      const { from, to, startDate } = this.data;
      if (!from || !to || from.code === to.code) return;
      const day = startDate || offsetDay(1);
      this.setData({ flLoading: true, flErr: '', flDate: day });
      request({ action: 'flightList', from: from.code, to: to.code, date: day })
        .then((res) => {
          const r = (res && res.result) || {};
          if (!r.ok) throw new Error(r.error || '拿不到航班');
          this.setData({ flights: r.flights || [], flLoading: false });
          this.buildView();
        })
        .catch((e) => {
          this.setData({
            flights: [],
            flView: [],
            flLoading: false,
            flErr: (e && e.message) || '航班没拿到，稍后再试',
          });
        });
    },

    // 按舱位筛选 + 排序后的视图（wxml 只负责渲染，不在模板里算）
    buildView() {
      const { flights, cabinFilter, sortBy, flOpen } = this.data;
      const list = flights
        .map((f) => {
          const cab = (f.cabins || []).filter(
            (c) => cabinFilter === 'all' || String(c.cls).indexOf(cabinFilter) >= 0,
          );
          // 同程偶尔只给一个最低价、不给分舱明细。原来这种航班会被下面的
          // filter 直接丢掉，页面就出现「共 10 班」的接口数据却显示「共 0 班」。
          // 兜一格「经济舱」占位，价格用最低价，至少不丢航班。
          if (!cab.length && cabinFilter === 'all' && f.low) {
            cab.push({ name: f.lowDesc || '', cls: '经济舱', code: 'low', price: f.low, discount: '' });
          }
          return Object.assign({}, f, {
            cabList: cab,
            showLow: cab.length ? cab[0].price : f.low,
            // 舱位多的时候只显示前 2 档，点展开再看全部
            cabTop: cab.slice(0, 2),
            cabMore: cab.length > 2 ? cab.length - 2 : 0,
          });
        })
        .filter((f) => f.cabList.length);
      if (sortBy === 'price') list.sort((a, b) => a.showLow - b.showLow);
      else list.sort((a, b) => String(a.dep).localeCompare(String(b.dep)));
      const LIMIT = 5;
      this.setData({
        flView: list,
        flShow: flOpen || list.length <= LIMIT ? list : list.slice(0, LIMIT),
        flMore: Math.max(0, list.length - LIMIT),
      });
    },

    toggleFlights() {
      this.setData({ flOpen: !this.data.flOpen });
      this.buildView();
    },

    setCabin(e) {
      const v = e.currentTarget.dataset.v || 'all';
      if (v === this.data.cabinFilter) return;
      this.setData({ cabinFilter: v });
      this.buildView();
    },

    setSort(e) {
      const v = e.currentTarget.dataset.v || 'time';
      if (v === this.data.sortBy) return;
      this.setData({ sortBy: v });
      this.buildView();
    },

    // 点走势里某一根柱子 → 直接切到那天，并把「当天航班」换成那天的
    // （只重拉航班，不重拉 30 天日历，省一次请求）
    pickBar(e) {
      const d = e.currentTarget.dataset.date;
      if (!d) return;
      const off = diffDays(d);
      const dayOff = off === 0 || off === 1 || off === 2 ? off : -1;
      this.setData({ startDate: d, dayOff: dayOff });
      this.queryFlights();
    },

    toggleCabins(e) {
      const i = Number(e.currentTarget.dataset.i);
      const cur = this.data.flView[i];
      if (!cur) return;
      const v = !cur.cabOpen;
      const patch = {};
      patch['flView[' + i + '].cabOpen'] = v;
      // 视图渲染的是 flShow（前 5 班），两处都要设，否则点了没反应
      if (i < this.data.flShow.length) patch['flShow[' + i + '].cabOpen'] = v;
      this.setData(patch);
    },

    retryFlights() {
      this.queryFlights();
    },

    toggleList() {
      this.setData({ listOpen: !this.data.listOpen });
    },

    // ---------------- 降价追踪（本地航线簿）----------------
    recordLast(code, min, cheapest) {
      const map = wx.getStorageSync(LAST_KEY) || {};
      const prev = map[code];
      map[code] = { price: min, date: cheapest ? cheapest.date : '', at: Date.now() };
      wx.setStorageSync(LAST_KEY, map);
      // 同一航线，这次比上次记录的低 → 主动提示
      if (prev && prev.price > min) {
        this.setData({
          dropTip: {
            code: code,
            drop: prev.price - min,
            now: min,
            before: prev.price,
            date: cheapest ? cheapest.date : '',
          },
        });
      }
    },

    dismissDrop() {
      this.setData({ dropTip: null });
    },

    // 盯一条航线 = 往监控任务表里加一条 adapter='flight' 的任务。
    // 这样首页「监控」列表能看到它、能被 poll 轮询、跌破目标价能走订阅消息推送——
    // 上一版只写在本地的 radar_flight_watch 里，关掉小程序就什么都不会发生。
    // 盯一条航线 = 往监控任务表里加一条 adapter='flight' 的任务。
    // 这样首页「监控」列表能看到它、能被 poll 轮询、跌破目标价能走订阅消息推送——
    // 上一版只写在本地的 radar_flight_watch 里，关掉小程序就什么都不会发生。
    // 已经盯着的再点一次 = 改目标价（不用先删再加）。
    addWatch() {
      const { res, from, to } = this.data;
      if (!res) return wx.showToast({ title: '先等价格出来', icon: 'none' });
      const id = TASK_PREFIX + from.code + '-' + to.code;
      const tasks = loadTasks() || [];
      const exists = tasks.filter((t) => t && t._id === id)[0] || null;
      const curBudget = exists ? Number(exists.params && exists.params.budget) || 0 : 0;
      const min = Number(res.stats && res.stats.min) || 0;
      const cheap = (res.stats && res.stats.cheapest) || {};
      // 默认建议价：当前最低价再低 10%，取到十位（¥360 → ¥320）。
      // 只是预填，用户可以改成任何数；留空则「只看不提醒」。
      const suggest = min ? Math.floor((min * 0.9) / 10) * 10 : '';
      wx.showModal({
        title: (exists ? '改目标价 · ' : '盯 ') + from.name + ' → ' + to.name,
        editable: true,
        placeholderText: '低于多少提醒你，留空就只看不提醒',
        content: curBudget ? String(curBudget) : suggest ? String(suggest) : '',
        confirmText: exists ? '保存' : '开始盯',
        success: (r) => {
          if (!r.confirm) return;
          const n = Number(String(r.content || '').replace(/[^0-9.]/g, ''));
          const budget = n > 0 ? Math.round(n) : 0;
          if (exists) {
            exists.params = Object.assign({}, exists.params, {
              budget: budget,
              lastMin: min,
              lastDate: cheap.date || '',
            });
            saveTasks(tasks);
          } else {
            tasks.unshift({
              _id: id,
              adapter: 'flight',
              name: from.name + ' → ' + to.name,
              params: {
                from: from.code,
                to: to.code,
                fromName: from.name,
                toName: to.name,
                days: 30,
                budget: budget,
                lastMin: min,
                lastDate: cheap.date || '',
              },
              poster: '',
              enabled: true,
              created_at: Date.now(),
            });
            saveTasks(tasks);
            // 机票的「到价」推送走的是同一条一次性订阅，加完顺手静默续一条
            renewAfterAdd().catch(() => {});
          }
          this.setWatches();
          wx.showToast({
            title: exists
              ? budget
                ? '目标价改成 ¥' + budget
                : '已取消价格提醒'
              : budget
                ? '已盯上，低于 ¥' + budget + ' 提醒你'
                : '已盯上，只记价不提醒',
            icon: 'none',
          });
        },
      });
    },

    removeWatch(e) {
      const id = e.currentTarget.dataset.id;
      saveTasks((loadTasks() || []).filter((t) => !(t && t._id === id)));
      this.setWatches();
    },

    useWatch(e) {
      const w = e.currentTarget.dataset.w;
      if (!w) return;
      this.setData({
        from: { code: w.from, name: w.fromName || w.from },
        to: { code: w.to, name: w.toName || w.to },
        res: null,
        bars: [],
        deals: [],
        rows: [],
      });
      wx.setStorageSync(ROUTE_KEY, {
        from: { code: w.from, name: w.fromName || w.from },
        to: { code: w.to, name: w.toName || w.to },
      });
      this.query();
    },

    /** 把早先存在 radar_flight_watch 里的航线搬进监控任务表（只跑一次） */
    migrateLegacyWatches() {
      const legacy = wx.getStorageSync(WATCH_KEY);
      if (!Array.isArray(legacy) || !legacy.length) return;
      const tasks = loadTasks() || [];
      legacy.forEach((w) => {
        if (!w || !w.from || !w.to) return;
        const id = TASK_PREFIX + w.from + '-' + w.to;
        if (tasks.some((t) => t && t._id === id)) return;
        tasks.push({
          _id: id,
          adapter: 'flight',
          name: (w.fromName || w.from) + ' → ' + (w.toName || w.to),
          params: {
            from: w.from,
            to: w.to,
            fromName: w.fromName || w.from,
            toName: w.toName || w.to,
            days: 30,
            // 旧记录没有目标价，不替用户瞎设一个——当「只看不提醒」搬过来
            budget: 0,
            lastMin: Number(w.best) || 0,
            lastDate: w.bestDate || '',
          },
          poster: '',
          enabled: true,
          created_at: w.at || Date.now(),
        });
      });
      saveTasks(tasks);
      wx.removeStorageSync(WATCH_KEY);
    },

    // 打开面板顺手比自己上次看到的最低价（本地快照，跨会话）。
    // 真正的推送不靠这里：monitor 页的 poll 才是 7×24 那条线。
    refreshWatches() {
      // 首页可能刚删过任务，进来先把卡片对齐一遍
      const list = this.setWatches();
      if (!list.length) return;
      const last = wx.getStorageSync(LAST_KEY) || {};
      let drop = null;
      let done = 0;
      list.slice(0, 6).forEach((w) => {
        const p = w.params || {};
        request({ action: 'flight', from: p.from, to: p.to, days: Number(p.days) || 30 })
          .then((res) => {
            const r = (res && res.result) || {};
            if (!r.ok || !r.stats) return;
            const min = r.stats.min;
            const prev = last[w._id];
            if (prev && prev.price > min && (!drop || prev.price - min > drop.drop)) {
              drop = {
                code: w._id,
                drop: prev.price - min,
                now: min,
                before: prev.price,
                date: r.stats.cheapest ? r.stats.cheapest.date : '',
              };
            }
            last[w._id] = {
              price: min,
              date: r.stats.cheapest ? r.stats.cheapest.date : '',
              at: Date.now(),
            };
            wx.setStorageSync(LAST_KEY, last);
            // 记下这次看到的最低价，卡片上显示「上次 ¥xxx」，不用再查一次
            const tasks = loadTasks() || [];
            const t = tasks.filter((x) => x && x._id === w._id)[0];
            if (t && t.params) {
              t.params.lastMin = min;
              t.params.lastDate = r.stats.cheapest ? r.stats.cheapest.date : '';
              saveTasks(tasks);
            }
          })
          .catch(() => {})
          .then(() => {
            done += 1;
            if (done === Math.min(6, list.length)) {
              this.setWatches();
              if (drop) this.setData({ dropTip: drop });
            }
          });
      });
    },

    // ---------------- 平台比价（复制链接，不做假数据）----------------
    copyLink(e) {
      const url = e.currentTarget.dataset.url;
      if (!url) return;
      wx.setClipboardData({
        data: url,
        success: () => wx.showToast({ title: '链接已复制，去浏览器打开', icon: 'none' }),
      });
    },

    noop() {},

    onPageShow() {
      this.refreshWatches();
    },
  },
});
