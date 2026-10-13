// 「讨论区」：大家互相交流场次信息。
//
// 数据存在云数据库 radar_posts，字段：
//   _id, openid, avatar, nickname, content, tags, createdAt, likes, replies
//
// 发帖前要求拿到微信头像昵称（chooseAvatar + nickname input），
// 这样讨论区就不会变成一堆「用户ABC123」在说话。

const USER_KEY = 'radar_user_profile';
const POSTS_DB = 'radar_posts';
const PAGE_SIZE = 20;

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

    self.setData({ loading: true, fail: '' });
    return wx.cloud
      .callFunction({
        name: 'radar',
        data: { action: 'listPosts', keyword: kw, offset, limit: PAGE_SIZE },
      })
      .then((res) => {
        const r = res.result || {};
        if (!r.ok) throw new Error(r.error || '未知错误');
        const list = (r.posts || []).map((p) => Object.assign({}, p, { rel: relTime(p.createdAt) }));
        const posts = reset ? list : self.data.posts.concat(list);
        self.setData({
          posts,
          loading: false,
          offset: offset + list.length,
          hasMore: list.length >= PAGE_SIZE,
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
    this.setData({ showPost: true, postContent: '', postTags: '' });
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
