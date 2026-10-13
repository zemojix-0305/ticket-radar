"""从用户那枚 1024×1024 小狐狸图生成小程序可用的透明底 mascot。

为什么必须处理
--------------
原图 483KB、1024×1024、**不透明**。小程序主包上限 2MB，
一张图吃掉 1/4 预算；而它是 RGB 不带 alpha，贴到任何背景上
都会露出一个方形紫底——在「我的」页圆形头像里尤其明显（会切出一个方角）。

所以这里做两件事：
1. 把接近背景色（#A28FAC 那一片）的像素抠成透明，得到干净的狐狸轮廓；
2. 缩到 160×160——mascot 在界面里最大也就 76px 显示，160 已经 2 倍超采样。

抠图阈值为什么这么定
--------------------
背景是**带渐变的紫**（左上角 #A492AD、右下角略深），
所以不能拿单一颜色做等值匹配——那样边缘会留下一圈紫色毛边。
做法是按「与紫底的色距离」判定，并且**中间色带全部保留**：
只把明确是紫的、以及明确离紫很远的（狐狸橙/奶白）分开，
中间的交给边缘羽化。这样描边不会有硬锯齿。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

#: 原图（1024×1024、483KB）放在**仓库根**而不是小程序包里——
#: 它只是抠图的输入，产出的 15KB 才是要进包的。多放一张 472KB 的图
#: 在 2MB 上限里等于白白浪费 1/4 预算。
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets_fox_src.png"
OUT = ROOT / "miniprogram" / "miniprogram" / "images" / "avatar_fox.png"

SIZE = 160
#: 背景紫的代表色（取自原图四角众数）
BG = (162, 143, 172)
#: 色距离小于这个值 → 当背景，抠掉
NEAR = 26
#: 大于这个值 → 一定是狐狸本体，保住
FAR = 62


def dist(px, bg) -> float:
    return sum((a - b) ** 2 for a, b in zip(px, bg)) ** 0.5


def main() -> None:
    im = Image.open(SRC).convert("RGB")
    px = im.load()
    w, h = im.size

    alpha = Image.new("L", (w, h), 255)
    ap = alpha.load()

    for y in range(h):
        for x in range(w):
            d = dist(px[x, y], BG)
            if d <= NEAR:
                ap[x, y] = 0            # 明确是底 → 全透
            elif d >= FAR:
                ap[x, y] = 255          # 明确是狐狸 → 不动
            else:
                # 中间地带线性羽化，避免硬边毛刺
                t = (d - NEAR) / (FAR - NEAR)
                ap[x, y] = int(t * 255)

    out = im.convert("RGBA")
    out.putalpha(alpha)

    # 先缩到 320 再用 LANCZOS 到 160：直接一步降会丢掉细边
    out = out.resize((SIZE * 2, SIZE * 2), Image.LANCZOS).resize(
        (SIZE, SIZE), Image.LANCZOS
    )
    out.save(OUT, optimize=True)

    kb = OUT.stat().st_size / 1024
    print(f"已生成 {OUT.name}  {SIZE}×{SIZE}  {kb:.1f}KB")


if __name__ == "__main__":
    main()
