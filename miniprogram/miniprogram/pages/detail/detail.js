// 演出详情页：云函数 detail action 拉场次×票档，「盯这场」写 radar_tasks。
import Toast from '/vant/toast/toast';
import { request } from '../utils/api';
import { loadTasks, saveTasks } from '../utils/tasks';
import Dialog from '/vant/dialog/dialog';

const CREDIT_KEY = 'radar_credit';

Page({
  data: {
    loading: true,
    error: '',
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
    sessions: [],
  },

  onLoad(q) {
    this.setData({
      platform: q.platform || '',
      id: q.id || '',
      label: q.label || '',
      name: decodeURIComponent(q.name || ''),
      poster: decodeURIComponent(q.poster || ''),
      city: decodeURIComponent(q.city || ''),
      venue: decodeURIComponent(q.venue || ''),
      date: decodeURIComponent(q.date || ''),
      price: decodeURIComponent(q.price || ''),
    });
    wx.setNavigationBarTitle({ title: this.data.name ? this.data.name.slice(0, 12) : '演出详情' });
    this.loadDetail();
  },

  loadDetail() {
    this.setData({ loading: true, error: '' });
request({ action: 'detail', platform: this.data.platform, id: this.data.id })
      .then((res) => {
        const r = res.result || {};
        if (!r.ok) throw new Error(r.error || '未知错误');
        this.setData({
          loading: false,
          sessions: r.sessions || [],
          name: r.name || this.data.name,
          venue: r.venue || this.data.venue,
        });
      })
      .catch((e) => {
        this.setData({ loading: false, error: String(e.message || e) });
      });
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
    else {
      Toast('这个平台暂不能直接盯');
      return;
    }
    const docId = p + '-' + String(this.data.id).replace(/[^a-zA-Z0-9]/g, '').slice(-12);
    this.setData({ adding: true });
saveTasks([...loadTasks(), { _id: docId, adapter: p, name: this.data.name || ('#' + this.data.id), params, enabled: true, poster: this.data.poster || '' }])
      })
      .then(() => {
        Toast({ type: 'success', message: '已开始盯' });
        Toast({ type: "success", message: "已开始盯" });
        this.setData({ adding: false });
        setTimeout(() => wx.switchTab({ url: "/pages/monitor/monitor" }), 800);