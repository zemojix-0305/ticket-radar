// 「我的」页：订阅额度（主角）+ 微信绑定（折叠）+ 我们不做什么。
//
// 为什么页面上显示的是 code 而不是 openid：换 openid 需要 AppSecret，
// 而 AppSecret 绝不能打进小程序包里（任何人反编译都能拿到）。
// 所以这里只负责把 code 显示出来，由用户在电脑上跑 `radar wxbind <code>` 去换。
// 这样既不需要开通云开发，也不泄露密钥。
const TMPL_ID = 'YnMbqU_UqlWb1eUOSMOp1RwxfGJozlrlXYtSaXFnsF4';

// 本地攒的额度条数。真正在微信后台存着多少条我们查不到（要 AppSecret），
// 所以这里只是给用户一个「我大概领过几次」的记账本，够用了。
const CREDIT_KEY = 'wx_subscribe_credit';
const USER_KEY = 'radar_user_profile';

// vant 函数式反馈（纸感主题，见 app.wxss 的 van-* 覆盖）
import Toast from '../../vant/toast/toast';
import Dialog from '../../vant/dialog/dialog';
import { request } from '../../utils/api';
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
    authorized: false,
    authMsg: '',
    credit: 0,
    batching: false,
    batchGot: 0,
    batchTotal: 0,
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
    dmCookie: '',
    myToken: '',
  },

  onLoad() {
    this.refreshCode();
    this.setData({ credit: wx.getStorageSync(CREDIT_KEY) || 0 });
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
  onDmInput(e) { this.setData({ dmCookie: e.detail.value }); },
  onMyTokenInput(e) { this.setData({ myToken: e.detail.value }); },
  saveCred() {
    wx.setStorageSync('radar_fwd_token', String(this.data.fwdToken || '').trim());
    wx.setStorageSync('radar_dm_cookie', String(this.data.dmCookie || '').trim());
    wx.setStorageSync('radar_my_token', String(this.data.myToken || '').trim());
    Toast({ type: 'success', message: '凭证已保存' });
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
    const url = e.detail.avatarUrl;
    const u = Object.assign({}, this.data.user, { avatar: url });
    this.setData({ user: u });
    wx.setStorageSync(USER_KEY, u);
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
    // 从别的 tab 领完额度切回来，数字要跟着变；
    // 监控页刚刷完一轮的话，「正在盯」的状态也跟着变
    this.setData({ credit: wx.getStorageSync(CREDIT_KEY) || 0 });
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
    // 下拉＝把这一页能变的东西都重读一遍：额度、正在盯的条数、
    // 展开状态下的我的留言；顺带换一个新的 wx.login code。
    this.setData({ credit: wx.getStorageSync(CREDIT_KEY) || 0 });
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

  // 把本次领取同步到云端记账（radar_subs 集合）。
  // 云函数发推送时从这里扣额度；本地 storage 只是显示缓存。
  // 同步失败不打断用户：下次领额度时会按云端实际值覆盖回来。
  syncQuotaToCloud(n) {
      request({ action: 'grantSub', n: n })
      .then((r) => {
        const res = r.result || {};
        if (res.ok && typeof res.quota === 'number') {
          wx.setStorageSync(CREDIT_KEY, res.quota);
          this.setData({ credit: res.quota });
        }
      })
      .catch(() => {});
  },

  // 一次性订阅：点一次「允许」= 换来一条推送额度。
  // 用完再发会报 43101，所以额度见底时回来再点一次就行。
  requestSubscribe() {
    if (!wx.requestSubscribeMessage) {
      this.setData({ authMsg: '微信版本过低，不支持订阅消息' });
      return;
    }
    wx.requestSubscribeMessage({
      tmplIds: [TMPL_ID],
      success: (res) => {
        const state = res[TMPL_ID];
        if (state === 'accept') {
          const n = (wx.getStorageSync(CREDIT_KEY) || 0) + 1;
          wx.setStorageSync(CREDIT_KEY, n);
          this.setData({
            authorized: true,
            credit: n,
            authMsg: `已领到第 ${n} 条额度`,
          });
          this.syncQuotaToCloud(1);
        } else if (state === 'reject') {
          this.setData({ authMsg: '你拒绝了这次授权，不会收到推送' });
        } else if (state === 'ban') {
          this.setData({ authMsg: '该模板已被后台禁用' });
        } else if (state === 'filter') {
          this.setData({ authMsg: '该模板被后台过滤了' });
        } else {
          this.setData({ authMsg: `授权结果：${state}` });
        }
      },
      fail: (err) => {
        this.setData({ authMsg: `拉起授权失败：${err.errMsg || '未知错误'}` });
      },
    });
  },

  // 连领 5 条：省得一次一次点。
  //
  // 为什么要排队而不是并发：wx.requestSubscribeMessage 同一时间只能弹一个窗，
  // 并发调用直接 fail。
  //
  // 为什么失败不立刻放弃：微信对连续调用有节流，间隔太短会被拒，
  // 但那只是「等一下再来」而不是「不能领」。所以失败后退避重试，
  // 真试不出来再让人手动点——批量只是省事，不该比手动更不可靠。
  requestSubscribeBatch() {
    if (this.data.batching) return;
    this.setData({ batching: true, authMsg: '正在领取…', batchGot: 0, batchTotal: 5 });

    let got = 0;
    let retry = 0;
    const total = 5;

    const done = (why) => {
      this.setData({
        batching: false,
        batchGot: 0,
        batchTotal: 0,
        authMsg:
          got > 0
            ? `${why}已领 ${got} 条，累计 ${this.data.credit} 条。剩下的手动点「再领一条额度」也一样。`
            : `${why}没领到，请点「允许通知」手动领一次。`,
      });
      if (got > 0) Toast({ type: 'success', message: `已领 ${got} 条` });
    };

    const next = () => {
      if (got >= total) {
        done('');
        return;
      }
      // 失败重试用递增间隔（1s → 2s → 3s），三次还不行就交给人
      const wait = retry === 0 ? 1000 : retry * 1000;

      if (retry >= 3) {
        done('微信挡住了连领（模拟器里尤其容易这样）。');
        return;
      }

      wx.requestSubscribeMessage({
        tmplIds: [TMPL_ID],
        success: (res) => {
          if (res[TMPL_ID] === 'accept') {
            got += 1;
            retry = 0;
            const n = (wx.getStorageSync(CREDIT_KEY) || 0) + 1;
            wx.setStorageSync(CREDIT_KEY, n);
            this.setData({ authorized: true, credit: n, batchGot: got });
          } else {
            // 用户点了拒绝：重试也没用，直接收工
            retry = 3;
          }
          setTimeout(next, 1000);
        },
        fail: () => {
          retry += 1;
          setTimeout(next, wait);
        },
      });
    };

    next();
  },

  resetCredit() {
    wx.removeStorageSync(CREDIT_KEY);
    this.setData({ credit: 0, authMsg: '计数已清零（只是本地记账，不影响微信后台的真实额度）' });
  },
});
