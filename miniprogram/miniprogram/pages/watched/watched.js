// 「正在盯」独立页：从「我的」页入口进来看正在监控的演出/车次。
// 数据 = 本地任务表 + monitor 页最近一轮轮询快照（radar_last_states），
// 纯本地拼装展示，不发任何请求；下架的置灰划线沉底，支持移除。
import Toast from '../../vant/toast/toast';
import { loadTasks, removeTask } from '../../utils/tasks';

// monitor 页每轮轮询后把结果快照存这里，本页直接读
const LAST_STATES_KEY = 'radar_last_states';
const PREV_KEY = 'radar_prev_states';

const PLATFORM_LABEL = {
  damai: '大麦', bilibili: 'B站会员购', maoyan: '猫眼',
  moretickets: '摩天轮', piaoxingqiu: '票星球', rail12306: '12306',
  fenwandao: '纷玩岛',
};

Page({
  data: {
    watched: [],
  },

  onShow() {
    // 每次进来重读：监控页可能刚刷过一轮，状态会变
    this.loadWatched();
  },

  // 下拉刷新：本页是本地任务表 + 上一轮快照拼的，重读一次即可。
  // 想拿最新余票还是回首页下拉（那边会真的去打平台接口）。
  onPullDownRefresh() {
    this.loadWatched();
    wx.stopPullDownRefresh();
  },

  loadWatched() {
    const tasks = loadTasks();
    const states = wx.getStorageSync(LAST_STATES_KEY) || [];
    const smap = {};
    for (const s of states) smap[s._id] = s;
    const watched = tasks.map((t) => {
      const s = smap[t._id];
      let status = 'wait';
      let statusText = '还没查过';
      let offline = false;
      if (s) {
        if (s.ok) {
          status = s.available > 0 ? 'ok' : 'none';
          statusText = s.available > 0 ? '有票 ' + s.available : '无票';
        } else if (/404|不存在|已下架|下线/.test(String(s.error || ''))) {
          status = 'off';
          statusText = '已下架';
          offline = true;
        } else {
          status = 'bad';
          statusText = '抓取失败';
        }
      }
      return {
        _id: t._id,
        name: t.name || '监控',
        platform: PLATFORM_LABEL[t.adapter] || t.adapter,
        adapter: t.adapter || '',
        params: t.params || {},
        poster: t.poster || '',
        status,
        statusText,
        offline,
        rel: (s && s.relTime) || '',
      };
    });
    // 下架的沉底，别的按原顺序
    watched.sort((a, b) => (a.offline === b.offline ? 0 : a.offline ? 1 : -1));
    this.setData({ watched });
  },

  /** 点条目 → 跳到对应详情。演出类进 detail 页；12306 回查票页并预填站名。 */
  openWatched(e) {
    const idx = Number(e.currentTarget.dataset.index);
    const it = this.data.watched[idx];
    if (!it) return;
    if (it.adapter === 'rail12306') {
      const p = it.params || {};
      // 查票页是 tab 页不能带参，先存预填再 switchTab
      wx.setStorageSync('shows_prefill_rail', {
        from: p.from || '',
        to: p.to || '',
        date: p.date || '',
      });
      wx.switchTab({ url: '/pages/shows/shows' });
      return;
    }
    const pid = it.params.project_id || it.params.performance_id || it.params.tour_id
      || it.params.item_id || it.params.show_id || '';
    if (!pid) {
      Toast({ message: '这条任务缺 id，跳不了详情', type: 'none' });
      return;
    }
    const q =
      'platform=' + it.adapter +
      '&id=' + pid +
      '&name=' + encodeURIComponent(it.name || '') +
      '&poster=' + encodeURIComponent(it.poster || '');
    wx.navigateTo({ url: '/pages/detail/detail?' + q });
  },

  removeWatched(e) {
    const id = e.currentTarget.dataset.id;
    const item = (this.data.watched || []).find((x) => x._id === id);
    wx.showModal({
      title: '移除监控',
      content: '不再盯「' + ((item && item.name) || id) + '」？',
      confirmText: '移除',
      success: (res) => {
        if (!res.confirm) return;
        removeTask(id);
        // 快照同步清掉，避免残留旧状态
        const prev = wx.getStorageSync(PREV_KEY) || {};
        delete prev[id];
        wx.setStorageSync(PREV_KEY, prev);
        wx.setStorageSync(
          LAST_STATES_KEY,
          (wx.getStorageSync(LAST_STATES_KEY) || []).filter((s) => s._id !== id)
        );
        Toast({ type: 'success', message: '已移除' });
        this.loadWatched();
      },
    });
  },
});
