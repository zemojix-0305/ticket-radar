// 「讨论区」：问题反馈白板 + 留言墙。
//
// 数据存在云数据库 radar_posts，字段：
//   _id, openid, avatar, nickname, content, tags, type, createdAt, likes, replies
//
// 留言墙视觉：每条帖子随机（按 _id 稳定）套一张手账便签做背景，
// 顶部盖一个小夹子/图钉——素材在 images/board/，工具脚本 tools/cut_board_assets.py。

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
  { key: 'puzzle', label: '拼图', bg: '/images/board2/collage16.png', dark: false },
  { key: 'twins', label: '双星', bg: '/images/board2/cartoon11.png', dark: false },
  { key: 'ice', label: '冰块', bg: '/images/board2/cartoon3.png', dark: false },
];
function stickerBg(key) {
  const s = STICKERS.find((x) => x.key === key);
  return s ? s.bg : '';
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
  },

  onPullDownRefresh() {
    this.loadPosts(true).then(() => wx.stopPullDownRefresh());
  },

  loadUser() {
    let u = wx.getStorageSync(USER_KEY);
    if (!u || typeof u !== 'object') u = {};
    this.setData({ user: u });
  },

  loadPosts(reset) {
    if (this.data.loading) return Promise.resolve();
    const self = this;
    const offset = reset ? 0 : this.data.offset;
    const kw = (this.data.kw || '').trim();
    // tab 筛选：0=全部，1/2/3 对应帖子上的 type 标签。云函数不过滤，前端筛。
    const typeMap = ['', '问题反馈', '建议留言', '求助求助'];
    const typeFilter = this.data.currentTab ? typeMap[this.data.currentTab] : '';

    self.setData({ loading: true, fail: '' });
    return wx.cloud
      .callFunction({
        name: 'radar',
        data: { action: 'listPosts', keyword: kw, offset, limit: PAGE_SIZE },
      })
      .then((res) => {
        const r = res.result || {};
        if (!r.ok) throw new Error(r.error || '未知错误');
        let list = (r.posts || []).map((p) => Object.assign({}, p, {
          rel: relTime(p.createdAt),
          noteIdx: noteIndex(p._id),
          clipIdx: clipIndex(p._id),
          // 用户选过素材就用选的，没选（历史帖）按 _id 稳定轮换便签
          bg: stickerBg(p.sticker) || '/images/board/note' + noteIndex(p._id) + '.png',
          onSticker: !!p.sticker,   // 贴纸卡类没有自带夹子，渲染时不再叠 clip
          // 微旋转 -1.2°~1.2°：让便签墙不至于太机械
          rot: ((stableHash(p._id + 'r') % 25) - 12) / 10,
        }));
        if (typeFilter) list = list.filter((p) => p.type === typeFilter);
        const posts = reset ? list : self.data.posts.concat(list);
        self.setData({
          posts,
          loading: false,
          offset: offset + (r.posts || []).length,
          hasMore: (r.posts || []).length >= PAGE_SIZE,
          searching: false,
        });
      })
      .catch((e) => {
        self.setData({ loading: false, fail: String(e.message || e), searching: false });
      });
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
      wx.showModal({
        title: '先设置头像和昵称',
        content: '去「我的」页点一下头像、填个昵称，再来发帖。这样讨论区才知道你是谁。',
        confirmText: '去设置',
        success: (r) => {
          if (r.confirm) wx.switchTab({ url: '/pages/mine/mine' });
        },
      });
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

  submitPost() {
    const content = (this.data.postContent || '').trim();
    if (!content) return wx.showToast({ title: '写点什么再发', icon: 'none' });
    if (content.length > 500) return wx.showToast({ title: '太长了，限制 500 字', icon: 'none' });

    const u = this.data.user;
    const tags = (this.data.postTags || '')
      .split(/[\s,，]+/)
      .map((s) => s.trim())
      .filter(Boolean)
      .slice(0, 3);
    const typeLabels = ['', '问题反馈', '建议留言', '求助求助'];

    wx.cloud
      .callFunction({
        name: 'radar',
        data: {
          action: 'createPost',
          content,
          tags,
          type: typeLabels[this.data.postType],
          sticker: this.data.postSticker,
          nickname: u.nickname || '匿名狐友',
          avatar: u.avatar || '',
        },
      })
      .then((res) => {
        const r = res.result || {};
        if (!r.ok) throw new Error(r.error || '发布失败');
        wx.showToast({ title: '已发布', icon: 'success' });
        this.setData({ showPost: false, postContent: '', postTags: '', postType: 0 });
        this.loadPosts(true);
      })
      .catch((e) => {
        wx.showModal({ title: '发布失败', content: String(e.message || e), showCancel: false });
      });
  },
});
