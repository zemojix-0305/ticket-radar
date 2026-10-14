// 「讨论区」：问题反馈白板 + 留言墙。
//
// 数据存在云数据库 radar_posts，字段：
//   _id, openid, avatar, nickname, content, tags, type, createdAt, likes, replies
//
// 留言墙视觉：每条帖子随机（按 _id 稳定）套一张手账便签做背景，
// 顶部盖一个小夹子/图钉——素材在 images/board/，工具脚本 tools/cut_board_assets.py。

import Toast from '../../vant/toast/toast';
import Dialog from '../../vant/dialog/dialog';
// 免费架构（第二十六轮）：帖子存本地 storage，不再走服务端
// （云库已 no-op，listPosts/createPost 拿到的永远是空表）
import { loadPosts as readLocalPosts, addPost, removePost, savePosts } from '../../utils/posts';

const USER_KEY = 'radar_user_profile';
const POSTS_DB = 'radar_posts';
const PAGE_SIZE = 20;

const NOTE_COUNT = 8;   // images/board/note1..8.png
const CLIP_COUNT = 13;  // images/board/clip1..13.png

// 发帖可选素材：8 张便签纸 + 6 张浅色贴纸卡（深色贴纸写字看不清，不收）。
// key 会存进帖子字段 sticker，渲染时按 key 找背景图。
const STICKERS = [
  { key: 'note1', label: '黄纸', bg: '/images/board/note1.png', dark: false },
  { key: 'note2', label: '粉纸', bg: '/images/board/note2.png', dark: false },
  { key: 'note3', label: '蓝纸', bg: '/images/board/note3.png', dark: false },
  { key: 'note4', label: '绿纸', bg: '/images/board/note4.png', dark: false },
  { key: 'note5', label: '橘纸', bg: '/images/board/note5.png', dark: false },
  { key: 'note6', label: '紫纸', bg: '/images/board/note6.png', dark: false },
  { key: 'note7', label: '白纸', bg: '/images/board/note7.png', dark: false },
  { key: 'note8', label: '米纸', bg: '/images/board/note8.png', dark: false },
  { key: 'apple', label: '苹果', bg: '/images/board2/cartoon4.png', dark: false },
  { key: 'starPurple', label: '星星', bg: '/images/board2/cartoon8.png', dark: false },
  { key: 'leaf', label: '叶子', bg: '/images/board2/collage12.png', dark: true },
  { key: 'dolls', label: '娃娃', bg: '/images/board2/collage14.png', dark: true },
  { key: 'puzzle', label: '格纹星', bg: '/images/board2/origami7.png', dark: false },
  { key: 'twins', label: '双星', bg: '/images/board2/cartoon11.png', dark: false },
  { key: 'ice', label: '冰块', bg: '/images/board2/cartoon3.png', dark: false },
];
function stickerBg(key) {
  const s = STICKERS.find((x) => x.key === key);
  return s ? s.bg : '';
}

// 设备 uid：发帖时标记发布者（删除权判定用「我的/管理员」）
function deviceUid() {
  try {
    let u = wx.getStorageSync('radar_uid');
    if (!u) {
      u = 'u' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
      wx.setStorageSync('radar_uid', u);
    }
    return u;
  } catch (e) {
    return 'anon';
  }
}

// 把任意字符串 hash 成 0..n-1。同一个 _id 永远拿到同一个素材编号。
function stableHash(str) {
  let h = 0;
  const s = String(str || '');
  for (let i = 0; i < s.length; i += 1) {
    h = (h * 31 + s.charCodeAt(i)) >>> 0;
  }
  return h;
}
function noteIndex(id) {
  return (stableHash(id) % NOTE_COUNT) + 1;
}
function clipIndex(id) {
  return (stableHash(id + 'c') % CLIP_COUNT) + 1;
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

Page({
  data: {
    posts: [],
    loading: false,
    fail: '',
    kw: '',
    searching: false,
    showPost: false,
    postContent: '',
    postTags: '',
    postType: 0,
    postSticker: 'note1',
    previewBg: stickerBg('note1'),
    stickers: STICKERS,
    zoomIdx: -1,
    mineWall: false,
    user: { uid: '', nickname: '', avatar: '' },
    hasMore: true,
    offset: 0,
    currentTab: 0,
  },

  onLoad() {
    this.loadUser();
    this.loadPosts(true);
  },

  onShow() {
    this.loadUser();
    // 「我的 → 我的留言 → 以留言墙的方式查看」跳进来：只看自己发的留言
    try {
      if (wx.getStorageSync('radar_mine_wall')) {
        wx.removeStorageSync('radar_mine_wall');
        this.setData({ mineWall: true });
        wx.setNavigationBarTitle({ title: '我的留言墙' });
        this.loadPosts(true);
      }
    } catch (e) { /* storage 异常不挡正常浏览 */ }
  },

  /** 退出我的留言墙，回全部留言 */
  exitMyWall() {
    this.setData({ mineWall: false });
    wx.setNavigationBarTitle({ title: '讨论区' });
    this.loadPosts(true);
  },

  onPullDownRefresh() {
    // 顺便重读用户信息：昵称可能在「我的」页改过
    this.loadUser();
    this.loadPosts(true).then(() => wx.stopPullDownRefresh());
  },

  loadUser() {
    let u = wx.getStorageSync(USER_KEY);
    if (!u || typeof u !== 'object') u = {};
    this.setData({ user: u });
  },

  loadPosts(reset) {
    const self = this;
    const kw = (this.data.kw || '').trim().toLowerCase();
    // tab 筛选：0=全部，1/2/3 对应帖子上的 type 标签
    const typeMap = ['', '问题反馈', '建议留言', '求助求助'];
    const typeFilter = this.data.currentTab ? typeMap[this.data.currentTab] : '';

    self.setData({ loading: true, fail: '' });
    // 本地帖子按时间倒序读出，关键词 + 类型在前端过滤
    Promise.resolve()
      .then(() => {
        let list = (readLocalPosts() || []).slice();
        if (kw) {
          list = list.filter((p) =>
            ((p.content || '') + ' ' + (p.tags || []).join(' ') + ' ' + (p.nickname || ''))
              .toLowerCase()
              .indexOf(kw) >= 0
          );
        }
        if (typeFilter) list = list.filter((p) => p.type === typeFilter);
        // 我的留言墙模式：只看本机发的（按设备 uid 判定）
        if (self.data.mineWall) list = list.filter((p) => p.uid && p.uid === deviceUid());
        list = list.map((p) => Object.assign({}, p, {
          rel: relTime(p.createdAt),
          noteIdx: noteIndex(p._id),
          clipIdx: clipIndex(p._id),
          // 用户选过素材就用选的，没选（历史帖）按 _id 稳定轮换便签
          bg: stickerBg(p.sticker) || '/images/board/note' + noteIndex(p._id) + '.png',
          onSticker: !!p.sticker,   // 贴纸卡类没有自带夹子，渲染时不再叠 clip
          // 微旋转 -1.2°~1.2°：让便签墙不至于太机械
          rot: ((stableHash(p._id + 'r') % 25) - 12) / 10,
        }));
        const posts = reset ? list : self.data.posts.concat(list);
        self.setData({
          posts,
          loading: false,
          hasMore: false,   // 本地全量读，没有分页
          searching: false,
        });
      })
      .catch((e) => {
        self.setData({ loading: false, fail: String(e.message || e), searching: false });
      });
  },

  // 分类切换：van-tabs 用 e.detail.index；回退 dataset.tab（旧写法兼容）
  onTab(e) {
    const idx = e.detail && typeof e.detail.index === 'number'
      ? e.detail.index
      : Number(e.currentTarget.dataset.tab);
    if (isNaN(idx) || idx === this.data.currentTab) return;
    this.setData({ currentTab: idx });
    this.loadPosts(true);
  },

  onInput(e) {
    this.setData({ kw: e.detail.value });
  },

  doSearch() {
    this.setData({ searching: true });
    this.loadPosts(true);
  },

  clearSearch() {
    this.setData({ kw: '', searching: false });
    this.loadPosts(true);
  },

  loadMore() {
    if (!this.data.hasMore || this.data.loading) return;
    this.loadPosts(false);
  },

  openPost() {
    const u = this.data.user;
    if (!u.nickname || !u.avatar) {
      Dialog.confirm({
        title: '先设置头像和昵称',
        message: '去「我的」页点一下头像、填个昵称，再来发帖。这样讨论区才知道你是谁。',
        confirmButtonText: '去设置',
      }).then(() => {
        wx.switchTab({ url: '/pages/mine/mine' });
      }).catch(() => {});
      return;
    }
    this.setData({ showPost: true, postContent: '', postTags: '', postType: 0, postSticker: 'note1', previewBg: stickerBg('note1') });
  },

  closePost() {
    this.setData({ showPost: false });
  },

  onPostInput(e) {
    this.setData({ postContent: e.detail.value });
  },

  onTagInput(e) {
    this.setData({ postTags: e.detail.value });
  },

  onPickSticker(e) {
    const key = e.currentTarget.dataset.key;
    this.setData({ postSticker: key, previewBg: stickerBg(key) });
  },

  // 发帖弹层里的类型三选一（问题反馈/建议留言/求助求助）
  onType(e) {
    const t = Number(e.currentTarget.dataset.type);
    this.setData({ postType: isNaN(t) ? 0 : t });
  },

  // 点便签 → 放大查看
  onNoteTap(e) {
    const idx = Number(e.currentTarget.dataset.index);
    if (idx >= 0 && idx < this.data.posts.length) {
      this.setData({ zoomIdx: idx });
    }
  },

  closeZoom() {
    this.setData({ zoomIdx: -1 });
  },

  /** 放大层里删除这条留言（发布者可删；本机留言墙=本机即管理端）。
   * 确认框用原生 wx.showModal：vant Dialog 曾有被弹层盖住弹不出来的嫌疑，
   * 原生弹窗永远在最顶层，杜绝「点了没反应」。 */
  deleteZoomPost() {
    const idx = this.data.zoomIdx;
    const p = this.data.posts[idx];
    if (!p) return;
    const mine = !p.uid || p.uid === deviceUid();
    wx.showModal({
      title: '删除这条留言',
      content: mine ? '删了就找不回来了，确定？' : '确定以管理员身份删除这条留言？',
      confirmText: '删除',
      confirmColor: '#C0655A',
      success: (res) => {
        if (!res.confirm) return;
        const removed = removePost(p._id);
        this.setData({ zoomIdx: -1 });
        this.loadPosts(true);
        Toast({ message: removed ? '已删除' : '这条不在本机留言墙里', type: removed ? 'success' : 'text' });
      },
    });
  },

  // 空处理器：给需要阻断冒泡的节点挂 catchtap 用（空字符串 catchtap 不可靠）
  noop() {},

  submitPost() {
    const content = (this.data.postContent || '').trim();
    if (!content) return Toast({ message: '写点什么再发', type: 'none' });
    if (content.length > 500) return Toast({ message: '太长了，限制 500 字', type: 'none' });

    const u = this.data.user;
    const tags = (this.data.postTags || '')
      .split(/[\s,，]+/)
      .map((s) => s.trim())
      .filter(Boolean)
      .slice(0, 3);
    const typeLabels = ['', '问题反馈', '建议留言', '求助求助'];

    const post = {
      _id: 'p' + Date.now() + Math.floor(Math.random() * 1000),
      // 设备 uid：标记发布者。「我的」页管理列表和删除权判定都用它
      uid: deviceUid(),
      content,
      tags,
      type: typeLabels[this.data.postType] || '',
      sticker: this.data.postSticker,
      nickname: u.nickname || '匿名狐友',
      avatar: u.avatar || '',
      createdAt: new Date().toISOString(),
      likes: 0,
      replies: 0,
    };
    try {
      addPost(post);
      Toast({ message: '已发布', type: 'success' });
      this.setData({ showPost: false, postContent: '', postTags: '', postType: 0 });
      this.loadPosts(true);
    } catch (e) {
      Dialog.alert({ title: '发布失败', message: String((e && e.message) || e) });
    }
  },
});
