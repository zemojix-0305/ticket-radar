// 演出详情页：云函数 detail action 拉场次×票档，「盯这场」写 radar_tasks。
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
    // 降级模式：详情接口被风控拦住时，用跳转带过来的搜索信息渲染基础详情，
    // 场次/票档/盯票不可用
    degraded: false,
    degradeMsg: '',
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
    // 12306 查询上下文（shows 页跳转时带上；详情=单趟车次全席别余票必需）
    railFrom: '',
    railTo: '',
    railDate: '',
    sessions: [],
    // 票档扁平列表（去重），供「只盯这个价位」选择；空 = 该演出没票档粒度
    tiers: [],
    // 选中的价位关键字；空字符串 = 盯整场
    tierSel: '',
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
    this.setData({ loading: true, error: '', degraded: false, degradeMsg: '', tierSel: '' });
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
        // 服务端 SSR 降级成功：演出信息（名称/场馆/日期）来自大麦官方 PC 页，
        // 只是拿不到场次×票档。照常渲染，盯票区隐藏。
        if (r.degraded) {
          this.setData({
            loading: false,
            degraded: true,
            sessions: [],
            tiers: [],
            name: r.name || this.data.name,
            venue: r.venue || this.data.venue,
            date: r.dateRange || this.data.date,
            degradeMsg: '场次和票档暂时拿不到（大麦限制），演出信息先看看',
          });
          return;
        }
        // 场次里的票档压平去重（按名字），给「只盯某个价位」的选择条用
        const seen = {};
        const tiers = [];
        for (const s of r.sessions || []) {
          for (const t of s.tiers || []) {
            const key = t.name || '';
            if (!key || seen[key]) continue;
            seen[key] = true;
            tiers.push({ name: key, price: t.price || '' });
          }
        }
        this.setData({
          loading: false,
          sessions: r.sessions || [],
          tiers,
          name: r.name || this.data.name,
          venue: r.venue || this.data.venue,
        });
      })
      .catch((e) => {
        // 大麦风控拦详情接口时（RGV587），别整页报错——搜索结果点进来时
        // 名称/城市/场馆/价格/海报都随 URL 带上了，用这些降级渲染，
        // 只是拿不到场次×票档，盯不了票。其余平台保持原样报错。
        if (this.data.platform === 'damai' && this.data.name) {
          this.setData({
            loading: false,
            degraded: true,
            degradeMsg: '场次和票档暂时拿不到（大麦太挤了），稍后再来',
          });
          return;
        }
        this.setData({ loading: false, error: String(e.message || e) });
      });
  },

  /** 选/取消一个价位。选中后只盯这个价位；再点一下取消回「盯整场」。 */
  onPickTier(e) {
    const name = e.currentTarget.dataset.name || '';
    this.setData({ tierSel: this.data.tierSel === name ? '' : name });
  },

  reload() {
    this.loadDetail();
  },

  /** 盯这场：和演出页同一套建库逻辑，参数名按平台区分 */
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
    else if (p === 'damai') params = { item_id: this.data.id };
    else if (p === 'bilibili') params = { project_id: this.data.id };
    else if (p === 'piaoxingqiu') params = { show_id: this.data.id };
    else if (p === 'fenwandao') params = { project_id: this.data.id };
    else if (p === 'rail12306') {
      if (!this.data.railFrom || !this.data.railTo || !this.data.railDate) {
        Toast('缺站名/日期——从查票页点车次进来才能盯');
        return;
      }
      params = { from: this.data.railFrom, to: this.data.railTo, date: this.data.railDate };
      // 选了席别就只盯这个席别（12306 的"价位"就是席别）
      if (this.data.tierSel) params.seat_types = [this.data.tierSel];
    }
    else {
      Toast('这个平台暂不能直接盯');
      return;
    }
    // 选了价位就只盯这个价位（服务端 poll 会按关键字过滤 items）
    if (this.data.tierSel && p !== 'rail12306') params.price_label = this.data.tierSel;
    let docId = p + '-' + String(this.data.id).replace(/[^a-zA-Z0-9]/g, '').slice(-12);
    // 12306 同一车次不同日期是两个任务，docId 要带上日期防串
    if (p === 'rail12306') docId += '-' + String(this.data.railDate || '').replace(/[^0-9]/g, '');
    this.setData({ adding: true });
    try {
      saveTasks([...loadTasks(), { _id: docId, adapter: p, name: this.data.name || ('#' + this.data.id), params, enabled: true, poster: this.data.poster || '' }]);
      Toast({ type: 'success', message: '已开始盯' });
      this.setData({ adding: false });
      setTimeout(() => wx.switchTab({ url: '/pages/monitor/monitor' }), 800);
    } catch (e) {
      this.setData({ adding: false });
      Dialog.alert({ title: '添加失败', message: String(e.message || e) });
    }
  },
});