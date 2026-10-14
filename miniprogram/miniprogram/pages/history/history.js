// 「历史」页：两个 tab——历史曲线 + 推送记录。
import { request } from '../../utils/api';
//
// 历史曲线存在的理由：竞品的讨论区全是人在互相问「有回流吗」「什么价位好抢」
// 「什么时候二开」——那些问题本来该由产品回答。我们手里有每次抓取的记录，
// 就能算出「这场的脾气」。
//
// 推送记录存在的理由：微信不把订阅消息回传给小程序，所以我们自己记证据——
// 每次真的去查了平台、查到了什么、额度还剩几条。

const ACT_KEY = 'radar_activity';
const CREDIT_KEY = 'wx_subscribe_credit';
const MAX_ACT = 40;

function parseTs(t) {
  const v = Date.parse(String(t || '').replace(' ', 'T'));
  return isNaN(v) ? 0 : v;
}

function fmtSpan(ms) {
  const h = Math.floor(ms / 3600000);
  if (h < 1) return Math.max(1, Math.floor(ms / 60000)) + ' 分钟';
  if (h < 48) return h + ' 小时';
  return Math.floor(h / 24) + ' 天';
}

function analyze(rec) {
  const hist = (rec.history || []).filter((h) => h && h.t);
  const pts = hist.slice(-30);
  const nums = pts.map((h) => Number(h.n) || 0);
  const peak = Math.max(1, ...nums);

  // 柱子高度：留 3px 底线，太矮的点在 58px 高的图里根本看不见
  const bars = nums.map((n) => ({ h: Math.max(4, Math.round((n / peak) * 100)), n }));

  // 放票次数 = 余票从 0 变正数的次数（不是「有票的采样点数」，
  // 那会把持续在售的场次算成放票了很多次）
  let drops = 0;
  for (let i = 1; i < nums.length; i += 1) {
    if (nums[i - 1] === 0 && nums[i] > 0) drops += 1;
  }

  const spanMs = pts.length >= 2 ? parseTs(pts[pts.length - 1].t) - parseTs(pts[0].t) : 0;
  const everHad = nums.some((n) => n > 0);
  const last = nums.length ? nums[nums.length - 1] : 0;

  let verdict;
  let tone;
  if (pts.length < 3) {
    verdict = '刚开始盯，采样还太少，说不出规律。再让它跑几个小时。';
    tone = 'none';
  } else if (last > 0 && everHad) {
    verdict =
      '当前有票，而且盯到现在一直有票——不用抢，直接去官方渠道买就行。';
    tone = 'ok';
  } else if (last > 0) {
    verdict = '刚放票，正在有票。这类窗口通常很短，看到提醒就赶紧下手。';
    tone = 'ok';
  } else if (everHad) {
    verdict =
      '有过票，现在售罄。盯的就是这种回流——有票会突然出现，别删掉它。';
    tone = 'warn';
  } else {
    verdict =
      '一直是零。要么还没开售，要么已经抢光了。先去官方渠道确认一下档期，' +
      '别一直等一个不会来的通知。';
    tone = 'none';
  }

  return {
    bars,
    samples: pts.length,
    drops,
    peak,
    span: spanMs > 0 ? fmtSpan(spanMs) : '不到 1 分钟',
    verdict,
    tone,
    enough: pts.length >= 3,
  };
}

const LABEL = { maoyan: '猫眼', moretickets: '摩天轮', rail12306: '12306', amadeus: '机票' };

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

function logActivity(kind, name, detail) {
  try {
    const list = wx.getStorageSync(ACT_KEY) || [];
    list.unshift({ t: new Date().toISOString(), kind, name, detail });
    wx.setStorageSync(ACT_KEY, list.slice(0, MAX_ACT));
  } catch (e) {}
}

const KIND = {
  check: { label: '正常检查', cls: 'tag-none' },
  change: { label: '余票变化', cls: 'tag-ok' },
  fail: { label: '检查失败', cls: 'tag-bad' },
};

Page({
  data: {
    tab: 0,
    rows: [],
    loading: true,
    fail: '',
    totalSamples: 0,
    totalTasks: 0,
    enoughCount: 0,
    credit: 0,
    activity: [],
    states: [],
    healthy: 0,
    total: 0,
    lastCheck: '',
    lastCheckRel: '',
  },

  onLoad() {
    this.refresh();
    this.loadActivity();
  },

  onShow() {
    this.loadActivity();
  },

  onPullDownRefresh() {
    if (this.data.tab === 0) {
      this.refresh().then(() => wx.stopPullDownRefresh());
    } else {
      this.loadActivity().then(() => wx.stopPullDownRefresh());
    }
  },

  switchTab(e) {
    const idx = Number(e.currentTarget.dataset.idx);
    this.setData({ tab: idx });
    if (idx === 1) this.loadActivity();
  },

  loadActivity() {
    this.setData({
      credit: wx.getStorageSync(CREDIT_KEY) || 0,
      activity: this.withRel(wx.getStorageSync(ACT_KEY) || []),
    });
    return this.pullStates();
  },

  withRel(list) {
    return (list || []).map((a) => {
      const k = KIND[a.kind] || KIND.check;
      return Object.assign({}, a, { rel: relTime(a.t), kindLabel: k.label, kindTag: k.cls });
    });
  },

  pullStates() {
    const self = this;
      request({ action: 'query' })
      .then((res) => {
        const r = res.result || {};
        if (!r.ok) throw new Error(r.error || '未知错误');
        const states = r.states || [];
        const healthy = states.filter((s) => s.ok).length;

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
      .catch(() => {});
  },

  goMine() {
    wx.switchTab({ url: '/pages/mine/mine' });
  },

  refresh() {
    const self = this;
    self.setData({ loading: true, fail: '' });
      request({ action: 'query' })
      .then((res) => {
        const r = res.result || {};
        if (!r.ok) throw new Error(r.error || '未知错误');
        const rows = (r.states || []).map((s) => {
          const a = analyze(s);
          return Object.assign({}, s, a, {
            platform: LABEL[s.platform] || s.platform,
          });
        });
        rows.sort((x, y) => y.samples - x.samples);
        self.setData({
          rows,
          totalTasks: rows.length,
          totalSamples: rows.reduce((n, r) => n + r.samples, 0),
          enoughCount: rows.filter((r) => r.enough).length,
          loading: false,
        });
      })
      .catch((e) => {
        self.setData({ loading: false, fail: String(e.message || e) });
      });
  },
});
