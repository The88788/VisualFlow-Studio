"""Quick local test for watermark detection.
Usage:
    python test_wm_detect.py video.mp4
    python test_wm_detect.py video.mp4 --verbose
"""
import sys
import argparse
from pathlib import Path

# ── Same constants as watermark_service.py ────────────────────────────────────
WM_XPCT = 0.88;  WM_YPCT = 0.90
WM_WPCT = 0.12;  WM_HPCT = 0.10
EXCESS_THRESHOLD = 15   # ROI pixels must be brighter than bg_mean by this much
EXCESS_PX_MIN    = 30   # minimum such pixels per frame
ALPHA_THRESHOLD = 0.05
TEMPORAL_PCTILE = 80
TEMPORAL_SAMPLES = 30
ALPHA_BOOST = 1.8
DILATE_RADIUS = 3
FEATHER_RADIUS = 20


def detect(video_path: Path, verbose=False):
    import cv2, numpy as np

    cap = cv2.VideoCapture(str(video_path))
    total  = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps    = cap.get(cv2.CAP_PROP_FPS)
    print(f"\nФайл  : {video_path.name}")
    print(f"Размер: {width}x{height}, {fps:.1f} fps, {total} кадров")

    x = int(width * WM_XPCT);  w = min(int(width  * WM_WPCT), width  - x)
    y = int(height * WM_YPCT); h = min(int(height * WM_HPCT), height - y)
    print(f"WM зона: x={x} y={y} w={w} h={h}  ({w*h} px)")

    margin = 10
    n_check = min(5, total)
    step = max(1, total // n_check)
    detections = 0

    for idx in range(n_check):
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx * step)
        ret, frame = cap.read()
        if not ret:
            continue

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        roi  = gray[y:y+h, x:x+w].astype(np.float32)

        border = []
        if x >= margin:          border.append(gray[y:y+h,    x-margin:x       ].ravel())
        if y >= margin:          border.append(gray[y-margin:y, x:x+w           ].ravel())
        if y+h+margin <= height: border.append(gray[y+h:y+h+margin, x:x+w      ].ravel())
        if x+w+margin <= width:  border.append(gray[y:y+h,    x+w:x+w+margin    ].ravel())

        bg_mean   = float(np.median(np.concatenate(border))) if border else 0.0
        excess_px = int(np.sum(roi > bg_mean + EXCESS_THRESHOLD))
        found     = excess_px >= EXCESS_PX_MIN
        if found:
            detections += 1

        print(f"  кадр {idx*step:4d}: roi_mean={roi.mean():5.1f}  bg_mean={bg_mean:5.1f}  "
              f"excess(>{EXCESS_THRESHOLD})={excess_px:5d}  {'← ДЕТЕКТ' if found else ''}")

    cap.release()

    detected = detections >= 2
    print(f"\nДетект (relative excess): {'✓ ВОТЕРМАРКА НАЙДЕНА' if detected else '✗ не найдена'}  ({detections}/{n_check} кадров)")

    # Сохраняем кроп зоны из среднего кадра для визуальной проверки
    cap2 = cv2.VideoCapture(str(video_path))
    cap2.set(cv2.CAP_PROP_POS_FRAMES, total // 2)
    ret, frame = cap2.read()
    cap2.release()
    if ret:
        crop = frame[y:y+h, x:x+w]
        # Сохраняем с 4x увеличением чтобы было видно
        crop_big = cv2.resize(crop, (w*4, h*4), interpolation=cv2.INTER_NEAREST)
        cv2.imwrite("debug_wm_crop.png", crop_big)
        # Весь кадр с прямоугольником вокруг зоны
        frame_marked = frame.copy()
        cv2.rectangle(frame_marked, (x, y), (x+w, y+h), (0, 0, 255), 2)
        cv2.imwrite("debug_wm_frame.png", frame_marked)
        print(f"\n  Сохранено: debug_wm_crop.png (зона x4)  debug_wm_frame.png (весь кадр)")

    return detected, (x, y, w, h), (width, height, total)


def alpha_check(video_path: Path, region, verbose=False):
    import cv2, numpy as np

    x, y, w, h = region
    margin = 10

    cap   = cv2.VideoCapture(str(video_path))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fh    = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fw    = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    step  = max(1, total // TEMPORAL_SAMPLES)

    roi_stack, bg_stack = [], []
    for i in range(0, total, step):
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ret, frame = cap.read()
        if not ret:
            continue
        roi_stack.append(frame[y:y+h, x:x+w].astype(np.float32))
        samples = []
        if x >= margin:           samples.append(frame[y:y+h,    x-margin:x      ].reshape(-1, 3))
        if y >= margin:           samples.append(frame[y-margin:y, x:x+w         ].reshape(-1, 3))
        if y+h+margin <= fh:      samples.append(frame[y+h:y+h+margin, x:x+w    ].reshape(-1, 3))
        if x+w+margin <= fw:      samples.append(frame[y:y+h,    x+w:x+w+margin  ].reshape(-1, 3))
        bg = np.median(np.concatenate(samples).astype(np.float32), axis=0) if samples else np.zeros(3)
        bg_stack.append(bg)
    cap.release()

    roi_arr  = np.stack(roi_stack)
    bg_arr   = np.stack(bg_stack)
    excess   = np.clip(roi_arr - bg_arr[:, np.newaxis, np.newaxis, :], 0, None)
    pctile   = np.percentile(excess, TEMPORAL_PCTILE, axis=0)
    med_bg   = np.median(bg_arr, axis=0)
    denom    = np.clip(255.0 - med_bg, 1.0, None)
    amap     = np.clip(np.max(pctile / denom, axis=2), 0.0, 1.0).astype(np.float32)

    # postprocess: threshold → dilate → boost → feather
    amap[amap < ALPHA_THRESHOLD] = 0.0
    if DILATE_RADIUS > 0:
        binary  = (amap > 0).astype(np.uint8)
        k       = DILATE_RADIUS * 2 + 1
        kernel  = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        dilated = cv2.dilate(binary, kernel)
        new_px  = (dilated > 0) & (amap == 0)
        if np.any(new_px):
            fill = cv2.GaussianBlur(amap, (DILATE_RADIUS*4+1, DILATE_RADIUS*4+1), 0)
            amap[new_px] = fill[new_px]
    amap = np.clip(amap * ALPHA_BOOST, 0.0, 1.0)
    if FEATHER_RADIUS > 0:
        amap = cv2.GaussianBlur(amap, (FEATHER_RADIUS*2+1, FEATHER_RADIUS*2+1), 0)
    amap = np.clip(amap, 0.0, 1.0)

    wm_px = int(np.sum(amap > ALPHA_THRESHOLD))
    print(f"\nAlpha-map ({len(roi_stack)} кадров):")
    print(f"  min={amap.min():.3f}  max={amap.max():.3f}  mean={amap.mean():.4f}")
    print(f"  WM пикселей: {wm_px} из {amap.size}  ({wm_px/amap.size*100:.1f}%)")
    print(f"  Фон (BGR медиана): {med_bg.astype(int).tolist()}")

    if wm_px >= 10:
        print(f"\n  → Удаление будет запущено ✓")
    else:
        print(f"\n  → Пикселей слишком мало (<10), удаление будет пропущено")

    if verbose:
        # Save debug images to current dir
        cv2.imwrite("debug_amap.png", (amap * 255).astype("uint8"))
        print(f"  Сохранено: debug_amap.png")

    return wm_px


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("video", help="Путь к видео файлу")
    ap.add_argument("-v", "--verbose", action="store_true", help="Подробный вывод + debug_amap.png")
    args = ap.parse_args()

    path = Path(args.video)
    if not path.exists():
        print(f"Файл не найден: {path}")
        sys.exit(1)

    try:
        import cv2, numpy
    except ImportError:
        print("Установи зависимости: pip install opencv-python numpy")
        sys.exit(1)

    detected, region, info = detect(path, verbose=args.verbose)

    if detected:
        alpha_check(path, region, verbose=args.verbose)
    else:
        print("\n  Вотермарка не обнаружена — обработка не нужна")
        if args.verbose:
            print("  Если ты уверен что она есть — добавь -v и проверь числа яркости")
