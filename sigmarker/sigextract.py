"""sigextract.py — 签名水印算法核心

纯函数、无状态模块。不依赖 PyQt，可独立 CLI 测试。

依赖: fitz(PyMuPDF), numpy, Pillow
"""

from __future__ import annotations

import argparse
from io import BytesIO
from math import isfinite
from dataclasses import dataclass
from typing import Optional, Tuple

import cv2
import fitz  # PyMuPDF
import numpy as np
from PIL import Image, ImageFilter

DEFAULT_OUTPUT_DPI = 300


# ---------- 参数数据类 ----------

@dataclass
class WatermarkParams:
    """水印渲染参数。所有参数都有合理默认值。"""
    threshold: int = 140          # 灰度阈值 (< 此值视为墨迹), 60-200
    feather: int = 0              # alpha 边缘羽化像素, 0-40
    contrast: float = 1.0         # 对比度, 0.5-2.0
    opacity: float = 1.0          # 整体不透明度, 0.1-1.0
    sharpness: float = 0.0        # 锐化强度, 0-2.0 (用于手机拍照模糊)
    color_mode: str = 'black'     # 'black' | 'white' | 'original'
    clear_background: bool = True # 是否将近白背景 alpha 压到 0
    denoise: bool = True          # 清除孤立扫描噪点, 保留相邻细笔画
    output_dpi: int = DEFAULT_OUTPUT_DPI  # 物理输出尺寸基准, 不重采样像素
    ink_density: float = 1.0      # 去背景后的墨迹浓度, 1-3 倍, 不影响纸色判断

    # 用于自动检测签名范围
    region_top_ratio: float = 0.25  # 搜索区域高度占页面比例
    region_left_ratio: float = 0.5  # 搜索区域宽度占页面比例

    # 自动检测时的 bbox 外扩像素 (给签名留点呼吸空间)
    bbox_padding: int = 32


# ---------- PDF 加载 ----------

def load_pdf_page(pdf_path: str, dpi: int = 300, page_index: int = 0) -> Image.Image:
    """加载 PDF 某页并渲染成 RGB Pillow 图像。"""
    with fitz.open(pdf_path) as doc:
        if not 0 <= page_index < len(doc):
            raise IndexError(f"page_index {page_index} out of range, total {len(doc)}")
        pix = doc[page_index].get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False)
        return Image.frombytes('RGB', (pix.width, pix.height), pix.samples)


# ---------- 背景校正和墨迹范围 ----------

def _estimate_local_bg(g: np.ndarray) -> np.ndarray:
    """用亮度包络估纸色, 避免均值模糊把笔画混进背景造成白边。"""
    h, w = g.shape
    # 在受限分辨率估背景, 大照片无需运行巨大的形态学核。
    scale = min(1.0, 1600.0 / max(h, w))
    small = cv2.resize(g.astype(np.uint8), None, fx=scale, fy=scale,
                       interpolation=cv2.INTER_AREA) if scale < 1 else g.astype(np.uint8)
    radius = max(9, min(61, min(small.shape) // 10)) | 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius, radius))
    envelope = cv2.morphologyEx(small, cv2.MORPH_CLOSE, kernel)
    # 中值平滑不会把跨越纸边的暗色桌面平均回白纸。
    envelope = cv2.medianBlur(envelope, 5)
    return cv2.resize(envelope, (w, h), interpolation=cv2.INTER_LINEAR).astype(np.float32)


def _ink_strength(gray: np.ndarray) -> np.ndarray:
    g = np.asarray(gray, dtype=np.float32)
    bg = _estimate_local_bg(g)
    # 比值校正同时适应灰纸和曝光偏暗的手机照片。
    return np.clip((bg - g) * 255.0 / np.maximum(bg, 32.0), 0, 255)


def _analyze_components(mask: np.ndarray, downsample: int = 4):
    """兼容诊断脚本的连通域接口; 补齐末行末列, 不再丢失边缘笔画。"""
    h, w = mask.shape
    f = max(1, int(downsample))
    padded = np.pad(mask, ((0, (-h) % f), (0, (-w) % f)))
    small = padded.reshape(padded.shape[0] // f, f, padded.shape[1] // f, f).any(axis=(1, 3))
    count, _, stats, _ = cv2.connectedComponentsWithStats(small.astype(np.uint8), 8)
    return {i: (int(row[4]), *map(int, row[:4])) for i, row in enumerate(stats[1:], 1)}


def _signature_mask(gray: np.ndarray, dark_threshold: int = 150) -> np.ndarray:
    strength = _ink_strength(gray)
    # 噪声地板采用中位数/MAD; 限制自适应门槛, 保留淡色签字笔。
    median = float(np.median(strength))
    mad = float(np.median(np.abs(strength - median)))
    cutoff = max(10.0, min(26.0, median + 6 * mad), (255 - dark_threshold) * 0.10)
    mask = (strength >= cutoff).astype(np.uint8)
    h, w = mask.shape
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    keep = np.zeros(count, dtype=np.uint8)
    min_area = max(3, int(h * w * 0.000003))
    # 弱阈值保留抗锯齿边缘, 但候选连通域必须有足够的可信墨迹。
    # 纸纹/压缩噪声经膨胀可连成覆盖半页的大组, 应在分组前排除。
    core_cutoff = cutoff + max(6.0, 3 * mad)
    core_counts = np.bincount(labels[strength >= core_cutoff], minlength=count)
    for i, (x, y, bw, bh, area) in enumerate(stats[1:], 1):
        if area < min_area:
            continue
        if core_counts[i] < max(2, area * .10):
            continue
        density = area / (bw * bh)
        touches = x <= 1 or y <= 1 or x + bw >= w - 1 or y + bh >= h - 1
        # 只排除具备边框/桌沿特征的块, 不再固定舍弃页面四周 10%。
        if touches and (bw > w * .55 or bh > h * .55):
            continue
        if touches:
            border_hits = (np.count_nonzero(labels[0, x:x + bw] == i)
                           + np.count_nonzero(labels[-1, x:x + bw] == i)
                           + np.count_nonzero(labels[y:y + bh, 0] == i)
                           + np.count_nonzero(labels[y:y + bh, -1] == i))
            if border_hits > max(12, min(bw, bh) * .5):
                continue
        if max(bw / max(bh, 1), bh / max(bw, 1)) > 35 and max(bw, bh) > min(h, w) * .30:
            continue
        if density > .70 and area > h * w * .02:
            continue
        keep[i] = 1
    cleaned = keep[labels]
    if not cleaned.any():
        return cleaned
    # 将相邻笔画聚为候选组; 只剔除远离主体的微小污点。
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT,
                                     (max(7, w // 40), max(7, h // 40)))
    grouped = cv2.dilate(cleaned, kernel)
    n, groups, group_stats, _ = cv2.connectedComponentsWithStats(grouped, 8)
    weights = np.bincount(groups.ravel(), weights=cleaned.ravel(), minlength=n)
    weights[0] = 0
    scores = weights.copy()
    for i, (x, y, bw, bh, _) in enumerate(group_stats[1:], 1):
        if x == 0 or y == 0 or x + bw == w or y + bh == h:
            scores[i] *= .05
    anchor = int(scores.argmax())
    ax, ay, aw, ah, _ = group_stats[anchor]
    accepted = np.zeros(n, dtype=bool)
    reach = max(12, min(aw, ah) * .60)
    for i, (x, y, bw, bh, _) in enumerate(group_stats[1:], 1):
        gap_x = max(0, ax - x - bw, x - ax - aw)
        gap_y = max(0, ay - y - bh, y - ay - ah)
        if weights[i] >= max(min_area, weights[anchor] * .025) and np.hypot(gap_x, gap_y) <= reach:
            accepted[i] = True
    return (cleaned & accepted[groups]).astype(np.uint8)


def _largest_components_bbox(gray: np.ndarray, dark_threshold: int = 150,
                             edge_trim: float = 0.0) -> Optional[Tuple[int, int, int, int]]:
    h, w = gray.shape
    scale = min(1.0, 1600.0 / max(h, w))
    sample = cv2.resize(np.asarray(gray, dtype=np.uint8), None, fx=scale, fy=scale,
                        interpolation=cv2.INTER_AREA) if scale < 1 else gray
    mask = _signature_mask(sample, dark_threshold)
    ys, xs = np.nonzero(mask)
    if not len(xs):
        return None
    sx, sy = w / sample.shape[1], h / sample.shape[0]
    return (int(np.floor(xs.min() * sx)), int(np.floor(ys.min() * sy)),
            min(w, int(np.ceil((xs.max() + 1) * sx))),
            min(h, int(np.ceil((ys.max() + 1) * sy))))


def detect_signature_orientation(gray: np.ndarray, dark_threshold: int = 150) -> bool:
    """只在有效墨迹明显纵向排列时转正, 空白图不旋转。"""
    box = _largest_components_bbox(gray, dark_threshold)
    return bool(box and (box[3] - box[1]) > (box[2] - box[0]) * 1.6)


def detect_ink_bbox(gray: np.ndarray,
                    region_hint: Optional[Tuple[float, float, float, float]] = None,
                    dark_threshold: int = 150, padding: int = 32) -> Tuple[int, int, int, int]:
    """背景校正后找墨迹并集; region_hint 为归一化搜索范围, 坐标右下角不包含。"""
    h, w = gray.shape
    if not h or not w:
        raise ValueError('图像尺寸为空')
    ox = oy = 0
    region = gray
    if region_hint is not None:
        x0, y0, x1, y1 = region_hint
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            raise ValueError('搜索范围必须为 0 到 1 之间的有效矩形')
        ox, oy = int(x0 * w), int(y0 * h)
        region = gray[oy:max(oy + 1, int(np.ceil(y1 * h))),
                      ox:max(ox + 1, int(np.ceil(x1 * w)))]
    box = _largest_components_bbox(region, dark_threshold)
    if box is None:
        return (ox, oy, ox + region.shape[1], oy + region.shape[0])
    x0, y0, x1, y1 = box
    pad = max(0, int(padding))
    return (max(ox, ox + x0 - pad), max(oy, oy + y0 - pad),
            min(ox + region.shape[1], ox + x1 + pad),
            min(oy + region.shape[0], oy + y1 + pad))


# ---------- 核心: 渲染透明水印 ----------

def render_watermark(crop_rgb: Image.Image, params: WatermarkParams) -> Image.Image:
    """先分离纸色与墨迹, 再构造直通 alpha, 原彩模式也去除纸张白边。"""
    rgb = np.asarray(crop_rgb.convert('RGB')).astype(np.float32)
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    # 白纸拍照常带灰度和阴影, 纸色校正属于提取必经步骤。
    strength = _ink_strength(g)
    strength *= params.contrast
    if params.sharpness > 0:
        blur = cv2.GaussianBlur(strength, (0, 0), 1)
        strength = np.clip(strength + params.sharpness * (strength - blur), 0, 255)
    # 阈值越大越能保留淡笔画。背景门槛消掉纸纹和 JPEG 轻噪声。
    floor = max(3.0, (255 - params.threshold) * .10) if params.clear_background else 0.0
    active = strength > floor
    if params.denoise and active.any():
        count, labels, stats, _ = cv2.connectedComponentsWithStats(active.astype(np.uint8), 8)
        keep = np.ones(count, dtype=bool)
        keep[0] = False
        min_area = max(2, min(12, int(g.size * .000008)))
        for i, row in enumerate(stats[1:], 1):
            if row[4] < min_area:
                keep[i] = False
        active = keep[labels]
    samples = strength[active]
    anchor = max(floor + 12, float(np.percentile(samples, 90))) if samples.size else 255.0
    # 保持抗锯齿连续性, 同时将淡墨主体拉到可用的水印浓度。
    alpha = np.clip((strength - floor) / max(anchor - floor, 1), 0, 1)
    alpha[~active] = 0
    if params.feather > 0:
        blurred = Image.fromarray((alpha * 255).astype(np.uint8)).filter(
            ImageFilter.GaussianBlur(params.feather))
        alpha = np.asarray(blurred).astype(np.float32) / 255
    # 在背景清除和归一化后加深笔画, 避免再次被归一化抵消。
    # 透明处仍为 0; 整体不透明度在最后应用, 继续控制水印透明程度。
    if params.ink_density != 1.0:
        alpha = np.clip(alpha * np.clip(params.ink_density, 1.0, 3.0), 0, 1)
    rgba = np.zeros((*g.shape, 4), dtype=np.uint8)
    if params.color_mode == 'white':
        rgba[..., :3] = 255
    elif params.color_mode == 'original' and samples.size:
        # 按像素反解白纸混色, 保留不同墨色, 避免抗锯齿边缘带纸白。
        bg = _estimate_local_bg(g)
        normalized = np.clip(rgb * 255 / np.maximum(bg[..., None], 32), 0, 255)
        intrinsic_alpha = np.maximum((255 - normalized.min(axis=2)) / 255, .01)
        ink = (normalized - (1 - intrinsic_alpha[..., None]) * 255) / intrinsic_alpha[..., None]
        rgba[..., :3] = np.clip(ink, 0, 255).astype(np.uint8)
    rgba[..., 3] = np.rint(alpha * np.clip(params.opacity, 0, 1) * 255).astype(np.uint8)
    return Image.fromarray(rgba)


# ---------- 导出 ----------

def _validated_dpi(dpi):
    dpi = float(dpi)
    if not isfinite(dpi) or dpi <= 0:
        raise ValueError('输出 DPI 必须是大于 0 的有限数值。')
    return dpi


def export_png(rgba_img: Image.Image, path, dpi=DEFAULT_OUTPUT_DPI) -> None:
    """写入明确的 DPI/pHYs 信息, 像素和透明度不变; path 也可为 BytesIO。"""
    dpi = _validated_dpi(dpi)
    rgba_img.save(path, format='PNG', dpi=(dpi, dpi))


def watermark_pdf_bytes(rgba_img: Image.Image, dpi=DEFAULT_OUTPUT_DPI) -> bytes:
    """以和剪贴板/PNG相同的 DPI 换算单页透明 PDF 的物理尺寸。"""
    dpi = _validated_dpi(dpi)
    w_pt, h_pt = (pixels * 72.0 / dpi for pixels in rgba_img.size)
    png = BytesIO()
    export_png(rgba_img, png, dpi=dpi)
    with fitz.open() as out:
        page = out.new_page(width=w_pt, height=h_pt)
        page.insert_image(fitz.Rect(0, 0, w_pt, h_pt), stream=png.getvalue())
        return out.tobytes()


def export_pdf(rgba_img: Image.Image, path: str, dpi=DEFAULT_OUTPUT_DPI) -> None:
    payload = watermark_pdf_bytes(rgba_img, dpi=dpi)
    with open(path, 'wb') as output:
        output.write(payload)


# ---------- CLI 入口 ----------

def _main() -> None:
    parser = argparse.ArgumentParser(description='从 PDF 扫描件中提取签名水印')
    parser.add_argument('input', help='输入 PDF 路径')
    parser.add_argument('-o', '--output', default='signature',
                        help='输出文件前缀 (不含扩展名), 默认 signature')
    parser.add_argument('--page', type=int, default=0)
    parser.add_argument('--dpi', type=int, default=300)

    # bbox: 不指定则自动检测; 指定则手动 (x0,y0,x1,y1)
    parser.add_argument('--bbox', type=str, default=None,
                        help='手动指定裁剪框 "x0,y0,x1,y1" (像素, 基于渲染后尺寸)')

    # 参数
    parser.add_argument('--threshold', type=int, default=140)
    parser.add_argument('--feather', type=int, default=0)
    parser.add_argument('--contrast', type=float, default=1.0)
    parser.add_argument('--opacity', type=float, default=1.0)
    parser.add_argument('--ink-density', type=float, default=1.0,
                        help='去背景后的墨迹浓度, 1-3 倍, 默认 1')
    parser.add_argument('--output-dpi', type=int, default=DEFAULT_OUTPUT_DPI,
                        help='输出物理尺寸使用的 DPI, 默认 300; 不改变像素')
    parser.add_argument('--color', choices=['black', 'white', 'original'], default='black')
    parser.add_argument('--no-clear-bg', action='store_true',
                        help='不强制清除近白背景')
    args = parser.parse_args()

    params = WatermarkParams(
        threshold=args.threshold,
        feather=args.feather,
        contrast=args.contrast,
        opacity=args.opacity,
        color_mode=args.color,
        clear_background=not args.no_clear_bg,
        output_dpi=args.output_dpi,
        ink_density=args.ink_density,
    )

    # 1. 加载
    page_img = load_pdf_page(args.input, dpi=args.dpi, page_index=args.page)
    print(f'[load] page rendered: {page_img.size}')

    # 2. 裁剪
    if args.bbox:
        x0, y0, x1, y1 = map(int, args.bbox.split(','))
    else:
        gray = np.asarray(page_img.convert('L')).astype(np.float32)
        if detect_signature_orientation(gray):
            page_img = page_img.rotate(90, expand=True)
            gray = np.asarray(page_img.convert('L')).astype(np.float32)
        x0, y0, x1, y1 = detect_ink_bbox(gray, padding=params.bbox_padding)
    print(f'[bbox] ({x0},{y0})-({x1},{y1}), size=({x1-x0},{y1-y0})')

    crop = page_img.crop((x0, y0, x1, y1))

    # 3. 渲染
    wm = render_watermark(crop, params)
    print(f'[render] watermark size: {wm.size}')

    # 4. 导出
    png_path = args.output + '.png'
    pdf_path = args.output + '.pdf'
    export_png(wm, png_path, dpi=params.output_dpi)
    export_pdf(wm, pdf_path, dpi=params.output_dpi)
    print(f'[done] -> {png_path}, {pdf_path}')


if __name__ == '__main__':
    _main()
