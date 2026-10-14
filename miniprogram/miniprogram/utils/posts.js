const POSTS_KEY = 'radar_posts_local';
const MAX_POSTS = 100;

/** 从本地 storage 读讨论帖子列表 */
function loadPosts() {
  try {
    return wx.getStorageSync(POSTS_KEY) || [];
  } catch (e) {
    return [];
  }
}

/** 写讨论帖子列表到本地 storage（保留最新 MAX_POSTS 条） */
function savePosts(posts) {
  try {
    wx.setStorageSync(POSTS_KEY, posts.slice(0, MAX_POSTS));
  } catch (e) {
    console.warn('[ticket-radar] savePosts failed:', e);
  }
}

/** 添加一条新帖子到列表头 */
function addPost(post) {
  const posts = loadPosts();
  posts.unshift(post);
  savePosts(posts);
  return post;
}

/** 按 _id 删除一条帖子。返回是否删到了。 */
function removePost(id) {
  const posts = loadPosts();
  const next = posts.filter((p) => p._id !== id);
  const removed = next.length !== posts.length;
  if (removed) savePosts(next);
  return removed;
}

module.exports = { loadPosts, savePosts, addPost, removePost, POSTS_KEY };
