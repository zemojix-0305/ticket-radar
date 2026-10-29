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
 *   2. 之后每次加监控静默再续一条（勾了「总是保持以上选择」的话完全不弹窗）；
 *   3. 暂停时不弹窗、不续期，加监控也不打扰。
 */
const TMPL_ID = 'YnMbqU_UqlWb1eUOSMOp1RwxfGJozlrlXYtSaXFnsF4';

const ON_KEY = 'radar_push_on';
const PAUSED_KEY = 'radar_push_paused'; // 用户自己按过「暂停推送」
const ASKED_AT = 'radar_push_asked_at';

/** 推送现在开着吗 */
function isOn() {
  return !!wx.getStorageSync(ON_KEY);
}

function setOn(v) {
  if (v) wx.removeStorageSync(PAUSED_KEY);
  wx.setStorageSync(ON_KEY, !!v);
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
    wx.requestSubscribeMessage({
      tmplIds: [TMPL_ID],
      success: (res) => {
        const ok = res && res[TMPL_ID] === 'accept';
        if (ok) {
          setOn(true);
        } else if (!o.silent) {
          const why =
            res && res[TMPL_ID] === 'reject' ? '你选了拒绝，所以不会收到推送' : '没开成，稍后再点一次';
          wx.showToast({ title: why, icon: 'none' });
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
 * 加监控成功后调用：静默续一条（第一次加监控时就是在这里拿到授权）。
 * 用户自己按过「暂停推送」的，就不打扰——他要的是别推，不是又被弹一次窗。
 */
function renewAfterAdd() {
  if (!isOn() && wx.getStorageSync(PAUSED_KEY)) return Promise.resolve(false);
  return renew({ silent: true });
}

module.exports = { TMPL_ID, isOn, setOn, off, renew, renewAfterAdd };
