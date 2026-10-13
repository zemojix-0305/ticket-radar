"""生成底部 tabBar 的 10 个图标（5 个 tab × 普通/选中两态）。

为什么要脚本生成而不是手工放图片文件：
配色是全局令牌（app.wxss 里的 --brand），换了主色整套图标都得跟着重出，
脚本跑一次就全部重新生成，不用开设计软件。

用法：
    python tools/make_tabicons.py
    python tools/make_tabicons.py --brand "#0F7B6B"     # 换主色

规格：81×81 px、透明背景、线宽 6、圆头。
PIL 不支持圆头线段，所以两端手动补圆点。
先在 4 倍画布上画再缩小，曲线和圆角才不会锯齿。
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 81
SS = 4                      # 超采样倍数
W = 6                      # 主线宽（81 空间）
W_THIN = 4                  # 细线宽
OUT_DIR = Path(__file__).resolve().parent.parent / "miniprogram" / "miniprogram" / "images"

DEFAULT_IDLE = "#9CA3AF"
DEFAULT_BRAND = "#185FA5"

Pen = ImageDraw.ImageDraw


def line(d: Pen, pts: list[tuple[int, int]], c: str, w: int = W) -> None:
    sp = [(x * SS, y * SS) for x, y in pts]
    d.line(sp, fill=c, width=w * SS, joint="curve")
    for sx, sy in (sp[0], sp[-1]):
        r = w * SS / 2
        d.ellipse([sx - r, sy - r, sx + r, sy + r], fill=c)


def circle(d: Pen, cx: int, cy: int, r: int, c: str, w: int = W_THIN) -> None:
    d.ellipse(
        [(cx - r) * SS, (cy - r) * SS, (cx + r) * SS, (cy + r) * SS],
        outline=c,
        width=w * SS,
    )


def arc(d: Pen, box: tuple[int, int, int, int], start: int, end: int, c: str, w: int = W) -> None:
    d.arc([v * SS for v in box], start=start, end=end, fill=c, width=w * SS)


def icon_home(d: Pen, c: str) -> None:
    """首页：狐狸侧脸轮廓 + 房子——原创标识，不是通用图标。"""
    # 房子
    line(d, [(22, 39), (40, 23), (58, 39)], c)
    line(d, [(26, 37), (26, 60)], c)
    line(d, [(54, 37), (54, 60)], c)
    # 狐狸耳朵：房子顶上加两个尖角，一眼是狐狸
    line(d, [(34, 30), (38, 22), (43, 29)], c, W_THIN)
    line(d, [(45, 29), (49, 22), (52, 31)], c, W_THIN)


def icon_shows(d: Pen, c: str) -> None:
    """演出：放大镜——「搜名字直接加」是我们相对竞品的关键动作。"""
    circle(d, 34, 34, 17, c, W)
    line(d, [(46, 46), (63, 63)], c)


def icon_history(d: Pen, c: str) -> None:
    """历史：折线趋势——竞品完全没有的能力。"""
    line(d, [(17, 21), (17, 61), (64, 61)], c)
    line(d, [(24, 52), (36, 37), (47, 45), (63, 26)], c)


def icon_discuss(d: Pen, c: str) -> None:
    """讨论：对话气泡 + 狐狸耳。"""
    arc(d, (20, 22, 60, 56), 0, 360, c)
    line(d, [(30, 56), (35, 67), (43, 56)], c)
    # 气泡上方两只尖耳，破掉「标准气泡」的通用感
    line(d, [(32, 22), (36, 15), (40, 22)], c, W_THIN)
    line(d, [(42, 22), (46, 15), (49, 22)], c, W_THIN)


def icon_mine(d: Pen, c: str) -> None:
    """我的：头 + 肩，头上加狐狸耳。"""
    circle(d, 40, 32, 12, c, 5)
    arc(d, (21, 48, 59, 82), 180, 360, c)
    line(d, [(33, 22), (37, 15), (41, 21)], c, W_THIN)
    line(d, [(43, 21), (47, 15), (50, 23)], c, W_THIN)


ICONS = {
    "home": icon_home,
    "shows": icon_shows,
    "history": icon_history,
    "discuss": icon_discuss,
    "mine": icon_mine,
}


def render(name: str, color: str, out: Path) -> None:
    img = Image.new("RGBA", (SIZE * SS, SIZE * SS), (0, 0, 0, 0))
    ICONS[name](ImageDraw.Draw(img), color)
    img.resize((SIZE, SIZE), Image.LANCZOS).save(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", default=DEFAULT_BRAND, help="选中态主色")
    ap.add_argument("--idle", default=DEFAULT_IDLE, help="普通态颜色")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name in ICONS:
        render(name, args.idle, OUT_DIR / f"tab_{name}.png")
        render(name, args.brand, OUT_DIR / f"tab_{name}_on.png")
    print(f"已生成 {len(ICONS) * 2} 个图标 → {OUT_DIR}")


if __name__ == "__main__":
    main()
