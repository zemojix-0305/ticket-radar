const api = require('./api');
const push = require('./push');

const TASKS_KEY = 'radar_tasks_local';

/** 从本地 storage 读监控任务列表 */
function loadTasks() {
  try {
    return wx.getStorageSync(TASKS_KEY) || [];
  } catch (e) {
    return [];
  }
}

/**
 * 写监控任务列表，并顺手同步给服务端。
 * ---------------------------------------------------------------------------
 * 同步这一步是「关掉小程序也能收到微信」的前提：服务端拿不到任务列表就不
 * 知道要查什么。放在这里而不是各个页面，是因为加监控的地方有四五处
 * （详情页、搜索页、比价页、机票面板、监控页删除），漏掉一处就会出现
 * 「这个票在手机上加的，关掉小程序就不提醒了」这种极难复现的 bug。
 *
 * 同步是异步且静默失败的：服务端没开会连不上，但这不该让用户加不了监控。
 */
function saveTasks(tasks) {
  try {
    wx.setStorageSync(TASKS_KEY, tasks);
  } catch (e) {
    console.warn('[ticket-radar] saveTasks failed:', e);
    return;
  }
  api.pushSync(tasks, push.isOn());
}

/** 按 _id 删除一条监控任务 */
function removeTask(id) {
  try {
    const list = wx.getStorageSync(TASKS_KEY) || [];
    saveTasks(list.filter((t) => t._id !== id));
  } catch (e) {
    console.warn('[ticket-radar] removeTask failed:', e);
  }
}

/** 只同步推送开关（不动任务列表）。push.js 里切换开关时调它。 */
function syncPush() {
  api.pushSync(loadTasks(), push.isOn());
}

module.exports = { loadTasks, saveTasks, removeTask, syncPush, TASKS_KEY };
