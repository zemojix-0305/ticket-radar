/**
 * 社区帖子
 * ---------------------------------------------------------------------------
 * 第二十五轮：帖子从「手机本地」搬到云数据库。
 * 以前存在 wx.getStorageSync 里，结果是「你发的帖子只有你自己看得见」——
 * 一个叫「社区」的东西做成单机是没意义的。
 *
 * 现在的读法分两层：
 *   1. cachedPosts()  同步读本地缓存 → 进页面立刻有内容，不白屏；
 *   2. fetchPosts()   异步拉云端 → 拿到真数据后覆盖，同时写回缓存。
 *
 * 发帖/删帖一律以云端为准，本地只做缓存。云端失败会抛错，由页面提示，
 * 不静默假成功——「以为发出去了其实没发」比报错更难受。
 */

const api = require('./api');

const POSTS_KEY = 'radar_posts_cache';
const MAX_CACHE = 100;

/**
 * 帖子分类：**下标即 postType（0/1/2）**，讨论区、「我的」页共用这一份。
 * 以前两处各写一个数组，其中一处 0 号位是空字符串、第三项写成「求助求助」，
 * 于是「问题反馈」发出去没有标签、「求助留言」永远筛不出来。
 */
const TYPE_LABELS = ['聊票', '提问', '避坑'];

/** 把历史帖子里的脏 type 归一化（老版本存的是「问题反馈/建议留言/求助留言」） */
function normalizeType(t) {
  const s = String(t == null ? '' : t).trim();
  if (!s) return '';
  if (s === '求助求助' || s === '求助' || s === '求助留言') return '提问';
  if (s === '问题反馈') return '避坑';
  if (s === '建议留言') return '聊票';
  return s;
}

/** 本地缓存（同步、可能为空） */
function cachedPosts() {
  try {
    const list = wx.getStorageSync(POSTS_KEY) || [];
    return Array.isArray(list) ? list : [];
  } catch (e) {
    return [];
  }
}

function saveCache(list) {
  try {
    wx.setStorageSync(POSTS_KEY, (list || []).slice(0, MAX_CACHE));
  } catch (e) {
    /* 缓存写失败不影响主流程 */
  }
}

/**
 * 拉云端帖子。
 * opts: { keyword, type, offset, limit, mine }
 * 返回 Promise<Array>，type 已归一化。
 */
function fetchPosts(opts) {
  const o = opts || {};
  return api
    .listPosts({
      keyword: o.keyword || '',
      type: o.type || '',
      offset: o.offset || 0,
      limit: o.limit || 30,
      mine: !!o.mine,
    })
    .then((res) => {
      const r = (res && res.result) || {};
      if (!r.ok) throw new Error(r.error || '拿不到帖子');
      const list = (r.posts || []).map((p) =>
        Object.assign({}, p, { type: normalizeType(p.type) })
      );
      // 只有「默认列表」（无搜索 / 无筛选 / 第一页）才覆盖缓存，
      // 否则搜一次关键词就把缓存清成一条，下次进页面白屏。
      const isDefault = !o.keyword && !o.type && !o.mine && !o.offset;
      if (isDefault) saveCache(list);
      return list;
    });
}

/** 发帖。成功返回 { _id, post }，失败抛错。 */
function addPost(post) {
  const p = post || {};
  const payload = {
    content: p.content || '',
    tags: p.tags || [],
    type: normalizeType(p.type),
    sticker: p.sticker || '',
    nickname: p.nickname || '匿名狐友',
    avatar: p.avatar || '',
  };
  return api.createPost(payload).then((res) => {
    const r = (res && res.result) || {};
    if (!r.ok) throw new Error(r.error || '发送失败');
    // 本地缓存插一条，返回列表时立刻能看到（不必等下一次拉取）
    const mine = Object.assign({ _id: r._id, mine: true }, payload, {
      createdAt: new Date().toISOString(),
      likes: 0,
      replies: 0,
    });
    saveCache([mine].concat(cachedPosts()).slice(0, MAX_CACHE));
    return { _id: r._id, post: mine };
  });
}

/** 删自己的帖子。成功返回 true，失败抛错（云函数会说清是没权限还是没这条）。 */
function removePost(id) {
  return api.delPost(id).then((res) => {
    const r = (res && res.result) || {};
    if (!r.ok) throw new Error(r.error || '删除失败');
    saveCache(cachedPosts().filter((p) => p._id !== id));
    return true;
  });
}

/**
 * 一次性把旧版本存在手机本地的帖子搬到云端。
 * ---------------------------------------------------------------------------
 * 上一版社区是单机的，用户的帖子在 radar_posts_local 里。切成共享之后
 * 那些帖子不会自动出现（云端是空的），用户会以为「我发的都没了」。
 * 这里在启动时静默搬一次，搬完删掉旧 key，只跑一次。
 *
 * 串行上传：并发发帖会撞内容安全接口的频率限制，反而更慢更容易失败。
 * 单条失败不阻断后面的——不能因为一条违规内容卡住整批。
 */
function migrateLocalPosts() {
  const OLD_KEY = 'radar_posts_local';
  const FLAG = 'radar_posts_migrated_v1';
  try {
    if (wx.getStorageSync(FLAG)) return Promise.resolve(0);
  } catch (e) {
    return Promise.resolve(0);
  }

  let old = [];
  try {
    old = wx.getStorageSync(OLD_KEY) || [];
  } catch (e) {
    old = [];
  }
  if (!Array.isArray(old) || !old.length) {
    try {
      wx.setStorageSync(FLAG, 1);
    } catch (e) {
      /* ignore */
    }
    return Promise.resolve(0);
  }

  let done = 0;
  const step = (i) => {
    if (i >= old.length) {
      try {
        wx.setStorageSync(FLAG, 1);
        wx.removeStorageSync(OLD_KEY);
      } catch (e) {
        /* ignore */
      }
      return Promise.resolve(done);
    }
    const p = old[i] || {};
    if (!p.content) return step(i + 1);
    return addPost({
      content: p.content,
      tags: p.tags || [],
      type: p.type || '',
      sticker: p.sticker || '',
      nickname: p.nickname || '匿名狐友',
      avatar: p.avatar || '',
    })
      .then(() => {
        done += 1;
        return step(i + 1);
      })
      .catch(() => step(i + 1));
  };
  return step(0);
}

module.exports = {
  cachedPosts,
  fetchPosts,
  addPost,
  removePost,
  migrateLocalPosts,
  TYPE_LABELS,
  normalizeType,
  POSTS_KEY,
};
