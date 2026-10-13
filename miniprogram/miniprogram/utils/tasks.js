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

module.exports = { loadTasks, saveTasks, TASKS_KEY };
