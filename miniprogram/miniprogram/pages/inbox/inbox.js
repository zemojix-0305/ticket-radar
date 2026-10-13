// 「消息」页：回答竞品 FAQ 里那两条最常见的抱怨——
//   「为什么别人收到通知而你收不到」「为什么收到却看不到票」。
//
// 这里不能列微信发出去的服务通知——微信不把订阅消息回传给小程序。
// 所以记的是**我们自己这边的证据**：每次真的去查了平台、查到了什么、
// 额度还剩几条。用户看这一页就能判断「是没票，还是我没在查」。
//
// 记录存在本地 storage：小程序端的检查日志不涉及他人数据，
// 没必要为此多写一份云端集合。

const ACT_KEY = 'radar_activity';
const CREDIT_KEY = 'wx_subscribe_credit';
const MAX = 40;

// 追加一条活动。写进 storage 失败（隐私模式、配额满）不该让主流程崩掉。
function logActivity(kind, name, detail) {
  try {
    const list = wx.getStorageSync(ACT_KEY) || [];
    list.unshift({ t: new Date().toISOString(), kind, name, detail });
    wx.setStorageSync(ACT_KEY, list.slice(0, MAX));
  } catch (e) {
    // 存不下就算了，这页只是个辅助视图
  }
}

function relTime(iso) {
  const t = Date.parse(String(iso || '').replace(' ', 'T'));
  if (!t || isNaN(t)) return '';
  const m = Math.floor((Date.now() - t) / 60000);
  if (m < 1) return '刚刚';
  if (m < 60) return m + ' 分钟前';
  const h = Math.floor(m / 60);
  if (h < 24) return h + ' 小时前';
  return Math.floor(h / 24) + ' 天前';
}

const KIND = {
  check: { label: '正常检查', cls: 'tag-none' },
  change: { label: '余票变化', cls: 'tag-ok' },
  fail: { label: '检查失败', cls: 'tag-bad' },
};

Page({
  data: {
    credit: 0,
    activity: [],
    states: [],
    healthy: 0,
    total: 0,
    lastCheck: '',
    lastCheckRel: '',
  },

  onShow() {
    this.setData({
      credit: wx.getStorageSync(CREDIT_KEY) || 0,
      activity: this.withRel(wx.getStorageSync(ACT_KEY) || []),
    });
    this.pullStates();
  },

  // storage 里存的是原始时间戳，展示时现算「x 分钟前」——
  // 否则历史记录会一直显示写下那一刻的时间，看着像坏了。
  withRel(list) {
    return (list || []).map((a) => {
      const k = KIND[a.kind] || KIND.check;
      return Object.assign({}, a, { rel: relTime(a.t), kindLabel: k.label, kindTag: k.cls });
    });
  },

  pullStates() {
    const self = this;
    wx.cloud
      .callFunction({ name: 'radar', data: { action: 'query' } })
      .then((res) => {
        const r = res.result || {};
        if (!r.ok) throw new Error(r.error || '未知错误');
        const states = r.states || [];
        const healthy = states.filter((s) => s.ok).length;

        // 只在状态真的变了才记一条 activity：
        // 每 5 分钟都往列表里塞「一切正常」，两天后这页就没法看了。
        const prev = wx.getStorageSync(ACT_KEY) || [];
        for (const s of states) {
          const key = 'st:' + s._id;
          const prevN = wx.getStorageSync(key);
          const nowN = typeof s.available === 'number' ? s.available : -1;
          if (prevN !== undefined && prevN !== nowN) {
            logActivity('change', s.name || s._id, '余票 ' + prevN + ' → ' + nowN);
          }
          wx.setStorageSync(key, nowN);
        }

        let lastCheck = '';
        for (const s of states) {
          if (!lastCheck || (s.checked_at || '') > lastCheck) lastCheck = s.checked_at || '';
        }

        self.setData({
          states,
          healthy,
          total: states.length,
          lastCheck,
          lastCheckRel: relTime(lastCheck),
          activity: this.withRel(wx.getStorageSync(ACT_KEY) || []),
        });
      })
      .catch(() => {
        /* 读不到就不更新，让用户看上一份记录，别把界面清空 */
      });
  },

  goMine() {
    wx.switchTab({ url: '/pages/mine/mine' });
  },
});
