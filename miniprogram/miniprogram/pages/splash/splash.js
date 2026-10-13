// 开屏页：2.4s 自动进入，点任意处跳过
Page({
  data: {
    jumped: false,
  },

  onLoad() {
    // 首次展示开屏，之后 24h 内不再打扰
    const last = wx.getStorageSync('splash_shown_at') || 0;
    if (Date.now() - last < 24 * 3600 * 1000) {
      this.jump();
      return;
    }
    wx.setStorageSync('splash_shown_at', Date.now());
    this._timer = setTimeout(() => this.jump(), 2400);
  },

  goHome() {
    if (this._timer) clearTimeout(this._timer);
    this.jump();
  },

  jump() {
    if (this.data.jumped) return;
    this.setData({ jumped: true });
    wx.switchTab({ url: '/pages/monitor/monitor' });
  },

  onUnload() {
    if (this._timer) clearTimeout(this._timer);
  },
});
