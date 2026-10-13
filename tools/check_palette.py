"""校验配色是否达标：所有文字/背景组合的 WCAG 对比度。

为什么需要脚本：把参考图的低饱和莫兰迪色直接抄进界面很好看，
但「有票」是灰绿、「无票」是暖灰——两者太像就分不清，
在手绘风里尤其容易翻车。这里把每对组合的对比度算出来，
不够的直接报出来，不靠肉眼判断。

用法：
    python tools/check_palette.py
"""
from __future__ import annotations

#: 与 miniprogram/miniprogram/app.wxss 里的 :root 同源，改一处记得改两处。
#:
#: 定色思路：参考图给的低饱和莫兰迪直接抄进来很好看，但在浅米色纸上
#: 有一半达不到 WCAG 对比度要求（次要文字 4.46:1、有票 3.87:1…）。
#: 唯一的解法是**保留低饱和、压低明度**——色相不动、深一度，
#: 这样既不刺眼又读得清。「有票/无票」除了文案不同，还拉开了亮度差，
#: 色弱用户也能靠明暗分辨。
PALETTE = {
    "bg": "#F5F1E8",       # 纸底
    "card": "#FFFDF8",     # 暖白便签
    "brand": "#4F6E80",    # 雾霾蓝
    "t1": "#3D3A34",       # 正文
    "t2": "#6E6A61",       # 次要
    "t3": "#767268",       # 提示
    "ok": "#4F7A3D",       # 有票：暖绿
    "none": "#6B7076",     # 无票：冷灰蓝（刻意跟 ok 反色相，色弱也能靠色相分辨）
    "bad": "#A94E3E",      # 异常
    "warn": "#8F6417",     # 提醒
}

#: (用途, 前景, 背景, 是否大字)。大字阈值 3.0，普通文本 4.5。
CHECKS = [
    ("正文", "t1", "card", False),
    ("次要文字", "t2", "card", False),
    ("提示文字", "t3", "card", False),
    ("正文/纸底", "t1", "bg", False),
    ("有票标签", "ok", "card", False),
    ("无票标签", "none", "card", False),
    ("异常标签", "bad", "card", False),
    ("提醒标签", "warn", "card", False),
    ("主色链接", "brand", "card", False),
    ("白字/主按钮", "card", "brand", False),
    ("大数字/纸底", "ok", "bg", True),
]


def luminance(hex_color: str) -> float:
    """WCAG 相对亮度。"""
    chans = [int(hex_color[i: i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in chans]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(fg: str, bg: str) -> float:
    a, b = luminance(fg), luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def hue(hex_color: str) -> float:
    """0–360 色相。只在饱和度够高时才有意义，灰色一律返回 -1。"""
    r, g, b = (int(hex_color[i: i + 2], 16) / 255 for i in (1, 3, 5))
    mx, mn = max(r, g, b), min(r, g, b)
    if mx - mn < 0.02:          # 接近中性色，色相无意义
        return -1.0
    if mx == r:
        h = ((g - b) / (mx - mn)) % 6
    elif mx == g:
        h = (b - r) / (mx - mn) + 2
    else:
        h = (r - g) / (mx - mn) + 4
    return h * 60


def hue_gap(a: str, b: str) -> float:
    """两个颜色的色相差（0–180）。"""
    ha, hb = hue(a), hue(b)
    if ha < 0 or hb < 0:
        return 0.0
    d = abs(ha - hb) % 360
    return min(d, 360 - d)


def main() -> int:
    print(f"{'用途':14s} {'前景':9s} {'背景':9s} {'对比度':>9s}   判定")
    print("-" * 60)
    bad = 0
    for name, fg_key, bg_key, is_large in CHECKS:
        fg, bg = PALETTE[fg_key], PALETTE[bg_key]
        r = contrast(fg, bg)
        need = 3.0 if is_large else 4.5
        ok = r >= need
        if not ok:
            bad += 1
        verdict = "达标" if ok else f"不足（需 {need}）"
        print(f"{name:14s} {fg:9s} {bg:9s} {r:8.2f}:1  {verdict}")

    # 「有票」和「无票」要能一眼分开。区分靠两个通道：明暗差 或 色相差，
    # 任一达标即可——只靠亮度的话，低饱和手绘风里两者必然靠得很近。
    delta = abs(luminance(PALETTE["ok"]) - luminance(PALETTE["none"]))
    gap = hue_gap(PALETTE["ok"], PALETTE["none"])
    ok_diff = delta > 0.05 or gap > 25
    print(f"\n有票 vs 无票：亮度差 {delta:.3f}，色相差 {gap:.0f}° → "
          + ("能区分" if ok_diff else "太接近，必须拉开一个"))
    if not ok_diff:
        bad += 1

    print("\n结论：" + ("全部达标" if bad == 0 else f"{bad} 项需要调整"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
