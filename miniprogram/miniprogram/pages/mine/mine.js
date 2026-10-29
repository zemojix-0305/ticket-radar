// 「我的」页：推送开关 + 微信绑定 + 我们不做什么。
//
// 为什么页面上显示的是 code 而不是 openid：换 openid 需要 AppSecret，
// 而 AppSecret 绝不能打进小程序包里（任何人反编译都能拿到）。
// 所以这里只负责把 code 显示出来，由用户在电脑上跑 `radar wxbind <code>` 去换。
// 这样既不需要开通云开发，也不泄露密钥。
const USER_KEY = 'radar_user_profile';

// vant 函数式反馈（纸感主题，见 app.wxss 的 van-* 覆盖）
import Toast from '../../vant/toast/toast';
import Dialog from '../../vant/dialog/dialog';
import { request } from '../../utils/api';
import { isOn, renew as pushRenew, off as pushOff } from '../../utils/push';
import { loadTasks } from '../../utils/tasks';
import { loadPosts, removePost } from '../../utils/posts';

function makeUid() {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  let s = '';
  for (let i = 0; i < 6; i++) s += chars.charAt(Math.floor(Math.random() * chars.length));
  return s;
}

Page({
  data: {
    code: '',
    err: '',
    // 推送开关：只有「开 / 关」两种状态，不再显示还剩几条
    pushOn: false,
    authMsg: '',
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
    this.setData({ pushOn: isOn() });
    this.loadUser();
    this.loadWatched();
    this.setData({
      fwdToken: wx.getStorageSync('radar_fwd_token') || '',
      dmCookie: wx.getStorageSync('radar_dm_cookie') || '',
      myToken: wx.getStorageSync('radar_my_token') || '',
    });
  },

  // --- 平台凭证 -------------------------------------------------------------
  // 纷玩岛票档级库存、猫眼分档价格、大麦风控，都只有登录通道能给。
  // 凭证只存本机 storage，随 find/detail/poll 请求带上，服务端不落库。
  toggleCred() {
    this.setData({ credOpen: !this.data.credOpen });
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
    // 本机留言墙全量在本地 storage；发帖时带 uid 的才算本机发的（历史帖一律算）
    const list = (loadPosts() || []).map((p) => ({
      _id: p._id,
      content: p.content || '',
      type: p.type || '',
      time: this.fmtTime(p.createdAt),
    }));
    this.setData({ myPosts: list });
  },

  deleteMyPost(e) {
    const id = e.currentTarget.dataset.id || '';
    if (!id) return;
    Dialog.confirm({ title: '删除这条留言', message: '删了就找不回来了，确定？' })
      .then(() => {
        removePost(id);
        this.loadMyPosts();
        Toast({ message: '已删除', type: 'success' });
      })
      .catch(() => {});
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

  // 切换推送：开 → 关 直接关；关 → 开 走一次订阅授权。
  // 这是用户唯一需要操作的推送动作，没有第二个入口。
  togglePush() {
    if (this.data.pushOn) {
      pushOff();
      this.setData({ pushOn: false, authMsg: '' });
      Toast({ type: 'text', message: '已暂停推送' });
      return;
    }
    pushRenew({ silent: false, force: true }).then((ok) => {
      this.setData({
        pushOn: isOn(),
        authMsg: ok ? '' : '没开成。若之前选过拒绝，去微信「设置 → 隐私 → 授权管理」里重新允许。',
      });
      if (ok) Toast({ type: 'success', message: '推送已恢复' });
    });
  },
});
  },
});
