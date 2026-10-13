// 开屏页逻辑
Page({
  data: {
    loaded: false
  },

  onLoad() {
    // 模拟加载动画
    setTimeout(() => {
      this.setData({ loaded: true });
    }, 800);
    
    // 延迟跳转
    setTimeout(() => {
      wx.switchTab({
        url: '/pages/monitor/monitor'
      });
    }, 2200);
  },

  onUnload() {
    // 防止返回
  }
});
