const TASKS_KEY = 'radar_tasks_local';

/** 从本地 storage 读监控任务列表 */
function loadTasks() {
  try {
    return wx.getStorageSync(TASKS_KEY) || [];
  } catch (e) {
    return [];
  }
}

/** 写监控任务列表到本地 storage */
function saveTasks(tasks) {
  try {
    wx.setStorageSync(TASKS_KEY, tasks);
  } catch (e) {
    console.warn('[ticket-radar] saveTasks failed:', e);
  }
}

/** 按 _id 删除一条监控任务 */
function removeTask(id) {
  try {
    const list = wx.getStorageSync(TASKS_KEY) || [];
    wx.setStorageSync(TASKS_KEY, list.filter((t) => t._id !== id));
  } catch (e) {
    console.warn('[ticket-radar] removeTask failed:', e);
  }
}

module.exports = { loadTasks, saveTasks, removeTask, TASKS_KEY };
