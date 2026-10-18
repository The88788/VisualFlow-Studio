"""
Watermark Remover — Reverse Alpha Blending (v3)
================================================
Убирает полупрозрачную вотермарку из видоса.

Стратегия:
  1. Темпоральная alpha-карта (percentile 80 по кадрам)
  2. Reverse alpha blending с boosted alpha
  3. Inpainting остатков (cv2.inpaint) — добивает то, что не убрал alpha-blend
  4. Color correction для выравнивания яркости

Зависимости:
  pip install opencv-python numpy tqdm

Использование:
  python watermark_remover.py --input video.mp4 --output clean.mp4
  python watermark_remover.py --input video.mp4 --output clean.mp4 --detect
  python watermark_remover.py --input video.mp4 --output clean.mp4 --alpha-boost 2.0 --inpaint-radius 5
"""

import cv2
import numpy as np
import argparse
import os
import sys
from tqdm import tqdm


# ─────────────────────────────────────────────
#  Константы по умолчанию (Veo вотермарка)
# ─────────────────────────────────────────────
DEFAULT_WATERMARK_COLOR = np.array([255, 255, 255], dtype=np.float32)

# Дефолтные проценты для Veo вотермарки (правый нижний угол)
DEFAULT_XPCT = 0.88
DEFAULT_YPCT = 0.90
DEFAULT_WPCT = 0.12
DEFAULT_HPCT = 0.10


class Config:
    """Все параметры в одном месте, легко менять через CLI."""
    alpha_threshold   = 0.02     # порог шума alpha (мягкий)
    alpha_boost       = 1.8      # boost alpha (↑ с 1.4 — агрессивнее)
    feather_radius    = 3        # размытие маски (px)
    dilate_radius     = 3        # дилатация маски (px) (↑ с 2)
    removal_passes    = 2        # проходов reverse alpha blend
    temporal_samples  = 120      # кадров для alpha-карты
    temporal_pctile   = 80       # перцентиль (↑ с 75 — ловим WM лучше)
    inpaint_radius    = 4        # радиус inpainting для добивания остатков
    inpaint_threshold = 0.05     # минимальная alpha для inpaint маски
    color_correct     = True     # коррекция яркости после удаления
    region_blend      = 20       # градиент по краям прямоугольника (px)


CFG = Config()


# ─────────────────────────────────────────────
#  Определение зоны вотермарки
# ─────────────────────────────────────────────
def get_bottom_right_region(width, height, x_pct=0.90, y_pct=0.88, w_pct=0.09, h_pct=0.07):
    x = int(width * x_pct)
    y = int(height * y_pct)
    w = min(int(width * w_pct), width - x)
    h = min(int(height * h_pct), height - y)
    return (x, y, w, h)


def detect_watermark_region(frame, threshold=220, min_area=500):
    """Ищет светлую зону в правой нижней четверти кадра."""
    h_full, w_full = frame.shape[:2]
    x_off = w_full // 2
    y_off = h_full // 2
    roi = frame[y_off:, x_off:]

    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    largest = max(contours, key=cv2.contourArea)
    if cv2.contourArea(largest) < min_area:
        return None

    rx, ry, rw, rh = cv2.boundingRect(largest)
    # Добавляем padding вокруг найденной зоны
    pad = 8
    x = max(0, rx + x_off - pad)
    y = max(0, ry + y_off - pad)
    w = min(rw + pad * 2, w_full - x)
    h = min(rh + pad * 2, h_full - y)
    print(f"  [detect] Вотермарка: x={x}, y={y}, w={w}, h={h}")
    return (x, y, w, h)


# ─────────────────────────────────────────────
#  Оценка фонового цвета из полосы снаружи
# ─────────────────────────────────────────────
def estimate_background(frame, region, margin=12):
    """Медиана пикселей из полосы вокруг региона WM."""
    x, y, w, h = region
    fh, fw = frame.shape[:2]
    samples = []

    if x >= margin:
        samples.append(frame[y:y+h, x-margin:x].reshape(-1, 3))
    if y >= margin:
        samples.append(frame[y-margin:y, x:x+w].reshape(-1, 3))
    if y + h + margin <= fh:
        samples.append(frame[y+h:y+h+margin, x:x+w].reshape(-1, 3))
    if x + w + margin <= fw:
        samples.append(frame[y:y+h, x+w:x+w+margin].reshape(-1, 3))

    if samples:
        return np.median(np.concatenate(samples, axis=0).astype(np.float32), axis=0)
    return np.array([0.0, 0.0, 0.0])


# ─────────────────────────────────────────────
#  Темпоральная alpha-карта
# ─────────────────────────────────────────────
def extract_alpha_map_temporal(input_path, region):
    """
    Анализирует N кадров видео, вычисляет excess = pixel - local_bg
    и берёт percentile(80) — устойчивее чем median, лучше ловит WM.
    """
    cap = cv2.VideoCapture(input_path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    x, y, w, h = region

    step = max(1, total // CFG.temporal_samples)
    print(f"  [temporal] Анализ ~{CFG.temporal_samples} кадров (шаг {step})...")

    roi_stack = []
    bg_stack  = []

    for i in range(0, total, step):
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ret, frame = cap.read()
        if not ret:
            continue

        roi_stack.append(frame[y:y+h, x:x+w].astype(np.float32))
        bg_stack.append(estimate_background(frame.astype(np.float32), region, margin=10))

    cap.release()

    if not roi_stack:
        return None, None

    roi_arr = np.stack(roi_stack)  # (N, h, w, 3)
    bg_arr  = np.stack(bg_stack)   # (N, 3)

    # excess = pixel - background (вклад вотермарки)
    excess = roi_arr - bg_arr[:, np.newaxis, np.newaxis, :]
    excess = np.clip(excess, 0, None)

    # Перцентиль по кадрам — 80-й стабильнее ловит WM
    pctile_excess = np.percentile(excess, CFG.temporal_pctile, axis=0)  # (h,w,3)
    median_bg = np.median(bg_arr, axis=0)  # (3,)

    # α = excess / (255 - bg)
    denom = np.clip(255.0 - median_bg, 1.0, None)
    alpha_per_ch = pctile_excess / denom
    alpha_map = np.max(alpha_per_ch, axis=2)
    alpha_map = np.clip(alpha_map, 0.0, 1.0)

    print(f"  [temporal] Кадров: {len(roi_stack)}, медиан bg (BGR): {median_bg.astype(int).tolist()}")
    print(f"  [temporal] Alpha raw: {alpha_map.min():.3f}–{alpha_map.max():.3f}, mean={alpha_map.mean():.3f}")

    return alpha_map.astype(np.float32), median_bg


# ─────────────────────────────────────────────
#  Постобработка alpha-map
# ─────────────────────────────────────────────
def postprocess_alpha_map(alpha_map):
    """Порог → дилатация → boost → feather."""
    amap = alpha_map.copy()

    # 1. Порог
    amap[amap < CFG.alpha_threshold] = 0.0
    n_wm = np.sum(amap > 0)
    print(f"  [postproc] Порог {CFG.alpha_threshold}: {n_wm}/{amap.size} px WM")

    # 2. Дилатация — расширяем зону WM (ореол вокруг букв)
    if CFG.dilate_radius > 0:
        binary = (amap > 0).astype(np.uint8)
        k = CFG.dilate_radius * 2 + 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        dilated = cv2.dilate(binary, kernel)

        new_px = (dilated > 0) & (amap == 0)
        if np.any(new_px):
            blur_k = CFG.dilate_radius * 4 + 1
            fill = cv2.GaussianBlur(amap, (blur_k, blur_k), 0)
            amap[new_px] = fill[new_px]
        print(f"  [postproc] Дилатация r={CFG.dilate_radius}: +{np.sum(new_px)} px")

    # 3. Boost
    amap = np.clip(amap * CFG.alpha_boost, 0.0, 1.0)
    print(f"  [postproc] Boost x{CFG.alpha_boost}: max={amap.max():.3f}")

    # 4. Feather
    if CFG.feather_radius > 0:
        k = CFG.feather_radius * 2 + 1
        amap = cv2.GaussianBlur(amap, (k, k), 0)

    return np.clip(amap, 0.0, 1.0)


# ─────────────────────────────────────────────
#  Удаление вотермарки (основной алгоритм)
# ─────────────────────────────────────────────
def remove_watermark_frame(frame, region, alpha_map):
    """
    Reverse alpha blend + inpainting остатков.

    Шаг 1: O = (R - α*255) / (1-α)  — убираем основное
    Шаг 2: cv2.inpaint() по маске WM — добиваем артефакты
    """
    MAX_ALPHA = 0.98
    x, y, w, h = region
    fh, fw = frame.shape[:2]
    result = frame.copy()

    # ── Шаг 1: Reverse alpha blending (N проходов) ──
    for p in range(CFG.removal_passes):
        result_f = result.astype(np.float32)
        roi = result_f[y:y+h, x:x+w]

        if p == 0:
            alpha = np.clip(alpha_map, 0.0, MAX_ALPHA)[:, :, np.newaxis]
        else:
            # Последующие проходы — остаточная alpha (убираем хвосты)
            alpha = np.clip(alpha_map * 0.35, 0.0, MAX_ALPHA)[:, :, np.newaxis]

        restored = (roi - alpha * 255.0) / (1.0 - alpha)
        restored = np.clip(restored, 0, 255)

        # Blend: где alpha > 0 используем restored, иначе оригинал
        blend = alpha_map[:, :, np.newaxis].clip(0, 1)
        result_f[y:y+h, x:x+w] = blend * restored + (1.0 - blend) * roi
        result = result_f.astype(np.uint8)

    # ── Шаг 2: Inpainting остатков ──
    if CFG.inpaint_radius > 0:
        # Маска = только ядро букв (без дилатации — точнее, меньше тёмных пятен)
        inpaint_mask_local = (alpha_map > CFG.inpaint_threshold).astype(np.uint8) * 255

        # Полноразмерная маска
        full_mask = np.zeros((fh, fw), dtype=np.uint8)
        full_mask[y:y+h, x:x+w] = inpaint_mask_local

        # Navier-Stokes — сохраняет яркость лучше чем Telea
        result = cv2.inpaint(result, full_mask, CFG.inpaint_radius, cv2.INPAINT_NS)

    # ── Шаг 3: Color correction ──
    if CFG.color_correct:
        result = color_correct_region(result, region)

    # ── Шаг 4: Region edge blending ──
    # Мягкий градиент по краям прямоугольника чтобы не было видно границу
    if CFG.region_blend > 0:
        m = CFG.region_blend
        processed_roi = result[y:y+h, x:x+w].astype(np.float32)
        original_roi  = frame[y:y+h, x:x+w].astype(np.float32)

        # Маска: 1.0 в центре, плавно → 0.0 к краям за m пикселей
        mask = np.ones((h, w), dtype=np.float32)

        for i in range(min(m, h // 2)):
            t = (i + 1) / (m + 1)   # 0→1 плавно
            t = t * t * (3 - 2 * t)  # smoothstep для плавности
            mask[i, :] = np.minimum(mask[i, :], t)       # верх
            mask[h-1-i, :] = np.minimum(mask[h-1-i, :], t)  # низ

        for i in range(min(m, w // 2)):
            t = (i + 1) / (m + 1)
            t = t * t * (3 - 2 * t)
            mask[:, i] = np.minimum(mask[:, i], t)       # лево
            mask[:, w-1-i] = np.minimum(mask[:, w-1-i], t)  # право

        mask3 = mask[:, :, np.newaxis]
        blended = mask3 * processed_roi + (1.0 - mask3) * original_roi
        result[y:y+h, x:x+w] = np.clip(blended, 0, 255).astype(np.uint8)

    return result


def remove_watermark_patch(frame, region):
    """Фоллбэк: копируем патч слева."""
    x, y, w, h = region
    result = frame.copy()
    src_x = max(0, x - w)
    patch = frame[y:y+h, src_x:src_x+w]
    if patch.shape[1] != w:
        src_x = x + w
        patch = frame[y:y+h, src_x:min(src_x+w, frame.shape[1])]
    result[y:y+h, x:x+w] = cv2.resize(patch, (w, h))
    return result


def color_correct_region(frame, region, margin=14):
    """Выравнивает яркость зоны WM с окружением."""
    x, y, w, h = region
    result = frame.copy()

    roi = result[y:y+h, x:x+w].astype(np.float32)
    surround_bg = estimate_background(frame.astype(np.float32), region, margin)
    roi_mean = roi.mean(axis=(0, 1))

    diff = surround_bg - roi_mean
    # Корректируем только если разница заметная (> 3 уровня)
    if np.max(np.abs(diff)) > 3.0:
        corrected = roi + diff * 0.5
        result[y:y+h, x:x+w] = np.clip(corrected, 0, 255).astype(np.uint8)

    return result


def compute_psnr(a, b):
    mse = np.mean((a.astype(np.float32) - b.astype(np.float32)) ** 2)
    return float('inf') if mse == 0 else 20 * np.log10(255.0 / np.sqrt(mse))


# ─────────────────────────────────────────────
#  Основной процессинг
# ─────────────────────────────────────────────
def process_video(input_path, output_path, region=None, auto_detect=False, pct=None):
    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        print(f"[ERROR] Не могу открыть: {input_path}")
        sys.exit(1)

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps    = cap.get(cv2.CAP_PROP_FPS)
    width  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    print(f"\n{'='*60}")
    print(f"  Файл       : {input_path}")
    print(f"  Размер     : {width}x{height}, {fps:.1f} fps, {total_frames} кадров")
    print(f"  Boost      : x{CFG.alpha_boost}")
    print(f"  Passes     : {CFG.removal_passes}")
    print(f"  Inpaint    : r={CFG.inpaint_radius}")
    print(f"  Feather    : {CFG.feather_radius}px")
    print(f"  Dilate     : {CFG.dilate_radius}px")
    print(f"  Percentile : {CFG.temporal_pctile}")
    print(f"  Reg.blend  : {CFG.region_blend}px")
    print(f"{'='*60}\n")

    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))

    detected_region = region

    # Проценты → пиксели
    if region is None and not auto_detect and pct is not None:
        detected_region = get_bottom_right_region(width, height, *pct)

    # Дефолт
    if detected_region is None and not auto_detect:
        detected_region = get_bottom_right_region(width, height)

    if detected_region:
        x, y, w, h = detected_region
        print(f"  Регион WM: x={x}, y={y}, w={w}, h={h}")

    # ── Alpha-map ──
    alpha_map = None
    if detected_region and not auto_detect:
        alpha_map, _ = extract_alpha_map_temporal(input_path, detected_region)
        if alpha_map is not None:
            alpha_map = postprocess_alpha_map(alpha_map)

            # Debug визуализации
            cv2.imwrite("debug_alpha_map.png", (alpha_map * 255).astype(np.uint8))
            cv2.imwrite("debug_alpha_map_color.png",
                        cv2.applyColorMap((alpha_map * 255).astype(np.uint8), cv2.COLORMAP_JET))
            print(f"  Alpha-map: debug_alpha_map.png / debug_alpha_map_color.png")

    # ── Обработка кадров ──
    psnr_values = []

    for i in tqdm(range(total_frames), desc="Обработка"):
        ret, frame = cap.read()
        if not ret:
            break

        # Авто-детект на первом кадре
        if auto_detect and i == 0:
            detected = detect_watermark_region(frame)
            detected_region = detected or get_bottom_right_region(width, height)
            x, y, w, h = detected_region
            print(f"  Регион WM: x={x}, y={y}, w={w}, h={h}")

            alpha_map, _ = extract_alpha_map_temporal(input_path, detected_region)
            if alpha_map is not None:
                alpha_map = postprocess_alpha_map(alpha_map)

        if detected_region and alpha_map is not None:
            original = frame.copy()
            processed = remove_watermark_frame(frame, detected_region, alpha_map)

            if i < 10:
                x, y, w, h = detected_region
                psnr_values.append(compute_psnr(original[y:y+h, x:x+w], processed[y:y+h, x:x+w]))
        elif detected_region:
            processed = remove_watermark_patch(frame, detected_region)
        else:
            processed = frame

        out.write(processed)

    cap.release()
    out.release()

    print(f"\n{'='*60}")
    print(f"  Готово: {output_path}")
    if psnr_values:
        avg = np.mean(psnr_values)
        print(f"  PSNR (зона WM): {avg:.1f} dB", end="")
        print(" ✓" if avg > 30 else (" ~" if avg > 20 else " ✗"))
    print(f"{'='*60}\n")


# ─────────────────────────────────────────────
#  Unit test
# ─────────────────────────────────────────────
def run_unit_test():
    print("\n[TEST] Синтетический тест...")
    ALPHA = 0.5
    WM = np.array([255, 255, 255], dtype=np.float32)

    np.random.seed(42)
    orig = np.random.randint(50, 200, (100, 100, 3), dtype=np.uint8).astype(np.float32)
    wm_frame = (ALPHA * WM + (1 - ALPHA) * orig).clip(0, 255).astype(np.uint8)

    result = wm_frame.astype(np.float32)
    roi = result[0:100, 0:100]
    restored = (roi - ALPHA * WM) / (1.0 - ALPHA)
    restored = np.clip(restored, 0, 255)
    result[0:100, 0:100] = restored
    result = result.astype(np.uint8)

    diff = np.abs(orig - result.astype(np.float32))
    print(f"  Max err: {diff.max():.4f}, Mean err: {diff.mean():.4f}")
    print(f"  {'✓ PASSED' if diff.max() < 2.0 else '✗ FAILED'}\n")


# ─────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description="Watermark Remover v3")

    p.add_argument("--input",  "-i", type=str, help="Входное видео")
    p.add_argument("--output", "-o", type=str, default="output_clean.mp4")

    # Регион
    p.add_argument("--x", type=int, default=None)
    p.add_argument("--y", type=int, default=None)
    p.add_argument("--w", type=int, default=None)
    p.add_argument("--h", type=int, default=None)
    p.add_argument("--xpct", type=float, default=DEFAULT_XPCT)
    p.add_argument("--ypct", type=float, default=DEFAULT_YPCT)
    p.add_argument("--wpct", type=float, default=DEFAULT_WPCT)
    p.add_argument("--hpct", type=float, default=DEFAULT_HPCT)

    # Параметры удаления
    p.add_argument("--alpha-boost", type=float, default=CFG.alpha_boost,
                   help=f"Множитель alpha (default: {CFG.alpha_boost})")
    p.add_argument("--alpha-threshold", type=float, default=CFG.alpha_threshold)
    p.add_argument("--feather", type=int, default=CFG.feather_radius)
    p.add_argument("--dilate", type=int, default=CFG.dilate_radius)
    p.add_argument("--passes", type=int, default=CFG.removal_passes)
    p.add_argument("--inpaint-radius", type=int, default=CFG.inpaint_radius,
                   help=f"Радиус inpainting (0=выкл, default: {CFG.inpaint_radius})")
    p.add_argument("--temporal-pctile", type=int, default=CFG.temporal_pctile)
    p.add_argument("--no-color-correct", action="store_true")
    p.add_argument("--region-blend", type=int, default=CFG.region_blend,
                   help=f"Градиент по краям зоны обработки, px (default: {CFG.region_blend})")

    p.add_argument("--detect", action="store_true")
    p.add_argument("--test",   action="store_true")

    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.test:
        run_unit_test()
        sys.exit(0)

    if not args.input:
        print("[ERROR] --input video.mp4  или  --test")
        sys.exit(1)

    if not os.path.exists(args.input):
        print(f"[ERROR] Файл не найден: {args.input}")
        sys.exit(1)

    # Применяем CLI → Config
    CFG.alpha_boost      = args.alpha_boost
    CFG.alpha_threshold  = args.alpha_threshold
    CFG.feather_radius   = args.feather
    CFG.dilate_radius    = args.dilate
    CFG.removal_passes   = args.passes
    CFG.inpaint_radius   = args.inpaint_radius
    CFG.temporal_pctile  = args.temporal_pctile
    CFG.color_correct    = not args.no_color_correct
    CFG.region_blend     = args.region_blend

    # Регион
    if args.detect:
        region, auto, pct = None, True, None
    elif all(v is not None for v in [args.x, args.y, args.w, args.h]):
        region, auto, pct = (args.x, args.y, args.w, args.h), False, None
    else:
        region, auto = None, False
        pct = (args.xpct, args.ypct, args.wpct, args.hpct)
        print(f"[info] Режим: % координаты "
              f"(x={args.xpct*100:.0f}% y={args.ypct*100:.0f}% "
              f"w={args.wpct*100:.0f}% h={args.hpct*100:.0f}%)")

    process_video(args.input, args.output, region, auto, pct)