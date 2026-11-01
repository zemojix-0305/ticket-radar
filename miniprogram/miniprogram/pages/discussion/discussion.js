// 「讨论区」：问题反馈白板 + 留言墙。
//
// 数据存在云数据库 radar_posts，字段：
//   _id, openid, avatar, nickname, content, tags, type, createdAt, likes, replies
//
// 留言墙视觉：每条帖子随机（按 _id 稳定）套一张手账便签做背景，
// 顶部盖一个小夹子/图钉——素材在 images/board/，工具脚本 tools/cut_board_assets.py。

import Toast from '../../vant/toast/toast';
import Dialog from '../../vant/dialog/dialog';
// 第二十五轮：帖子改为云端共享。cachedPosts 只用于首屏秒出（弱网不白屏），
// 真数据一律以 fetchPosts 拉的为准。
import { cachedPosts, fetchPosts, addPost, removePost, TYPE_LABELS, normalizeType } from '../../utils/posts';

const USER_KEY = 'radar_user_profile';
const POSTS_DB = 'radar_posts';
const PAGE_SIZE = 20;

const NOTE_COUNT = 8;   // images/board/note1..8.png
const CLIP_COUNT = 13;  // images/board/clip1..13.png

// 发帖可选素材：只保留 8 张手账便签纸。
// 原来后面还跟了一串「贴纸卡」（苹果/星星/叶子/娃娃/格纹星/双星/冰块），
// 那些图是插画不是纸，铺到卡片上字压不住、比例也对不齐，2026-10-30 按用户要求砍掉。
// key 会存进帖子字段 sticker，渲染时按 key 找背景图；老帖子里的贴纸 key 找不到图，
// 会自动回落到按 _id 轮换的便签纸（见 loadPosts）。
const STICKERS = [
  { key: 'note1', label: '黄纸', bg: '/images/board/note1.png', dark: false },
  { key: 'note2', label: '粉纸', bg: '/images/board/note2.png', dark: false },
  { key: 'note3', label: '蓝纸', bg: '/images/board/note3.png', dark: false },
  { key: 'note4', label: '绿纸', bg: '/images/board/note4.png', dark: false },
  { key: 'note5', label: '橘纸', bg: '/images/board/note5.png', dark: false },
  { key: 'note6', label: '紫纸', bg: '/images/board/note6.png', dark: false },
  { key: 'note7', label: '白纸', bg: '/images/board/note7.png', dark: false },
  { key: 'note8', label: '米纸', bg: '/images/board/note8.png', dark: false },
];
function stickerBg(key) {
  const s = STICKERS.find((x) => x.key === key);
  return s ? s.bg : '';
}

// 留言类型（下标即 postType）统一从 utils/posts 取，见那边的注释

// 删帖权限不再靠本机 uid 判断了：那是随便能改的本地变量，而且换手机就失效。
// 现在由云函数用 getWXContext 拿真实 openid 比对，前端只消费 listPosts 回传的 mine。

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

/** 给一条帖子套上便签视觉：时间、纸型、夹子、微旋转。云端和缓存两条路共用。 */
function decoratePost(p) {
  return Object.assign({}, p, {
    type: normalizeType(p.type),
    rel: relTime(p.createdAt),
    noteIdx: noteIndex(p._id),
    clipIdx: clipIndex(p._id),
    // 用户选过纸就用选的；老帖子的贴纸 key 已经没有图了（贴纸卡已下线），
    // 自动回落到按 _id 稳定轮换的便签纸
    bg: stickerBg(p.sticker) || '/images/board/note' + noteIndex(p._id) + '.png',
    // 微旋转 -1.2°~1.2°：让便签墙不至于太机械
    rot: ((stableHash(p._id + 'r') % 25) - 12) / 10,
  });
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
    // 空状态文案（按「搜索/分类/我的留言墙」三种原因分别给，别一律说「还没有」）
    empty: { title: '还没有留言', sub: '第一条你来写。' },
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
    const kw = (this.data.kw || '').trim();
    // tab 筛选：0=全部，1/2/3 依次对应 TYPE_LABELS
    const typeFilter = this.data.currentTab ? TYPE_LABELS[this.data.currentTab - 1] || '' : '';
    const offset = reset ? 0 : (this.data.posts || []).length;

    self.setData({ loading: true, fail: '' });

    // 先把本地缓存铺出来：云端第一次拉要几百毫秒，弱网更久，
    // 不铺缓存就是一片空白，用户会以为社区坏了。
    if (reset) {
      const cached = cachedPosts();
      if (cached.length) {
        self.setData({
          posts: cached.map(decoratePost),
          loading: false,
          empty: self.emptyCopy(kw, typeFilter),
        });
      }
    }

    // 搜索、分类、「只看我的」都交给云端做：本地只有缓存（最多 100 条），
    // 拿它当数据源会漏掉别人的帖子——那正是上一版「社区只有自己」的原因。
    return fetchPosts({
      keyword: kw,
      type: typeFilter,
      offset: offset,
      limit: PAGE_SIZE,
      mine: self.data.mineWall,
    })
      .then((list) => {
        const rendered = list.map(decoratePost);
        const posts = reset ? rendered : self.data.posts.concat(rendered);
        self.setData({
          posts,
          loading: false,
          searching: false,
          hasMore: list.length >= PAGE_SIZE,
          empty: self.emptyCopy(kw, typeFilter),
        });
      })
      .catch((e) => {
        // 拉不到时保留缓存内容，只在顶部给一行提示，不要整页清空
        self.setData({
          loading: false,
          searching: false,
          fail: String((e && e.message) || e),
        });
      });
  },

  /** 空状态文案要分场景：以前不管什么原因都写「还没有反馈」，
   *  于是用户在「求助留言」分类下看到一句「还没有反馈」，以为留言全丢了 */
  emptyCopy(kw, typeFilter) {
    if (kw) return { title: '没搜到相关内容', sub: '换个关键词，或点搜索框右侧「清除」。' };
    if (typeFilter) return { title: '「' + typeFilter + '」还没有留言', sub: '切到「全部」看看，或者你来发第一条。' };
    if (this.data.mineWall) return { title: '你还没发过留言', sub: '点右下角 ✍ 写一条。' };
    return { title: '还没有留言', sub: '第一条你来写。' };
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
    // 权限由云函数判（它拿得到真实 openid，前端传什么都不可信）。
    // 这里只是提前给个提示，省一次注定失败的请求。
    if (p.mine === false) {
      return Toast({ message: '只能删自己的帖子', type: 'none' });
    }
    wx.showModal({
      title: '删除这条帖子',
      content: '删了就找不回来了，确定？',
      confirmText: '删除',
      confirmColor: '#C0655A',
      success: (res) => {
        if (!res.confirm) return;
        removePost(p._id)
          .then(() => {
            this.setData({ zoomIdx: -1 });
            Toast({ message: '已删除', type: 'success' });
            this.loadPosts(true);
          })
          .catch((e) => {
            Toast({ message: String((e && e.message) || '删除失败'), type: 'none' });
          });
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
    // _id / 作者 / 时间全由云端生成，前端不再自己编（编了也对不上真实记录）
    const post = {
      content,
      tags,
      // postType 0/1/2 直接对应 TYPE_LABELS，不会再有空 type
      type: TYPE_LABELS[this.data.postType] || TYPE_LABELS[0],
      sticker: this.data.postSticker,
      nickname: u.nickname || '匿名狐友',
      avatar: u.avatar || '',
    };
    addPost(post)
      .then(() => {
        Toast({ message: '已发布', type: 'success' });
        this.setData({ showPost: false, postContent: '', postTags: '', postType: 0 });
        this.loadPosts(true);
      })
      .catch((e) => {
        // 云函数会给出人话原因（内容不合规 / 超长 / 网络），原样展示
        Dialog.alert({ title: '发布失败', message: String((e && e.message) || e) });
      });
  },
});
