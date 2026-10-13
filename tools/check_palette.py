"""校验配色是否达标：所有文字/背景组合的 WCAG 对比度。

用法：
    python tools/check_palette.py
"""
from __future__ import annotations

#: 与 miniprogram/miniprogram/app.wxss 里的 page{} 同源，改一处记得改两处。
#:
#: 纸感票根手账（第十七轮）。暖白期刊纸底 + 墨色文字 + 单一茶绿强调色；
#: 语义色：朱砂（热卖/回流/售罄）、琥珀（额度提醒）；形状：纸直角 + 药丸。
PALETTE = {
    "bg": "#F7F2E7",       # 页面底：暖白期刊纸
    "card": "#FDFBF4",     # 卡片纸
    "paper-deep": "#EFE8D7",  # 牛皮纸块
    "brand": "#445C38",    # 茶绿（唯一强调色）
    "brand-deep": "#36492C",
    "t1": "#2A2620",       # 主文字：暖墨
    "t2": "#57503F",       # 次要
    "t3": "#6E6558",       # 提示/占位
    "ok": "#445C38",       # 有票/在售：茶绿
    "none": "#6E6558",     # 无票/未开售：墨灰
    "bad": "#A23B2E",      # 售罄/异常：朱砂
    "warn": "#7E6118",     # 提醒：琥珀
    "ok-weak": "#E9EDDF",  # 茶绿浅底
    "none-weak": "#F3EDDE",
    "bad-weak": "#F5E4E0",
    "warn-weak": "#F4EBD4",
    "brand-weak": "#E9EDDF",
}

#: (用途, 前景, 背景, 是否大字)。大字阈值 3.0，普通文本 4.5。
CHECKS = [
    ("正文", "t1", "card", False),
    ("次要文字", "t2", "card", False),
    ("提示文字", "t3", "card", False),
    ("正文/纸底", "t1", "bg", False),
    ("次要/纸底", "t2", "bg", False),
    ("占位/纸底", "t3", "bg", False),
    ("次要/牛皮纸底", "t2", "paper-deep", False),
    ("有票印章", "ok", "card", False),
    ("无票印章", "none", "card", False),
    ("售罄印章", "bad", "card", False),
    ("提醒印章", "warn", "card", False),
    ("主色链接", "brand", "card", False),
    ("纸白字/主按钮", "card", "brand", False),
    ("大数字/纸底", "brand", "bg", True),
    # 浅底上的深字
    ("茶绿浅底/绿字", "ok", "ok-weak", False),
    ("灰浅底/灰字", "none", "none-weak", False),
    ("朱砂浅底/红字", "bad", "bad-weak", False),
    ("琥珀浅底/琥珀字", "warn", "warn-weak", False),
    ("茶绿浅底/主色字", "brand", "brand-weak", False),
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
