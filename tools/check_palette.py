"""校验配色是否达标：所有文字/背景组合的 WCAG 对比度。

用法：
    python tools/check_palette.py
"""
from __future__ import annotations

#: 与 miniprogram/miniprogram/app.wxss 里的 page{} 同源，改一处记得改两处。
#:
#: 白底 coquette 风（第十四轮）。纯白底 + 细线分区；
#: 唯一点缀色深玫瑰 #BC3565；语义色：绿/灰/红/琥珀。
PALETTE = {
    "bg": "#FFFFFF",       # 纯白底
    "card": "#FFFFFF",     # 卡片同为白
    "brand": "#BC3565",    # 深玫瑰粉（coquette 唯一点缀色）
    "t1": "#1A1A1A",       # 正文
    "t2": "#6E6E73",       # 次要
    "t3": "#75757A",       # 提示
    "ok": "#1E7A46",       # 有票：绿
    "none": "#6A6A6F",     # 无票：灰（与 ok 色相差大，色弱可辨）
    "bad": "#BC3B2C",      # 异常：红
    "warn": "#96690A",     # 提醒：琥珀
    "ok-weak": "#E6F4EB",  # 浅底
    "none-weak": "#F4F4F5",
    "bad-weak": "#FBEAE7",
    "warn-weak": "#FBF3DC",
    "brand-weak": "#FBE9EF",
}

#: (用途, 前景, 背景, 是否大字)。大字阈值 3.0，普通文本 4.5。
CHECKS = [
    ("正文", "t1", "card", False),
    ("次要文字", "t2", "card", False),
    ("提示文字", "t3", "card", False),
    ("正文/纸底", "t1", "bg", False),
    ("次要/纸底", "t2", "bg", False),
    ("有票标签", "ok", "card", False),
    ("无票标签", "none", "card", False),
    ("异常标签", "bad", "card", False),
    ("提醒标签", "warn", "card", False),
    ("主色链接", "brand", "card", False),
    ("白字/主按钮", "card", "brand", False),
    ("大数字/纸底", "ok", "bg", True),
    ("大数字/主色", "brand", "bg", True),
    # pastel 浅底上的深字
    ("薄荷底/深绿字", "ok", "ok-weak", False),
    ("灰底/灰字", "none", "none-weak", False),
    ("粉底/莓粉字", "brand", "brand-weak", False),
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
    """0–360 色相。灰色返回 -1。"""
    r, g, b = (int(hex_color[i: i + 2], 16) / 255 for i in (1, 3, 5))
    mx, mn = max(r, g, b), min(r, g, b)
    if mx - mn < 0.02:
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
    fail = 0
    print(f"{'用途':<16} {'前景':<10} {'背景':<14} {'对比度':>10} 判定")
    print("-" * 60)
    for name, fgk, bgk, large in CHECKS:
        fg, bg = PALETTE[fgk], PALETTE[bgk]
        c = contrast(fg, bg)
        threshold = 3.0 if large else 4.5
        ok = c >= threshold
        if not ok:
            fail += 1
        status = "达标" if ok else f"不足（需 {threshold}）"
        print(f"{name:<16} {fg:<10} {bg:<14} {c:>6.2f}:1  {status}")

    ok_h = hue(PALETTE["ok"])
    none_h = hue(PALETTE["none"])
    print()
    if ok_h >= 0 and none_h >= 0:
        print(f"有票 vs 无票：色相差 {hue_gap(PALETTE['ok'], PALETTE['none']):.0f}° → 能区分" if hue_gap(PALETTE["ok"], PALETTE["none"]) > 30 else "警告：有票/无票色相差太小")
    else:
        print("有票/无票至少一个是灰色，靠亮度也能区分")
    print()
    print("结论：" + ("全部达标" if fail == 0 else f"{fail} 项需要调整"))
    return fail


if __name__ == "__main__":
    raise SystemExit(main())
