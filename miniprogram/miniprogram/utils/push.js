/**
 * 推送额度管理（一次性订阅消息）
 * ---------------------------------------------------------------------------
 * 先把微信的规则说清楚，因为它决定了这里只能这么设计：
 *   · 一次性订阅 = 「用户点一次允许」换「一条推送」，这是平台硬规则，
 *     个人主体拿不到长期订阅（长期订阅只开放给政务/医疗/交通等类目）。
 *   · 但授权弹窗底部有「总是保持以上选择，不再询问」——
 *     用户勾上之后，之后每次调用 requestSubscribeMessage 都**不再弹窗**，
 *     直接静默返回 accept。
 *
 * 所以这里的策略是「不让用户管额度」：
 *   1. 首次授权时把「总是保持」的勾选框讲明白，引导用户勾上；
 *   2. 之后在「加监控成功」这类自然时机静默续一条，用户完全无感；
 *   3. 弹窗一天最多一次（没勾那个勾的人也不会被反复骚扰）；
 *   4. 额度只花在「无票 → 有票」这一个事件上，1 条能用很久；
 *   5. 额度见底也不影响用——打开小程序照样能看到回流状态，只是不推微信。
 *
 * 结论：额度这个概念对用户仍然存在（平台决定的），但「一直手动领」这件事
 * 从这里开始没有了。
 */
// 「余票提醒」模板（个人主体，一次性订阅）
const TMPL_ID = 'YnMbqU_UqlWb1eUOSMOp1RwxfGJozlrlXYtSaXFnsF4';

const CREDIT_KEY = 'wx_subscribe_credit'; // 本地记账的剩余条数
const ASKED_AT = 'wx_subscribe_asked_at'; // 上次弹过窗的时间，用于一天一次节流

function credit() {
  return Number(wx.getStorageSync(CREDIT_KEY) || 0);
}

function setCredit(n) {
  const v = Math.max(0, Number(n) || 0);
  wx.setStorageSync(CREDIT_KEY, v);
  return v;
}

function addCredit(n) {
  return setCredit(credit() + (Number(n) || 1));
}

/** 今天是否已经自动弹过一次（避免没勾「总是保持」的人被反复打扰） */
function askedToday() {
  const last = Number(wx.getStorageSync(ASKED_AT) || 0);
  if (!last) return false;
  const d1 = new Date(last).toDateString();
  const d2 = new Date().toDateString();
  return d1 === d2;
}

function markAsked() {
  wx.setStorageSync(ASKED_AT, Date.now());
}

/**
 * 续一条额度。
 * @param {object} opt
 *   silent  - 不给任何 UI 反馈（默认 true，用户无感）
 *   force   - 无视「一天一次」节流，用户主动点的时候用
 *   success - 领取成功后的回调（拿到最新条数）
 * 返回 Promise<boolean>：是否领到。
 */
function renew(opt) {
  const o = opt || {};
  return new Promise((resolve) => {
    if (!wx.requestSubscribeMessage) {
      resolve(false);
      return;
    }
    if (!o.force && askedToday()) {
      // 今天已经弹过了。如果用户勾了「总是保持」，这里其实还能静默领，
      // 但我们判断不出来，所以保守跳过，避免骚扰。
      resolve(false);
      return;
    }
    markAsked();
    wx.requestSubscribeMessage({
      tmplIds: [TMPL_ID],
      success: (res) => {
        const ok = res && res[TMPL_ID] === 'accept';
        if (ok) {
          const n = addCredit(1);
          if (!o.silent) {
            wx.showToast({ title: `已续 1 条（共 ${n} 条）`, icon: 'none' });
          }
          if (typeof o.success === 'function') o.success(n);
        } else if (!o.silent) {
          const why =
            res && res[TMPL_ID] === 'reject'
              ? '你拒绝了授权，所以不会收到推送'
              : `没开成（${(res && res[TMPL_ID]) || '未知'}）`;
          wx.showToast({ title: why, icon: 'none' });
        }
        resolve(ok);
      },
      fail: (err) => {
        if (!o.silent) {
          wx.showToast({ title: '微信没弹窗，稍后再点一次', icon: 'none' });
        }
        resolve(false);
      },
    });
  });
}

/**
 * 加监控成功后调用：静默续期。
 * 用户勾过「总是保持」的话，这一步完全无感；没勾的话会弹一次授权窗——
 * 但那是在「他刚点完加监控」的时刻，属于自然时机，不算打扰。
 */
function renewAfterAdd() {
  return renew({ silent: true });
}

module.exports = { TMPL_ID, CREDIT_KEY, credit, setCredit, addCredit, renew, renewAfterAdd };
