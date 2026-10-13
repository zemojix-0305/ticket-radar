# -*- coding: utf-8 -*-
"""切分三张新贴纸素材图 → miniprogram/miniprogram/images/board2/

三张图分别是：
  collage  手绘奶油拼贴（兔子/大星/蝴蝶/拥抱小人/千纸鹤/You-Me 拼图…）
  origami  折纸星星 + 格纹星星 + 雨滴 + 雨景贴纸大张
  cartoon  蓝色卡通贴纸（4x3 网格）

做法：白底转透明 → alpha 连通域 → 膨胀分组（把散点/双件合成一组）
     → 按组裁剪 → 长边限 240 → PNG8 压缩。
过大的连通域（>50% 画布，如雨景大贴纸张）整张保留为一个素材。
"""
import os
import sys

import numpy as np
from PIL import Image
from scipy import ndimage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cut_board_assets import white_to_alpha  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "miniprogram", "miniprogram", "images", "board2")

SOURCES = [
    ("origami", r"D:\Net\WX_Log\xwechat_files\wxid_iu4gbwtnfnh522_ff23\temp\RWTemp\2026-10\97040c63535f0509ec57a269f007e2e1.jpg"),
    ("collage", r"D:\Net\WX_Log\xwechat_files\wxid_iu4gbwtnfnh522_ff23\temp\RWTemp\2026-10\8ba419ab1b593c3f8ce272e4e5a177a2.jpg"),
    ("cartoon", r"D:\Net\WX_Log\xwechat_files\wxid_iu4gbwtnfnh522_ff23\temp\RWTemp\2026-10\d860cc30cd561b7e9d4bfc409171e6ea.jpg"),
]


def group_boxes(mask, dilate_px, min_area, canvas_frac_skip):
    """膨胀连通域 → 返回按面积排序的合并包围盒。"""
    dil = ndimage.binary_dilation(mask, iterations=dilate_px)
    labeled, n = ndimage.label(dil)
    boxes = []
    H, W = mask.shape
    for i in range(1, n + 1):
        ys, xs = np.where((labeled == i) & mask)
        if len(ys) < min_area:
            continue
        x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
        area_frac = (x1 - x0) * (y1 - y0) / (H * W)
        boxes.append((x0, y0, x1, y1, area_frac))
    return boxes


def cut_one(rgba, box, name, max_side):
    x0, y0, x1, y1 = box
    crop = rgba[max(0, y0 - 4):y1 + 5, max(0, x0 - 4):x1 + 5]
    a = crop[:, :, 3]
    rows = np.where(a.max(axis=1) > 20)[0]
    cols = np.where(a.max(axis=0) > 20)[0]
    if not len(rows) or not len(cols):
        return None
    crop = crop[rows.min():rows.max() + 1, cols.min():cols.max() + 1]
    h, w = crop.shape[:2]
    scale = 200 / max(h, w)
    if scale < 1:
        crop = np.array(Image.fromarray(crop).resize(
            (int(w * scale), int(h * scale)), Image.LANCZOS))
    path = os.path.join(OUT, name)
    # PNG8 调色板：128 色，视觉无差体积减半以上
    Image.fromarray(crop).quantize(colors=128, method=Image.FASTOCTREE)\
        .save(path, optimize=True)
    return os.path.getsize(path)


def main():
    os.makedirs(OUT, exist_ok=True)
    total = 0
    for key, src in SOURCES:
        img = Image.open(src).convert("RGB")
        rgba = white_to_alpha(img)
        mask = rgba[:, :, 3] > 40
        H, W = mask.shape

        if key == "origami":
            # 雨景大贴纸张单独抠（右下角整块），其余走通用分组
            boxes = group_boxes(mask, dilate_px=14, min_area=2500,
                                canvas_frac_skip=1.1)
        else:
            boxes = group_boxes(mask, dilate_px=18, min_area=2500,
                                canvas_frac_skip=1.1)

        boxes.sort(key=lambda b: (round(b[1] / (H / 4)), b[0]))  # 按行序
        kept = 0
        for idx, (x0, y0, x1, y1, frac) in enumerate(boxes, 1):
            # 面积太碎的（<0.15% 画布）多为噪点，跳过
            if frac < 0.0015:
                continue
            kept += 1
            size = cut_one(rgba, (x0, y0, x1, y1), f"{key}{kept}.png", max_side=240)
            total += size
            print(f"  {key}{kept}.png  x{x0}-{x1} y{y0}-{y1}  {size//1024}KB")
        print(f"{key}: kept {kept}")
    print(f"TOTAL {total/1024:.0f}KB")


if __name__ == "__main__":
    main()
