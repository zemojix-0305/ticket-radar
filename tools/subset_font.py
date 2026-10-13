# -*- coding: utf-8 -*-
"""把站酷庆科黄油体子集化成小程序可打包的小字体。

子集字符 = 项目所有 wxml/wxss/js 里出现过的 CJK 字符 + ASCII + 常用标点 + 数字。
这样界面上的固定文案全部有字形；用户随手输入的生僻字会退回系统字体，可接受。

用法: python tools/subset_font.py [源ttf] [输出woff2]
输出放 miniprogram/miniprogram/fonts/，用 @font-face + 本地路径加载。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "miniprogram", "miniprogram")
FONT_IN = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "huangyou_full.ttf")
FONT_OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(SRC, "fonts", "butter.woff2")

# 1) 扫项目文件收集字符
chars = set()
for dirpath, _, files in os.walk(SRC):
    for fn in files:
        if not fn.endswith((".wxml", ".wxss", ".js", ".json")):
            continue
        try:
            text = open(os.path.join(dirpath, fn), encoding="utf-8").read()
        except Exception:
            continue
        chars.update(text)

# 2) 兜底补充：ASCII 全量 + 常用中文标点 + 数字圈 + 一些装饰符
extra = (
    "，。、；：？！…—·「」『』（）《》〈〉“”‘’【】％＋－×÷＝～￥"
    "〇一二三四五六七八九十百千万亿零壹贰叁肆伍陆柒捌玖拾"
    "°℃★☆♥❤♡✦✧＋"
    # 常用界面词兜底：搜索/表单/弹窗/留言墙交互会用到的字
    "到达吧盯站交换选择样式便签纸夹子贴纸写留言看放大缩小关闭确定取消"
    "删除分享复制链接粘贴扫码跳转展开收起点赞评论发布提交保存成功失败"
    "重试加载稍等网络异常暂无记录输入想说的话涂鸦拼贴星星雨滴蝴蝶兔子"
    "叶子千纸鹤拼图爱心支持请喝咖啡星标前往仓库问题反馈建议求助热门正在"
    "搜索车站日期出发地目的地候补候车乘客座位有无余快速全部筛选清空"
)
chars.update(extra)
chars.update(chr(c) for c in range(0x20, 0x7F))  # ASCII 可见区
# 去掉会破坏 fonttools 命令行/无字形的控制字符
chars = {c for c in chars if ord(c) >= 0x20}
text = "".join(sorted(chars))
print(f"collected {len(chars)} unique chars")

# 3) pyftsubset 子集化
from fontTools.subset import Subsetter, Options, load_font, save_font

opts = Options()
opts.flavor = "woff2"
opts.desubroutinize = True
opts.drop_tables += ["FFTM"]
opts.layout_features = ["*"]  # 保留字距等特性
opts.name_IDs = [1, 2, 3, 4, 6]
opts.notdef_outline = True

font = load_font(FONT_IN, opts)
subsetter = Subsetter(opts)
subsetter.populate(text=text)
subsetter.subset(font)
os.makedirs(os.path.dirname(FONT_OUT), exist_ok=True)
save_font(font, FONT_OUT, opts)

size = os.path.getsize(FONT_OUT)
print(f"saved {FONT_OUT} ({size/1024:.0f} KB)")

# 4) 校验：界面关键词必须全覆盖
from fontTools.ttLib import TTFont
cmap = TTFont(FONT_OUT).getBestCmap()
must = "正在盯的票余票监控支持作者加搜演出历史反馈我的有刷新看今天明天出发到达站开屏开始盯一吧数据来自猫眼每五分钟次问题建议留言求助全部置顶"
missing = [c for c in must if ord(c) not in cmap]
if missing:
    print("MISSING:", "".join(missing))
    sys.exit(1)
print("coverage check OK")
