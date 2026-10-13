# -*- coding: utf-8 -*-
"""抠图裁剪留言板素材。

输入：用户发的 3 张 1200x1600 参考图（白底 JPG）
  - img1: 4 张撕边便签（2x2，竖向长方形，带夹子）
  - img2: 4 张便签（2x2，近方形，带夹子）
  - img3: 12 个小夹子/图钉/回形针（4x3 网格，间距不均）

处理：白底转透明 → 连通域检测自动找每张贴纸的包围盒 →
  按包围盒裁剪 + 缩到小程序实用尺寸 → PNG（RGBA）
输出：miniprogram/miniprogram/images/board/
  note1..note8.png（便签，长边 ~480px）
  clip1..clip12.png（小夹子，长边 ~160px）
"""
import os
import numpy as np
from PIL import Image
from scipy import ndimage  # 如缺 scipy 用纯 numpy 方案兜底

SRC_DIR = r"D:\Net\WX_Log\xwechat_files\wxid_iu4gbwtnfnh522_ff23\temp\RWTemp\2026-10"
OUT_DIR = r"D:\Amyapp\ticket-radar\miniprogram\miniprogram\images\board"
os.makedirs(OUT_DIR, exist_ok=True)


def white_to_alpha(img, threshold=235, softness=250):
    """白底转透明。threshold 以下保留，softness 以上全透明，中间线性过渡。"""
    arr = np.array(img.convert("RGB")).astype(np.float32)
    # 用亮度近似白度（纯白 (255,255,255)）
    lum = arr.mean(axis=2)
    alpha = np.clip((softness - lum) / (softness - threshold), 0, 1)
    alpha = (alpha * 255).astype(np.uint8)
    rgba = np.dstack([arr.astype(np.uint8), alpha])
    return rgba


def find_components(alpha, min_area=3000):
    """在 alpha 通道上找连通域包围盒。"""
    mask = alpha > 40
    labeled, n = ndimage.label(mask)
    boxes = []
    for i in range(1, n + 1):
        ys, xs = np.where(labeled == i)
        if len(ys) < min_area:
            continue
        boxes.append((xs.min(), ys.min(), xs.max(), ys.max(), len(ys)))
    return boxes


def merge_boxes(boxes, merge_dist=30):
    """把重叠/邻近的包围盒合并（一张便签可能被撕边缝隙断开）。"""
    if not boxes:
        return []
    boxes = sorted(boxes, key=lambda b: (b[1], b[0]))
    merged = []
    for b in boxes:
        x0, y0, x1, y1, area = b
        placed = False
        for i, m in enumerate(merged):
            mx0, my0, mx1, my1 = m
            # 扩张 merge_dist 后相交则合并
            if not (x0 > mx1 + merge_dist or x1 < mx0 - merge_dist
                    or y0 > my1 + merge_dist or y1 < my0 - merge_dist):
                merged[i] = (min(mx0, x0), min(my0, y0), max(mx1, x1), max(my1, y1))
                placed = True
                break
        if not placed:
            merged.append((x0, y0, x1, y1))
    # 合并后可能产生连锁重叠，再跑一遍直到稳定
    changed = True
    while changed:
        changed = False
        out = []
        for b in merged:
            x0, y0, x1, y1 = b
            placed = False
            for i, m in enumerate(out):
                mx0, my0, mx1, my1 = m
                if not (x0 > mx1 + 20 or x1 < mx0 - 20 or y0 > my1 + 20 or y1 < my0 - 20):
                    out[i] = (min(mx0, x0), min(my0, y0), max(mx1, x1), max(my1, y1))
                    placed = True
                    changed = True
                    break
            if not placed:
                out.append(b)
        merged = out
    return merged


def trim_and_save(rgba, box, name, max_side):
    x0, y0, x1, y1 = box
    crop = rgba[y0:y1 + 1, x0:x1 + 1]
    # 去掉完全透明的边缘行/列
    a = crop[:, :, 3]
    rows = np.where(a.max(axis=1) > 20)[0]
    cols = np.where(a.max(axis=0) > 20)[0]
    if not len(rows) or not len(cols):
        print(f"  skip {name}: empty")
        return False
    crop = crop[rows.min():rows.max() + 1, cols.min():cols.max() + 1]
    h, w = crop.shape[:2]
    scale = max_side / max(h, w)
    if scale < 1:
        crop = Image.fromarray(crop).resize(
            (max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        crop = np.array(crop)
    Image.fromarray(crop).save(os.path.join(OUT_DIR, name), optimize=True)
    kb = os.path.getsize(os.path.join(OUT_DIR, name)) / 1024
    print(f"  {name}: {w}x{h} -> {crop.shape[1]}x{crop.shape[0]}, {kb:.0f}KB")
    return True


def process_grid(src, names, max_side, min_area=3000, merge_dist=30, grid_hint=None):
    """按连通域自动切分一张源图。"""
    img = Image.open(os.path.join(SRC_DIR, src))
    rgba = white_to_alpha(img)
    alpha = rgba[:, :, 3]
    boxes = find_components(alpha, min_area)
    print(f"{src}: {len(boxes)} raw components")
    boxes = merge_boxes(boxes, merge_dist)
    print(f"  merged -> {len(boxes)}")
    # 按从左到右、从上到下排序
    boxes.sort(key=lambda b: (round(b[1] / 200), b[0]))
    if len(boxes) != len(names):
        print(f"  WARNING: expected {len(names)}, got {len(boxes)} — 前缀名仍按顺序分配")
    for i, b in enumerate(boxes):
        name = names[i] if i < len(names) else f"extra{i}.png"
        trim_and_save(rgba, b[:4], name, max_side)


# ---- 图1/图2：各 4 张便签 ----
process_grid(
    "f8fa82051e01642c202cad7c8d64f38a.jpg",
    ["note1.png", "note2.png", "note3.png", "note4.png"],
    max_side=480, min_area=50000)

process_grid(
    "bb13f1be8897e33757380fc7c091edf7.jpg",
    ["note5.png", "note6.png", "note7.png", "note8.png"],
    max_side=480, min_area=50000)

# ---- 图3：12 个小夹子（图钉那组是 5 个钉子连成一片，作为一整个"图钉组"） ----
process_grid(
    "9a91e5870abe54907a2a453b693c13a5.jpg",
    ["clip1.png", "clip2.png", "clip3.png",
     "clip4.png", "clip5.png", "clip6.png",
     "clip7.png", "clip8.png", "clip9.png",
     "clip10.png", "clip11.png", "clip12.png"],
    max_side=200, min_area=2000)

print("ALL DONE ->", OUT_DIR)
