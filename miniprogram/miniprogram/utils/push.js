/**
 * 推送开关（微信一次性订阅消息）
 * ---------------------------------------------------------------------------
 * 用户只需要知道两件事：现在是「开」还是「关」，点一下能切换。
 *
 * 「还剩几条额度」这种说法已经全部去掉了——它是微信平台的实现细节，
 * 用户看不懂也没必要懂，看见「0 条」只会以为自己坏了。
 *
 * 微信的规则没变（一次性订阅 = 点一次允许换一条推送），所以实际做法是：
 *   1. 用户点「恢复推送」时请求一次授权，授权成功就记为「开」；
 *      这一次弹窗里勾上「总是保持以上选择」，后面就再也不会弹了；
 *   2. 之后用户每次点击小程序（看卡片、切页、刷新）都静默补一条，见 topUp()；
 *   3. 加监控时也顺手补一条（renewAfterAdd），因为那是最强的意愿时刻；
 *   4. 暂停时不弹窗、不续期，加监控也不打扰。
 *
 * 一句话：把「续」藏进用户本来就会做的动作里，界面上永远只有开和关。
 */
const TMPL_ID = 'YnMbqU_UqlWb1eUOSMOp1RwxfGJozlrlXYtSaXFnsF4';

// 一次 requestSubscribeMessage 最多能带 5 个模板，每个模板各 +1 次下发机会。
// 所以「5 个模板 × 一天最多补 4 次 = 20 条/天」，这是个人主体零操作能到的天花板。
// 注意：**标题相同的模板会被微信过滤，只保留一个**，所以这里的 5 个必须标题互不相同。
// 新增/替换模板时，云函数 index.js 的 TMPL_IDS + TMPL_MAP 要一起改，字段映射在那边。
const TMPL_IDS = [
  TMPL_ID, // 余票提醒
  '9w9j3Po1BnAGkoJQj6Xfql-wcRaRbyuXccxr7plsjdI', // 演出开票提醒
  'PB9i3a7YZGOh-kvWKZ4Ey_tB5unlgHuI6U1ng8eCeOY', // 回流票提醒
  'V0Cau9DqnX6xpflWxF50X7mdYZKd99TdeKoaELzM55M', // 最新演出信息提醒
  'dLZJDaUZi-1u1nSlQ2EZqYqWNlu3QjtbAcD8XUqRFnw', // 出行提醒（机票 / 火车）
];

const ON_KEY = 'radar_push_on';
const PAUSED_KEY = 'radar_push_paused'; // 用户自己按过「暂停推送」
const ASKED_AT = 'radar_push_asked_at';
const TOPUP_AT = 'radar_push_topup_at';
const TOPUP_DAY = 'radar_push_topup_day';
const REJECT_AT = 'radar_push_reject_at';

// 自动补次数：同一人 30 分钟内只补一次，一天最多 4 次。
// 微信允许「点一次就 +1」，但短时间内高频率调用会被判刷量、模板会被回收，
// 所以这里必须节流——用户完全无感，但不会把我们自己玩死。
// 勾过「总是保持以上选择」的人，这一步完全静默；没勾的人会看到弹窗，
// 所以频率再压低一点，别把人烦跑。
const TOPUP_GAP = 30 * 60 * 1000;
const TOPUP_MAX = 4;
// 用户点过「取消」= 不想收。别追着问，一周内不再补。
const REJECT_SILENCE = 7 * 24 * 60 * 60 * 1000;

/** 推送现在开着吗 */
function isOn() {
  return !!wx.getStorageSync(ON_KEY);
}

/**
 * 开关一变就通知服务端。
 * ---------------------------------------------------------------------------
 * 服务端是根据 pushOn 决定发不发的。不同步的话，用户点了「暂停推送」，
 * 服务端还在照旧往他手机上推——这比不发还难受。
 * 用 require 写在函数体里（而不是文件顶部）是为了避开 tasks <-> push 的
 * 循环依赖：真正调用时两个模块都已经加载完了。
 */
function notifyServer() {
  try {
    const t = require('./tasks');
    if (t && t.syncPush) t.syncPush();
  } catch (e) {
    /* 服务端连不上不影响本地开关 */
  }
}

function setOn(v) {
  if (v) wx.removeStorageSync(PAUSED_KEY);
  wx.setStorageSync(ON_KEY, !!v);
  notifyServer();
  return !!v;
}

/** 用户主动暂停：记一笔，之后加监控也不偷偷把推送打开 */
function off() {
  wx.setStorageSync(PAUSED_KEY, true);
  return setOn(false);
}

/** 今天是否已经自动弹过一次（避免没勾「总是保持」的人被反复打扰） */
function askedToday() {
  const last = Number(wx.getStorageSync(ASKED_AT) || 0);
  if (!last) return false;
  return new Date(last).toDateString() === new Date().toDateString();
}

function markAsked() {
  wx.setStorageSync(ASKED_AT, Date.now());
}

/**
 * 请求一次订阅授权。
 * @param {object} opt
 *   silent - 不给任何 UI 反馈（默认 true）
 *   force  - 无视「一天一次」节流（用户主动点的时候用）
 * 返回 Promise<boolean>：拿没拿到授权（拿到即视为推送已开）。
 */
function renew(opt) {
  const o = opt || {};
  return new Promise((resolve) => {
    if (!wx.requestSubscribeMessage) {
      resolve(false);
      return;
    }
    if (!o.force && askedToday()) {
      // 今天已经弹过。勾了「总是保持」的人其实还能静默领，但我们判断不出来，
      // 所以保守跳过，避免骚扰——不影响推送开着这件事本身。
      resolve(isOn());
      return;
    }
    markAsked();
    // 也记一笔自动补的时间：刚手动领过，不需要再补一次
    wx.setStorageSync(TOPUP_AT, Date.now());
    wx.requestSubscribeMessage({
      tmplIds: TMPL_IDS,
      success: (res) => {
        const accepted = TMPL_IDS.filter((id) => res && res[id] === 'accept');
        const ok = accepted.length > 0;
        if (ok) {
          wx.removeStorageSync(REJECT_AT);
          setOn(true);
          // 云端记账：一次授权 = 云端多一次下发机会。
          // 不记这一笔的话云函数那边 quota 恒为 0，推送永远发不出去，
          // 而小程序上还理直气壮显示「推送已开启」——最难查的一种错。
          try {
            const g = require('./api').grantSub(accepted.length);
            if (g && g.catch) g.catch(() => {});
          } catch (e) {
            /* 记账失败不影响本地开关 */
          }
        } else if (!o.silent) {
          const rejected = TMPL_IDS.some((id) => res && res[id] === 'reject');
          const why = rejected ? '你选了拒绝，所以不会收到推送' : '没开成，稍后再点一次';
          wx.showToast({ title: why, icon: 'none' });
        }
        if (TMPL_IDS.every((id) => res && res[id] === 'reject')) {
          // 明确拒绝：记下来，自动补次数先停一周，别反复弹
          wx.setStorageSync(REJECT_AT, Date.now());
        }
        resolve(ok);
      },
      fail: () => {
        if (!o.silent) {
          wx.showToast({ title: '微信没弹窗，稍后再点一次', icon: 'none' });
        }
        resolve(false);
      },
    });
  });
}

/**
 * 自动补一次下发机会（用户完全无感）。
 * ---------------------------------------------------------------------------
 * 微信的硬规则是「点一次授权 = 能收一条」，没法像服务号那样无限发。
 * 但我们可以在用户每次真正点击界面的时候，顺手补一条：
 *   - 用户勾过「总是保持以上选择」→ 完全不弹窗，静默 +1；
 *   - 没勾过 → 会弹一次，这也是他唯一一次需要做选择。
 * 补足后，用户看到的就是「一直能收到」，页面里也不出现任何「额度」字样。
 *
 * 只能在 tap 回调链里调用（微信要求用户手势），且必须节流，理由见 TOPUP_GAP。
 */
function topUp() {
  if (!isOn()) return Promise.resolve(false);
  const now = Date.now();
  const rej = Number(wx.getStorageSync(REJECT_AT) || 0);
  if (rej && now - rej < REJECT_SILENCE) return Promise.resolve(false);
  const today = new Date().toDateString();
  const last = Number(wx.getStorageSync(TOPUP_AT) || 0);
  let rec = wx.getStorageSync(TOPUP_DAY) || {};
  if (!rec || rec.d !== today) rec = { d: today, n: 0 };
  if (now - last < TOPUP_GAP || rec.n >= TOPUP_MAX) return Promise.resolve(true);
  wx.setStorageSync(TOPUP_AT, now);
  wx.setStorageSync(TOPUP_DAY, { d: today, n: rec.n + 1 });
  return renew({ silent: true, force: true });
}

/**
 * 加监控成功后调用：续一次订阅额度。
 * ---------------------------------------------------------------------------
 * 这里必须 force（每次都真的请求），不能沿用「一天一次」的节流：
 * 微信一次性订阅是**授权一次只能收一条**，攒额度全靠用户点击时那次请求。
 * 节流成一天一次 = 一天最多攒一条，真回流时早就没额度了。
 *
 * 用户勾过「总是保持以上选择」的话，这个调用完全不弹窗、静默加一条；
 * 没勾过才会弹一次窗，属于必要的打扰。
 *
 * 只能在用户点击的调用栈里调（wx.requestSubscribeMessage 硬性要求 TAP 手势），
 * 放 onShow 里一定失败。
 */
function renewAfterAdd() {
  if (wx.getStorageSync(PAUSED_KEY)) return Promise.resolve(false);
  return renew({ silent: true, force: true });
}

module.exports = { TMPL_ID, TMPL_IDS, isOn, setOn, off, renew, renewAfterAdd, topUp };
