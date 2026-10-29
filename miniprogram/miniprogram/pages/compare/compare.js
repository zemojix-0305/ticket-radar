// 跨平台比价页：同一场演出，各平台价格/余票一目了然。
// 入口：监控页点「跨平台比价」按钮 → 跳到本 page，输入演出名或艺人名。
import Toast from '../../vant/toast/toast';
import { request } from '../../utils/api';

const PLATFORM_LABEL = {
  damai: '大麦', taopiaopiao: '淘票票', maoyan: '猫眼',
  moretickets: '摩天轮', bilibili: 'B站', piaoxingqiu: '票星球',
};

// 状态颜色：有票=绿，售罄=灰，异常=红
function statusCls(row) {
  const s = String(row.status || '').toLowerCase();
  if (/有票|在售|可售|可购/.test(s)) return 'tag-ok';
  if (/售罄|不可购|停售/.test(s)) return 'tag-bad';
  return 'tag-none';
}

Page({
  data: {
    kw: '',
    loading: false,
    error: '',
    groups: [],     // 合并后的演出组
    searching: false,
  },

  onInput(e) { this.setData({ kw: e.detail.value }); },

  onSearch() {
    const kw = (this.data.kw || '').trim();
    if (!kw) { Toast('输入演出名或艺人名'); return; }
    this.setData({ loading: true, error: '', groups: [] });
    request({ action: 'compare', keyword: kw, limit: 5 })
      .then((res) => {
        const r = res.result || {};
        if (!r.ok) throw new Error(r.error || '未知错误');
        this.setData({ groups: r.merged || [], loading: false });
        if (!r.merged || !r.merged.length) {
          this.setData({ error: '没找到「' + kw + '」在任一平台的演出，换个关键词试试' });
        }
      })
      .catch((e) => {
        this.setData({ loading: false, error: String(e.message || e) });
      });
  },

  /** 点某平台某行 → 跳详情页 */
  goDetail(e) {
    const row = e.currentTarget.dataset.row;
    if (!row || !row._cmpPlatform || !row.performanceId) { Toast('这条数据不完整'); return; }
    const q = [
      'platform=' + encodeURIComponent(row._cmpPlatform),
      'id=' + encodeURIComponent(row.performanceId),
      'name=' + encodeURIComponent(row.name || ''),
      'poster=' + encodeURIComponent(row.poster || ''),
      'city=' + encodeURIComponent(row.city || ''),
      'venue=' + encodeURIComponent(row.venue || ''),
      'price=' + encodeURIComponent(row.price || ''),
      'date=' + encodeURIComponent(row.dateRange || ''),
      'label=' + encodeURIComponent(PLATFORM_LABEL[row._cmpPlatform] || row._cmpPlatform),
    ];
    wx.navigateTo({ url: '/pages/detail/detail?' + q.join('&') });
  },

  /** 整组加监控：取第一行的平台+id */
  addMonitor(e) {
    const group = e.currentTarget.dataset.group;
    if (!group || !group.rows[0]) { Toast('没有可选的演出'); return; }
    const row = group.rows[0];
    const p = row._cmpPlatform;
    const id = row.performanceId || row.id || '';
    if (!id) { Toast('这条没有 id'); return; }
    // 构建 params 按平台
    let params = {};
    if (p === 'damai' || p === 'taopiaopiao') params = { item_id: id };
    else if (p === 'maoyan') params = { performance_id: id };
    else if (p === 'moretickets') params = { tour_id: id };
    else if (p === 'bilibili') params = { project_id: id };
    else if (p === 'piaoxingqiu') params = { show_id: id };
    else { Toast('该平台暂不支持直接加监控'); return; }
    const tasks = require('../../utils/tasks');
    const taskId = p + '-' + String(id).replace(/[^a-zA-Z0-9]/g, '').slice(-12);
    try {
      const existing = tasks.loadTasks();
      const idx = existing.findIndex((t) => t._id === taskId);
      const task = { _id: taskId, adapter: p, name: group.name || row.name, params, enabled: true, poster: row.poster || '' };
      if (idx >= 0) existing[idx] = task; else existing.push(task);
      tasks.saveTasks(existing);
      Toast({ type: 'success', message: '已开始盯「' + (group.name || '').slice(0, 8) + '」' });
      setTimeout(() => wx.switchTab({ url: '/pages/monitor/monitor' }), 800);
    } catch (err) {
      Toast('添加失败：' + String(err.message || err));
    }
  },

  clear() {
    this.setData({ kw: '', groups: [], error: '', loading: false });
  },

  /** 标记每组中价格最低的行（用于 UI 高亮） */
  isBestPrice(group, index) {
    if (!group || !group.rows || group.rows.length <= 1) return false;
    const row = group.rows[index];
    const minPrice = this._minPrice(group);
    return minPrice !== null && this._extractPrice(row.price) === minPrice;
  },

  _minPrice(group) {
    let min = null;
    for (const r of group.rows) {
      const p = this._extractPrice(r.price);
      if (p !== null && (min === null || p < min)) min = p;
    }
    return min;
  },

  _extractPrice(priceStr) {
    const m = String(priceStr || '').match(/(\d+)/);
    return m ? Number(m[1]) : null;
  },

  /** 快捷搜索热门艺人 */
  quickSearch(e) {
    const kw = e.currentTarget.dataset.kw || '';
    if (!kw) return;
    this.setData({ kw, loading: true, error: '', groups: [] });
    request({ action: 'compare', keyword: kw, limit: 5 })
      .then((res) => {
        const r = res.result || {};
        if (!r.ok) throw new Error(r.error || '未知错误');
        this.setData({ groups: r.merged || [], loading: false });
        if (!r.merged || !r.merged.length) {
          this.setData({ error: '没找到「' + kw + '」在任一平台的演出，换个关键词试试' });
        }
      })
      .catch((e) => {
        this.setData({ loading: false, error: String(e.message || e) });
      });
  },
});
