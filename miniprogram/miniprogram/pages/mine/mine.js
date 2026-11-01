// 「我的」页：推送开关 + 微信绑定 + 我们不做什么。
//
// openid 现在是全自动的：云函数每次被调用，微信都在上下文里塞好 OPENID，
// 不需要 wx.login 换 code，也不需要 AppSecret（那玩意儿绝不能打进小程序包，
// 任何人反编译都能拿到）。下面那个手填 code 的入口只是旧版本的兜底，正常用不到。
const USER_KEY = 'radar_user_profile';

// vant 函数式反馈（纸感主题，见 app.wxss 的 van-* 覆盖）
import Toast from '../../vant/toast/toast';
import Dialog from '../../vant/dialog/dialog';
import { request, pushTest, pushStatus, extGet, extSet, extClear } from '../../utils/api';
import { isOn, renew as pushRenew, off as pushOff, topUp as pushTopUp } from '../../utils/push';
import { loadTasks } from '../../utils/tasks';
import { TIPS } from '../../utils/notice';

const NOTICE_KEY = 'home_notice_open';
import { fetchPosts, removePost, normalizeType } from '../../utils/posts';

function makeUid() {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  let s = '';
  for (let i = 0; i < 6; i++) s += chars.charAt(Math.floor(Math.random() * chars.length));
  return s;
}

Page({
  // 随便点一下页面就补一次推送机会，用户看不到「额度」这件事
  onAnyTap() {
    pushTopUp();
  },
  data: {
    code: '',
    err: '',
    // 推送开关：只有「开 / 关」两种状态，不再显示还剩几条
    pushOn: false,
    authMsg: '',
    srvMsg: '',
    // 外部提醒：企业微信机器人 / Server酱 / ntfy。不占微信额度、不限条数，
    // 是「有变化就提醒」真正的底座（微信那条一天只有 4 条）。
    extOpen: false,
    extInput: '',
    extKind: '',
    extLabel: '',
    extMasked: '',
    extBusy: false,
    // 温馨提示：和首页同一份文案（utils/notice.js），这里主要给「机票走邮箱」做解释
    tips: TIPS,
    tipsOpen: true,
    bindOpen: false,
    user: { uid: '', nickname: '', avatar: '' },
    editingName: false,
    watchedCount: 0,
    // 我的留言：本机发过的讨论区帖子，可在这里删除
    myPostsOpen: false,
    myPosts: [],
    // 平台凭证（纷玩岛 token / 大麦 Cookie / 猫眼 token），存本地、随请求带上
    credOpen: false,
    fwdToken: '',
    fwdTokenPending: '',  // 待确认的 token
    dmCookie: '',
    myToken: '',
  },

  onLoad() {
    this.refreshCode();
    this.setData({
      pushOn: isOn(),
      // 跟首页共用同一个开关状态：首页收起来了，这里也收起来，别两边打架
      tipsOpen: wx.getStorageSync(NOTICE_KEY) === false ? false : true,
    });
    this.loadUser();
    this.loadWatched();
    this.setData({
      fwdToken: wx.getStorageSync('radar_fwd_token') || '',
      dmCookie: wx.getStorageSync('radar_dm_cookie') || '',
      myToken: wx.getStorageSync('radar_my_token') || '',
    });
  },

  // --- 平台凭证 -------------------------------------------------------------  // 纷玩岛票档级库存、猫眼分档价格、大麦风控，都只有登录通道能给。
  // 凭证只存本机 storage，随 find/detail/poll 请求带上，服务端不落库。
  toggleCred() {
    this.setData({ credOpen: !this.data.credOpen });
  },

  /** 温馨提示展开/收起。状态与首页共用，一处收起两处都收 */
  toggleTips() {
    const open = !this.data.tipsOpen;
    wx.setStorageSync(NOTICE_KEY, open);
    this.setData({ tipsOpen: open });
  },

  /** 重看首次进入的三句话弹窗：清掉标记再跳首页。弹窗只做了一份，不在这页重复实现 */
  replayIntro() {
    wx.removeStorageSync('radar_intro_v1');
    wx.switchTab({ url: '/pages/monitor/monitor' });
  },
  onFwdInput(e) { this.setData({ fwdToken: e.detail.value }); },
  onFwdTokenPendingInput(e) { this.setData({ fwdTokenPending: e.detail.value }); },

  /** 打开纷玩岛小程序 */
  openFenwandao() {
    wx.navigateToMiniProgram({
      appId: 'wx5a8f481d967649eb',
      path: 'pages/index/index',
      success: () => {
        Toast('已打开纷玩岛，登录后按步骤获取 token');
      },
      fail: () => {
        // 跳转失败，提示用户手动打开
        Dialog.alert({
          title: '打开纷玩岛',
          message: '请在微信里搜索「纷玩岛」小程序并打开，确保已登录账号。',
        });
      },
    });
  },

  /** 从纷玩岛分享链接中提取 token */
  onDmInput(e) { this.setData({ dmCookie: e.detail.value }); },
  onMyTokenInput(e) { this.setData({ myToken: e.detail.value }); },
  saveCred() {
    wx.setStorageSync('radar_fwd_token', String(this.data.fwdToken || '').trim());
    wx.setStorageSync('radar_dm_cookie', String(this.data.dmCookie || '').trim());
    wx.setStorageSync('radar_my_token', String(this.data.myToken || '').trim());
    Toast({ type: 'success', message: '凭证已保存' });
  },

  /** 测试纷玩岛 token 是否有效 */
  testFwdToken() {
    const token = this.data.fwdToken.trim();
    if (!token) { Toast('请先填写 token'); return; }
    Toast({ type: 'loading', message: '测试中…' });
    request({ action: 'find', platform: 'fenwandao', keyword: '周杰伦', limit: 1, fwdToken: token })
      .then((res) => {
        const r = res.result || {};
        if (r.ok && r.rows && r.rows.length > 0) {
          Toast({ type: 'success', message: '✓ Token 有效！可搜索纷玩岛了' });
        } else {
          Toast('Token 可能已过期，请重新获取');
        }
      })
      .catch((e) => {
        Toast('Token 无效：' + String(e.message || e).slice(0, 40));
      });
  },

  /** 清除纷玩岛 token */
  clearFwdToken() {
    wx.removeStorageSync('radar_fwd_token');
    this.setData({ fwdToken: '' });
    Toast('已清除');
  },

  /** 保存待确认的 token */
  saveFwdTokenPending() {
    const token = (this.data.fwdTokenPending || '').trim();
    if (!token) { Toast('token 不能为空'); return; }
    wx.setStorageSync('radar_fwd_token', token);
    this.setData({ fwdToken: token, fwdTokenPending: '' });
    Toast({ type: 'success', message: 'token 已保存' });
  },

  /** 清除待确认的 token */
  clearFwdTokenPending() {
    this.setData({ fwdTokenPending: '' });
  },

  /** 打开大麦网页版 */
  openDamai() {
    wx.navigateToMiniProgram({
      appId: 'wxa75d61ff9e0c37e7', // 大麦小程序 appId
      path: '/pages/home/index',
      success: () => {
        Toast('已打开大麦，登录后按提示获取 Cookie');
      },
      fail: () => {
        // 跳转失败，打开 H5 版本
        wx.openWebviewGroup?.({});
        Dialog.alert({
          title: '打开大麦',
          message: '请在微信里搜索「大麦」小程序并打开，登录后：\n\n' +
            '1. 电脑浏览器登录 damai.cn\n' +
            '2. F12 → Network → 刷新页面\n' +
            '3. 点任意请求 → 复制 Request Headers 里的 Cookie\n' +
            '4. 粘贴到上方输入框',
        });
      },
    });
  },

  /** 打开猫眼小程序 */
  openMaoyan() {
    wx.navigateToMiniProgram({
      appId: 'wxe378d1f269e7b888', // 猫眼小程序 appId
      path: '/pages/index/index',
      success: () => {
        Toast('已打开猫眼，登录后按提示获取 token');
      },
      fail: () => {
        Dialog.alert({
          title: '打开猫眼',
          message: '请在微信里搜索「猫眼演出」小程序并打开，登录后：\n\n' +
            '1. 电脑微信开发者工具打开猫眼小程序项目\n' +
            '2. Network 标签 → F5 刷新\n' +
            '3. 找到 wx.maoyan.com 的请求\n' +
            '4. 复制 URL 里的 token= 参数值',
        });
      },
    });
  },

  // --- 我的留言管理 ---------------------------------------------------------
  toggleMyPosts() {
    const open = !this.data.myPostsOpen;
    if (open) this.loadMyPosts();
    this.setData({ myPostsOpen: open });
  },

  loadMyPosts() {
    // 只拉自己发的。云端按 openid 过滤，比本机 uid 判定可靠得多：
    // 换手机、清缓存都不会把「我的帖子」弄丢。
    fetchPosts({ mine: true, limit: 50 })
      .then((list) => {
        this.setData({
          myPosts: list.map((p) => ({
            _id: p._id,
            content: p.content || '',
            type: normalizeType(p.type),
            time: this.fmtTime(p.createdAt),
          })),
        });
      })
      .catch((e) => {
        this.setData({ myPosts: [] });
        Toast({ message: String((e && e.message) || '拉取失败'), type: 'none' });
      });
  },

  deleteMyPost(e) {
    const id = e.currentTarget.dataset.id || '';
    if (!id) return;
    // 用原生 wx.showModal：这页此前 Dialog.confirm 弹不出来（组件没挂载），
    // 点了等于没点。原生弹窗永远在最顶层，不会再出现「删除按钮点不动」。
    wx.showModal({
      title: '删除这条帖子',
      content: '删了就找不回来了，确定？',
      confirmText: '删除',
      confirmColor: '#C0655A',
      success: (r) => {
        if (!r.confirm) return;
        removePost(id)
          .then(() => {
            this.loadMyPosts();
            Toast({ message: '已删除', type: 'success' });
          })
          .catch((e) => {
            Toast({ message: String((e && e.message) || '删除失败'), type: 'none' });
          });
      },
    });
  },

  /** 以留言墙的方式看自己的留言：带标志切到讨论区，那边 onShow 接管 */
  goMyWall() {
    wx.setStorageSync('radar_mine_wall', 1);
    wx.switchTab({ url: '/pages/discussion/discussion' });
  },

  fmtTime(iso) {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    const now = new Date();
    const diff = (now - d) / 1000;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
    const pad = (n) => (n < 10 ? '0' + n : n);
    return (d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  },

  loadUser() {
    let u = wx.getStorageSync(USER_KEY);
    if (!u || typeof u !== 'object') u = {};
    if (!u.uid) u.uid = '用户' + makeUid();
    this.setData({ user: u });
    wx.setStorageSync(USER_KEY, u);
  },

  onChooseAvatar(e) {
    // 用 wx.chooseMedia 替代 open-type="chooseAvatar"（模拟器不支持后者）
    wx.chooseMedia({
      count: 1,
      mediaType: ['image'],
      sourceType: ['album', 'camera'],
      success: (res) => {
        const url = res.tempFiles[0].tempFilePath;
        const u = Object.assign({}, this.data.user, { avatar: url });
        this.setData({ user: u });
        wx.setStorageSync('radar_user_profile', u);
      },
      fail: (err) => {
        // 用户取消或权限不足，静默处理
        if (!err.errMsg || err.errMsg.indexOf('cancel') < 0) {
          console.warn('[mine] chooseMedia failed:', err);
        }
      },
    });
  },

  // 昵称走独立弹层：type="nickname" 的 input 必须在用户主动点击后
  // 才弹微信键盘，内联在页面里会被键盘顶飞，体验很差。
  onNicknameDraft(e) {
    this.setData({ 'user.nickname': e.detail.value });
  },

  saveNickname() {
    const name = (this.data.user.nickname || '').trim();
    if (!name) {
      this.setData({ editingName: false });
      Toast('昵称不能为空');
      return;
    }
    const u = Object.assign({}, this.data.user, { nickname: name });
    this.setData({ user: u, editingName: false });
    wx.setStorageSync(USER_KEY, u);
    Toast({ type: 'success', message: '已保存' });
  },

  toggleEditName() {
    this.setData({ editingName: !this.data.editingName });
  },

  copyUid() {
    const uid = this.data.user.uid || '';
    if (!uid) return;
    wx.setClipboardData({
      data: uid,
      success: () => Toast({ type: 'success', message: '已复制' }),
    });
  },

  // 支持作者：复制 GitHub 仓库链接（小程序不能直接跳外部浏览器）
  goStar() {
    wx.setClipboardData({
      data: 'https://github.com/zemojix-0305/ticket-radar',
      success: () => Toast('链接已复制，去点个 Star 吧'),
    });
  },

  onShow() {
    // 别的页面可能刚把推送开关切过（首页那条），回来对齐一下
    this.setData({ pushOn: isOn() });
    this.loadWatched();
    this.loadExt();
    if (isOn()) this.loadSrvStatus();
  },

  // --- 正在盯：入口按钮跳独立页 -------------------------------------------
  // 只统计个数 + 跳转，列表逻辑在 pages/watched/watched
  loadWatched() {
    const tasks = loadTasks();
    this.setData({ watchedCount: tasks.length });
  },

  goWatched() {
    wx.navigateTo({ url: '/pages/watched/watched' });
  },

  toggleBind() {
    const open = !this.data.bindOpen;
    this.setData({ bindOpen: open });
    // 展开时才去取 code：平时不需要，也避免白白发一次登录请求
    if (open && !this.data.code) this.refreshCode();
  },

  onPullDownRefresh() {
    this.setData({ pushOn: isOn() });
    this.loadWatched();
    if (this.data.myPostsOpen) this.loadMyPosts();
    this.refreshCode(() => wx.stopPullDownRefresh());
  },

  // 每次点都拿一个新的 code：它只能用一次、5 分钟过期
  refreshCode(done) {
    wx.login({
      success: (res) => {
        if (!res.code) {
          this.setData({ err: 'wx.login 没返回 code，检查 AppID 是否填对' });
          if (done) done();
          return;
        }
        this.setData({ code: res.code, err: '' });
        if (done) done();
      },
      fail: (err) => {
        this.setData({ err: `wx.login 失败：${err.errMsg || '未知错误'}` });
        if (done) done();
      },
    });
  },

  copyCode() {
    if (!this.data.code) return;
    wx.setClipboardData({
      data: this.data.code,
      success: () => Toast({ type: 'success', message: '已复制' }),
    });
  },

  // 切换推送：开 → 关 直接关；关 → 开 走一次订阅授权 + 立刻发一条验证。
  // 为什么开完要发一条：微信的一次性订阅是「授权一次 = 能收一条」，用户点完
  // 允许后如果什么都不发，这条授权就白白躺着，而他其实没法确认到底通不通。
  // 用掉这一条换一句「收到了」——反而最有用。
  togglePush() {
    if (this.data.pushOn) {
      pushOff();
      this.setData({ pushOn: false, authMsg: '', srvMsg: '' });
      Toast({ type: 'text', message: '已暂停推送' });
      return;
    }
    pushRenew({ silent: false, force: true }).then((ok) => {
      this.setData({ pushOn: isOn() });
      if (!ok) {
        this.setData({
          authMsg: '没开成。若之前选过拒绝，去微信「设置 → 隐私 → 授权管理」里重新允许。',
        });
        return;
      }
      this.setData({ authMsg: '正在往你微信发一条…' });
      pushTest('推送已开启')
        .then((res) => {
          const r = (res && res.result) || {};
          if (r.ok) {
            this.setData({ authMsg: '', srvMsg: '' });
            Toast({ type: 'success', message: '已发到微信，去「服务通知」看' });
          } else {
            const why = r.error || '发不出去，稍后再试';
            this.setData({ authMsg: '', srvMsg: why });
          }
        })
        .catch((e) =>
          this.setData({ authMsg: '', srvMsg: String((e && e.message) || '云端连不上') })
        );
    });
  },

  // ---- 外部提醒通道（企业微信机器人 / Server酱 / ntfy）------------------
  // 微信一次性订阅是「授权一次只能发一条」，一天最多攒 4 条；个人主体又拿不到
  // 长期订阅。所以「有变化就提醒」只能靠外面这三个。它们不占微信额度、
  // 不限条数，绑一个地址就生效——这是整个推送方案真正的底座。
  toggleExt() {
    const open = !this.data.extOpen;
    this.setData({ extOpen: open });
    if (open) this.loadExt();
  },

  loadExt() {
    extGet()
      .then((res) => {
        const r = (res && res.result) || {};
        if (!r.ok) return;
        // 首页要按这个判断「机票是不是还在走微信额度」，本地留一份免得每次多打一次云函数
        wx.setStorageSync('radar_ext_kind', r.kind || '');
        const patch = {
          extKind: r.kind || '',
          extLabel: r.label || '',
          extMasked: r.masked || '',
        };
        // 没绑任何通道时默认展开一次：收起态只有一个标题，用户根本不知道里面能填邮箱。
        // 只自动展开一次，之后用户自己收起了就不再强开。
        // mine_ext_force_open 是首页「机票还在走微信额度」那条提示带过来的一次性指令。
        if (!r.kind) {
          const forced = wx.getStorageSync('mine_ext_force_open');
          if (forced) wx.removeStorageSync('mine_ext_force_open');
          if (forced || !wx.getStorageSync('radar_ext_autoshow')) {
            wx.setStorageSync('radar_ext_autoshow', 1);
            patch.extOpen = true;
          }
        } else {
          // 已绑定时把残留标记清掉，不然下次解绑会莫名自动弹开
          wx.removeStorageSync('mine_ext_force_open');
        }
        this.setData(patch);
      })
      .catch(() => {});
  },

  onExtInput(e) {
    this.setData({ extInput: (e && e.detail && e.detail.value) || '' });
  },

  saveExt() {
    const v = String(this.data.extInput || '').trim();
    if (!v) {
      Toast({ type: 'text', message: '先粘一个地址进来' });
      return;
    }
    this.setData({ extBusy: true });
    extSet(v)
      .then((res) => {
        const r = (res && res.result) || {};
        this.setData({ extBusy: false });
        if (!r.ok) {
          Toast({ type: 'text', message: r.error || '没绑上' });
          return;
        }
        this.setData({
          extKind: r.kind || '',
          extLabel: r.label || '',
          extMasked: r.masked || '',
          extInput: '',
        });
        // 云函数存完会立刻发一条测试。收到了就是真通了，不用等下一次降价验证。
        if (r.tested) {
          Toast({ type: 'success', message: '已绑 ' + (r.label || '') + '，测试消息已发出' });
        } else {
          Toast({ type: 'text', message: '已保存，但测试没发出去：' + (r.testError || '查地址') });
        }
        this.loadSrvStatus();
      })
      .catch((e) => {
        this.setData({ extBusy: false });
        Toast({ type: 'text', message: String((e && e.message) || '云端连不上') });
      });
  },

  // 用原生 showModal：这页没挂 van-dialog，Dialog.confirm 会静默失败（踩过）
  clearExt() {
    wx.showModal({
      title: '解绑外部提醒？',
      content: '解绑后只剩微信订阅消息：靠打开小程序攒额度，一天最多 20 条。演出够用，机票会很紧。',
      confirmText: '解绑',
      confirmColor: '#c0392b',
      success: (m) => {
        if (!m.confirm) return;
        extClear()
          .then(() => {
            wx.setStorageSync('radar_ext_kind', '');
            this.setData({ extKind: '', extLabel: '', extMasked: '' });
            Toast({ type: 'text', message: '已解绑' });
            this.loadSrvStatus();
          })
          .catch(() => Toast({ type: 'text', message: '云端连不上' }));
      },
    });
  },

  // 云端是不是真的在盯、真的能发。不查这一下，用户看到「推送已开启」
  // 却永远收不到消息，还以为是自己手机的问题。
  loadSrvStatus() {
    pushStatus()
      .then((res) => {
        const r = (res && res.result) || {};
        if (!r.ok) return this.setData({ srvMsg: '云端没响应' });
        let msg = '';
        if (!r.pushOn) msg = '云端没在盯：上面的推送开关没开，开了才会一直查';
        else if (!r.tasks) msg = '还没有监控任务，加一个才会开始盯';
        // 绑了外部通道就不存在「额度用完」这回事了，别再吓用户
        else if (!r.quota && !r.extKind)
          msg = '微信额度用完了：点一次「恢复推送」会一次性补 5 条（5 个模板各 1 条）。';
        this.setData({ srvMsg: msg });
      })
      .catch((e) => this.setData({ srvMsg: String((e && e.message) || '云端连不上') }));
  },
});
