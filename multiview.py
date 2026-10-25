"""Анализ серии снимков: одна сцена с разных ракурсов или один человек в разных сценах.

Вызывается только из multiview_worker.py (изолированный процесс с лимитами). Все пороги — стартовые
значения для MVP; калибровать их нужно на evaluation-наборе серий, а не на отдельных примерах.
"""
from __future__ import annotations

import base64
import io
import math
import re
from datetime import datetime, timedelta
from itertools import combinations
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import ExifTags, Image, ImageOps

ROOT = Path(__file__).resolve().parent
YUNET_PATH = ROOT / "models" / "face_detection_yunet_2023mar.onnx"
YUNET_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
SFACE_PATH = ROOT / "models" / "face_recognition_sface_2021dec.onnx"
SFACE_SHA256 = "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"

ANALYSIS_EDGE = 1024
SIFT_FEATURES = 4000
VIS_HEIGHT = 300
GRID = 16

# Стартовые пороги, см. AGENTS.md раздел 7.
MIN_MATCHES = 15
SHARED_CONTENT_MATCHES = 40
CONSISTENT_INLIERS = 30
CONSISTENT_RATIO = 0.35
CONSISTENT_COVERAGE = 0.12
PLANAR_SHARE = 0.85
INCONSISTENT_RATIO = 0.25
NEAR_DUPLICATE_BASELINE = 0.01
SAME_PERSON_COSINE = 0.363  # рекомендованный порог SFace из документации OpenCV
DIFFERENT_PERSON_COSINE = 0.25
PASTED_FACE_SSIM = 0.85
PASTED_FACE_POSE_DEG = 4.0
MIN_FACE_PX = 60


class SeriesImage:
    def __init__(self, index: int, name: str, raw: bytes) -> None:
        image = Image.open(io.BytesIO(raw))
        self.exif = _exif_fields(image)
        rotated = image.getexif().get(274, 1) in (5, 6, 7, 8)  # EXIF Orientation с поворотом на 90°
        self.original_size = (image.height, image.width) if rotated else image.size
        # JPEG декодируется сразу в уменьшенном масштабе: 40-мегапиксельный кадр не разворачивается целиком.
        image.draft("RGB", (ANALYSIS_EDGE * 2, ANALYSIS_EDGE * 2))
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((ANALYSIS_EDGE, ANALYSIS_EDGE), Image.Resampling.LANCZOS)
        self.scale = image.width / self.original_size[0]  # анализ → координаты исходного (повёрнутого) снимка
        self.index = index
        self.name = name
        self.rgb = np.asarray(image)
        self.bgr = cv2.cvtColor(self.rgb, cv2.COLOR_RGB2BGR)
        gray = cv2.cvtColor(self.rgb, cv2.COLOR_RGB2GRAY)
        self.gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
        self.keypoints: list[cv2.KeyPoint] = []
        self.descriptors: np.ndarray | None = None

    @property
    def size(self) -> tuple[int, int]:
        return self.rgb.shape[1], self.rgb.shape[0]


# ---------------------------------------------------------------- EXIF ---

def _rational(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _gps(gps: dict[int, Any]) -> tuple[float, float] | None:
    try:
        def degrees(values: Any, ref: str) -> float:
            d, m, s = (float(v) for v in values)
            sign = -1 if ref in ("S", "W") else 1
            return sign * (d + m / 60 + s / 3600)
        return degrees(gps[2], gps[1]), degrees(gps[4], gps[3])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def _exif_fields(image: Image.Image) -> dict[str, Any]:
    exif = image.getexif()
    sub = exif.get_ifd(ExifTags.IFD.Exif)
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    taken = None
    raw_time = sub.get(36867) or exif.get(306)
    if raw_time:
        try:
            taken = datetime.strptime(str(raw_time).strip("\x00 "), "%Y:%m:%d %H:%M:%S")
            subsec = str(sub.get(37521) or "").strip("\x00 ")
            if subsec.isdigit():
                taken += timedelta(seconds=int(subsec) / (10 ** len(subsec)))
        except ValueError:
            taken = None
    return {
        "make": str(exif.get(271) or "").strip("\x00 ") or None,
        "model": str(exif.get(272) or "").strip("\x00 ") or None,
        "lens": str(sub.get(42036) or "").strip("\x00 ") or None,
        "software": str(exif.get(305) or "").strip("\x00 ") or None,
        "taken": taken,
        "offset": str(sub.get(36881) or "").strip("\x00 ") or None,
        "focal_35mm": _rational(sub.get(41989)),
        "gps": _gps(gps) if gps else None,
    }


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def _file_number(name: str) -> int | None:
    match = re.search(r"(\d{3,})(?!.*\d)", Path(name).stem)
    return int(match.group(1)) if match else None


def exif_series(images: list[SeriesImage]) -> dict[str, Any]:
    """Согласованность метаданных серии. Отсутствие EXIF нейтрально: мессенджеры его удаляют."""
    reasons: list[dict[str, Any]] = []
    with_exif = [image for image in images if image.exif["model"] or image.exif["taken"]]
    devices = sorted({f"{image.exif['make'] or ''} {image.exif['model'] or ''}".strip() for image in with_exif if image.exif["model"]})
    times = sorted(image.exif["taken"] for image in images if image.exif["taken"])
    span = (times[-1] - times[0]).total_seconds() if len(times) >= 2 else None
    points = [image.exif["gps"] for image in images if image.exif["gps"]]
    distance = max((_haversine_m(a, b) for a, b in combinations(points, 2)), default=None) if len(points) >= 2 else None
    numbers = [_file_number(image.name) for image in images]
    numbers_known = [number for number in numbers if number is not None]
    sequential = len(numbers_known) == len(images) and len(images) >= 2 and max(numbers_known) - min(numbers_known) <= 10 * len(images)
    software = sorted({image.exif["software"] for image in images if image.exif["software"]})

    if not with_exif:
        reasons.append(_reason("info", 0, "Метаданные камеры отсутствуют", "Это нормально для фото из мессенджеров и ничего не доказывает."))
    else:
        if len(with_exif) < len(images):
            reasons.append(_reason("info", 0, "EXIF есть не у всех снимков", f"Метаданные найдены у {len(with_exif)} из {len(images)}."))
        if len(devices) == 1 and len(with_exif) >= 2:
            reasons.append(_reason("weak", 1, "Одна камера", f"Все снимки с EXIF сделаны на {devices[0]}."))
        elif len(devices) > 1:
            reasons.append(_reason("weak", -1, "Разные устройства", "В серии разные камеры: " + ", ".join(devices[:4])))
        if span is not None:
            if span <= 30 * 60:
                reasons.append(_reason("weak", 1, "Снято подряд", f"Интервал между первым и последним кадром: {_duration(span)}."))
            elif span > 2 * 24 * 3600:
                reasons.append(_reason("info", 0, "Снимки разнесены во времени", f"Разброс по времени съёмки: {_duration(span)}."))
    if distance is not None:
        if distance <= 200:
            reasons.append(_reason("weak", 1, "Одно место по GPS", f"Максимальное расстояние между точками съёмки: {distance:.0f} м."))
        elif distance > 5000:
            reasons.append(_reason("info", 0, "Разные места по GPS", f"Точки съёмки разнесены на {distance / 1000:.1f} км."))
    if sequential:
        reasons.append(_reason("info", 1, "Номера файлов подряд", "Имена файлов похожи на последовательные кадры одной камеры."))
    return {
        "with_exif": len(with_exif),
        "devices": devices,
        "time_span_s": round(span, 1) if span is not None else None,
        "gps_max_distance_m": round(distance) if distance is not None else None,
        "sequential_names": sequential,
        "software": software,
        "reasons": reasons,
    }


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} с"
    if seconds < 5400:
        return f"{seconds / 60:.0f} мин"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} ч"
    return f"{seconds / 86400:.0f} дн"


# --------------------------------------------------------------- Scene ---

def detect_features(images: list[SeriesImage], exclude: dict[int, list[int]] | None = None) -> None:
    """exclude: индекс снимка → бокс лица (x, y, w, h в координатах анализа); область лица не участвует в сравнении фона."""
    sift = cv2.SIFT_create(nfeatures=SIFT_FEATURES)
    for image in images:
        mask = None
        box = (exclude or {}).get(image.index)
        if box:
            x, y, w, h = box
            mask = np.full(image.gray.shape, 255, dtype=np.uint8)
            cv2.rectangle(mask, (int(x - 0.4 * w), int(y - 0.6 * h)), (int(x + 1.4 * w), int(y + 1.5 * h)), 0, -1)
        image.keypoints, image.descriptors = sift.detectAndCompute(image.gray, mask)


def _mutual_ratio_matches(a: np.ndarray | None, b: np.ndarray | None) -> list[tuple[int, int]]:
    if a is None or b is None or len(a) < 2 or len(b) < 2:
        return []
    matcher = cv2.BFMatcher(cv2.NORM_L2)

    def one_way(x: np.ndarray, y: np.ndarray) -> dict[int, int]:
        result = {}
        for pair in matcher.knnMatch(x, y, k=2):
            if len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance:
                result[pair[0].queryIdx] = pair[0].trainIdx
        return result

    forward = one_way(a, b)
    backward = one_way(b, a)
    return [(i, j) for i, j in forward.items() if backward.get(j) == i]


def _hull_coverage(points: np.ndarray, size: tuple[int, int]) -> float:
    if len(points) < 3:
        return 0.0
    hull = cv2.convexHull(points.astype(np.float32))
    return float(cv2.contourArea(hull) / (size[0] * size[1]))


def _focal_px(image: SeriesImage) -> tuple[float, bool]:
    width, height = image.size
    diagonal = math.hypot(width, height)
    focal_35 = image.exif["focal_35mm"]
    if focal_35 and 8 <= focal_35 <= 400:
        return focal_35 * diagonal / 43.27, False
    return 28 * diagonal / 43.27, True  # типичный широкоугольный объектив телефона


def _cheirality(a: SeriesImage, b: SeriesImage, p1: np.ndarray, p2: np.ndarray) -> dict[str, Any] | None:
    """Доля точек, которые после восстановления позы лежат перед обеими камерами."""
    if len(p1) < 15:
        return None
    (fa, assumed_a), (fb, assumed_b) = _focal_px(a), _focal_px(b)
    ka = np.array([[fa, 0, a.size[0] / 2], [0, fa, a.size[1] / 2], [0, 0, 1]])
    kb = np.array([[fb, 0, b.size[0] / 2], [0, fb, b.size[1] / 2], [0, 0, 1]])
    n1 = cv2.undistortPoints(p1.reshape(-1, 1, 2), ka, None)
    n2 = cv2.undistortPoints(p2.reshape(-1, 1, 2), kb, None)
    threshold = 1.5 / ((fa + fb) / 2)
    essential, mask = cv2.findEssentialMat(n1, n2, np.eye(3), method=cv2.RANSAC, prob=0.999, threshold=threshold)
    if essential is None or essential.shape != (3, 3) or mask is None:
        return None
    inliers = int(mask.sum())
    if inliers < 10:
        return None
    passed, _, _, pose_mask = cv2.recoverPose(essential, n1, n2, np.eye(3), mask=mask.copy())
    return {
        "ratio": round(passed / inliers, 3),
        "points": inliers,
        "focal_assumed": assumed_a or assumed_b,
    }


def _inconsistency_grid(a: SeriesImage, inlier_points: np.ndarray) -> tuple[np.ndarray, float | None]:
    """Ячейки с текстурой внутри зоны перекрытия, где нет ни одного геометрически согласованного совпадения."""
    width, height = a.size
    cell_w, cell_h = width / GRID, height / GRID
    textured = np.zeros((GRID, GRID), dtype=int)
    for keypoint in a.keypoints:
        x, y = keypoint.pt
        textured[min(GRID - 1, int(y / cell_h)), min(GRID - 1, int(x / cell_w))] += 1
    supported = np.zeros((GRID, GRID), dtype=int)
    for x, y in inlier_points:
        supported[min(GRID - 1, int(y / cell_h)), min(GRID - 1, int(x / cell_w))] += 1
    inside = np.zeros((GRID, GRID), dtype=bool)
    if len(inlier_points) >= 3:
        hull = cv2.convexHull(inlier_points.astype(np.float32))
        for row in range(GRID):
            for col in range(GRID):
                center = ((col + 0.5) * cell_w, (row + 0.5) * cell_h)
                inside[row, col] = cv2.pointPolygonTest(hull, center, True) >= -0.5 * max(cell_w, cell_h)
    candidates = inside & (textured >= 4)
    suspicious = candidates & (supported == 0)
    share = float(suspicious.sum() / candidates.sum()) if candidates.sum() >= 8 else None
    return suspicious, share


def _jpeg_data_url(rgb: np.ndarray, quality: int = 78) -> str:
    buffer = io.BytesIO()
    Image.fromarray(rgb).save(buffer, "JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def _pair_visual(a: SeriesImage, b: SeriesImage, p1: np.ndarray, p2: np.ndarray, inlier_mask: np.ndarray, suspicious: np.ndarray | None) -> str:
    scale_a = VIS_HEIGHT / a.size[1]
    scale_b = VIS_HEIGHT / b.size[1]
    left = cv2.resize(a.rgb, (max(1, round(a.size[0] * scale_a)), VIS_HEIGHT), interpolation=cv2.INTER_AREA)
    right = cv2.resize(b.rgb, (max(1, round(b.size[0] * scale_b)), VIS_HEIGHT), interpolation=cv2.INTER_AREA)
    if suspicious is not None and suspicious.any():
        overlay = left.copy()
        cell_w, cell_h = left.shape[1] / GRID, left.shape[0] / GRID
        for row, col in zip(*np.nonzero(suspicious)):
            cv2.rectangle(overlay, (int(col * cell_w), int(row * cell_h)), (int((col + 1) * cell_w), int((row + 1) * cell_h)), (255, 70, 50), -1)
        left = cv2.addWeighted(overlay, 0.42, left, 0.58, 0)
    gap = np.full((VIS_HEIGHT, 8, 3), 255, dtype=np.uint8)
    canvas = np.ascontiguousarray(np.hstack([left, gap, right]))
    offset = left.shape[1] + 8
    rng = np.random.default_rng(0)
    inliers = np.flatnonzero(inlier_mask)
    outliers = np.flatnonzero(~inlier_mask)
    for indices, color, limit in ((outliers, (255, 60, 40), 40), (inliers, (40, 210, 90), 90)):
        if len(indices) > limit:
            indices = rng.choice(indices, limit, replace=False)
        for index in indices:
            x1, y1 = p1[index] * scale_a
            x2, y2 = p2[index] * scale_b
            cv2.line(canvas, (int(x1), int(y1)), (int(x2 + offset), int(y2)), color, 1, cv2.LINE_AA)
            cv2.circle(canvas, (int(x1), int(y1)), 2, color, -1, cv2.LINE_AA)
            cv2.circle(canvas, (int(x2 + offset), int(y2)), 2, color, -1, cv2.LINE_AA)
    return _jpeg_data_url(canvas)


def analyze_pair(a: SeriesImage, b: SeriesImage, visual: bool = True) -> dict[str, Any]:
    matches = _mutual_ratio_matches(a.descriptors, b.descriptors)
    result: dict[str, Any] = {"a": a.index, "b": b.index, "matches": len(matches)}
    if len(matches) < 8:
        result.update({"status": "no_overlap", "inliers": 0, "detail": "Общих деталей не найдено — снимки, вероятно, разных сцен."})
        return result
    p1 = np.float32([a.keypoints[i].pt for i, _ in matches])
    p2 = np.float32([b.keypoints[j].pt for _, j in matches])
    diagonal = math.hypot(*a.size)
    fundamental, f_mask = cv2.findFundamentalMat(p1, p2, cv2.USAC_MAGSAC, 1.5, 0.999, 10000)
    homography, h_mask = cv2.findHomography(p1, p2, cv2.USAC_MAGSAC, 3.0, maxIters=10000, confidence=0.999)
    f_inliers = f_mask.ravel().astype(bool) if f_mask is not None and fundamental is not None and fundamental.shape == (3, 3) else np.zeros(len(matches), bool)
    inlier_count = int(f_inliers.sum())

    h_share = None
    parallax = None
    if homography is not None and inlier_count:
        projected = cv2.perspectiveTransform(p1[f_inliers].reshape(-1, 1, 2), homography).reshape(-1, 2)
        residual = np.linalg.norm(projected - p2[f_inliers], axis=1)
        h_share = float(np.mean(residual < 3.0))
        parallax = float(np.median(residual) / diagonal)
    displacement = float(np.median(np.linalg.norm(p1 - p2, axis=1)) / diagonal)
    coverage = min(_hull_coverage(p1[f_inliers], a.size), _hull_coverage(p2[f_inliers], b.size)) if inlier_count else 0.0
    ratio = inlier_count / len(matches)
    cheirality = _cheirality(a, b, p1[f_inliers], p2[f_inliers]) if inlier_count >= 15 and (h_share or 0) < PLANAR_SHARE else None
    suspicious, suspicious_share = _inconsistency_grid(a, p1[f_inliers]) if inlier_count >= 8 else (None, None)

    if displacement < NEAR_DUPLICATE_BASELINE and (h_share or 0) >= 0.95 and coverage >= 0.5:
        status, detail = "near_duplicate", "Кадры почти одинаковые: ракурс не менялся, геометрическая проверка неинформативна."
    elif inlier_count >= CONSISTENT_INLIERS and (h_share or 0) >= PLANAR_SHARE:
        status, detail = "planar", "Совпадения объясняются плоской сценой или поворотом на месте. Возможна съёмка экрана или распечатки."
    elif (
        inlier_count >= CONSISTENT_INLIERS and ratio >= CONSISTENT_RATIO and coverage >= CONSISTENT_COVERAGE
        and (cheirality is None or cheirality["ratio"] >= 0.9)
    ):
        status, detail = "consistent_3d", f"Геометрия сходится как у реальной 3D-сцены: {inlier_count} согласованных точек из {len(matches)}."
    elif len(matches) >= SHARED_CONTENT_MATCHES and ratio < INCONSISTENT_RATIO:
        status, detail = "inconsistent", f"Похожих деталей много ({len(matches)}), но с перспективой согласуются только {inlier_count}. Так бывает у сгенерированных «других ракурсов» и монтажа."
    elif cheirality is not None and cheirality["ratio"] < 0.75:
        status, detail = "inconsistent", f"Восстановленные точки сцены оказались за камерой ({cheirality['ratio']:.0%} корректных). Физически так снять нельзя."
    elif inlier_count < MIN_MATCHES:
        # Через малое число точек фундаментальная матрица проходит почти всегда — это шум, а не совпадение.
        status, detail = "no_overlap", "Слишком мало согласованных общих деталей для вывода."
    else:
        status, detail = "weak", f"Частичное совпадение: {inlier_count} согласованных точек из {len(matches)}. Нужен другой ракурс с большим перекрытием."

    result.update({
        "status": status,
        "detail": detail,
        "inliers": inlier_count,
        "inlier_ratio": round(ratio, 3),
        "coverage": round(coverage, 3),
        "planar_share": round(h_share, 3) if h_share is not None else None,
        "parallax": round(parallax, 4) if parallax is not None else None,
        "baseline": round(displacement, 4),
        "cheirality": cheirality,
        "suspicious_share": round(suspicious_share, 3) if suspicious_share is not None else None,
    })
    if visual and status != "no_overlap":
        result["visual"] = _pair_visual(a, b, p1, p2, f_inliers, suspicious if status in {"consistent_3d", "weak", "inconsistent"} else None)
    return result


# --------------------------------------------------------------- Faces ---

# Обобщённая 3D-модель лица (мм) для 5 точек YuNet: правый глаз, левый глаз, нос, правый и левый угол рта.
FACE_MODEL = np.array([[-31, 33, -28], [31, 33, -28], [0, 0, 0], [-24, -32, -24], [24, -32, -24]], dtype=np.float64)


def _ssim(a: np.ndarray, b: np.ndarray) -> float:
    a = a.astype(np.float64)
    b = b.astype(np.float64)
    blur = lambda image: cv2.GaussianBlur(image, (11, 11), 1.5)
    mu_a, mu_b = blur(a), blur(b)
    var_a = blur(a * a) - mu_a ** 2
    var_b = blur(b * b) - mu_b ** 2
    covariance = blur(a * b) - mu_a * mu_b
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    value = ((2 * mu_a * mu_b + c1) * (2 * covariance + c2)) / ((mu_a ** 2 + mu_b ** 2 + c1) * (var_a + var_b + c2))
    return float(value.mean())


def _registered_face_ssim(a: np.ndarray, b: np.ndarray) -> float:
    """SSIM центральной части лиц после довыравнивания b на a (ECC, поворот + сдвиг)."""
    inner = (slice(20, 100), slice(16, 96))
    warp = np.eye(2, 3, dtype=np.float32)
    try:
        _, warp = cv2.findTransformECC(
            a.astype(np.float32), b.astype(np.float32), warp, cv2.MOTION_EUCLIDEAN,
            (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 60, 1e-5), None, 3,
        )
        b = cv2.warpAffine(b, warp, (b.shape[1], b.shape[0]), flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_REFLECT)
    except cv2.error:
        pass  # ECC не сошёлся — сравниваем без довыравнивания
    return _ssim(a[inner], b[inner])


def _head_pose(landmarks: np.ndarray, size: tuple[int, int]) -> np.ndarray | None:
    focal = float(size[0])
    camera = np.array([[focal, 0, size[0] / 2], [0, focal, size[1] / 2], [0, 0, 1]])
    ok, rotation, _ = cv2.solvePnP(FACE_MODEL, landmarks.astype(np.float64), camera, None, flags=cv2.SOLVEPNP_SQPNP)
    if not ok:
        return None
    matrix, _ = cv2.Rodrigues(rotation)
    return matrix


def _rotation_difference_deg(a: np.ndarray, b: np.ndarray) -> float:
    relative = a.T @ b
    return math.degrees(math.acos(max(-1.0, min(1.0, (np.trace(relative) - 1) / 2))))


def analyze_faces(images: list[SeriesImage]) -> dict[str, Any]:
    detector = cv2.FaceDetectorYN.create(str(YUNET_PATH), "", (320, 320), 0.8, 0.3, 50)
    recognizer = cv2.FaceRecognizerSF.create(str(SFACE_PATH), "")
    faces: list[dict[str, Any] | None] = []
    for image in images:
        detector.setInputSize(image.size)
        _, detections = detector.detect(image.bgr)
        if detections is None or not len(detections):
            faces.append(None)
            continue
        detection = max(detections, key=lambda row: row[2] * row[3])
        width, height = float(detection[2]), float(detection[3])
        aligned = recognizer.alignCrop(image.bgr, detection)
        landmarks = detection[4:14].reshape(5, 2)
        faces.append({
            "image": image.index,
            "count": int(len(detections)),
            "box": [round(float(value) / image.scale) for value in detection[:4]],  # в координатах оригинала
            "_box": [float(value) for value in detection[:4]],
            "size_px": round(min(width, height)),
            "score": round(float(detection[14]), 3),
            "low_resolution": min(width, height) < MIN_FACE_PX,
            "_feature": recognizer.feature(aligned),
            "_aligned": cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY),
            "_pose": _head_pose(landmarks, image.size),
            "crop": _jpeg_data_url(cv2.cvtColor(aligned, cv2.COLOR_BGR2RGB), 85),
        })

    pairs = []
    for first, second in combinations([face for face in faces if face], 2):
        cosine = float(recognizer.match(first["_feature"], second["_feature"], cv2.FaceRecognizerSF_FR_COSINE))
        pose_diff = _rotation_difference_deg(first["_pose"], second["_pose"]) if first["_pose"] is not None and second["_pose"] is not None else None
        pairs.append({
            "a": first["image"],
            "b": second["image"],
            "cosine": round(cosine, 3),
            "same_person": cosine >= SAME_PERSON_COSINE,
            "face_ssim": round(_registered_face_ssim(first["_aligned"], second["_aligned"]), 3),
            "pose_diff_deg": round(pose_diff, 1) if pose_diff is not None else None,
            "low_resolution": first["low_resolution"] or second["low_resolution"],
        })
    public = [{key: value for key, value in face.items() if not key.startswith("_")} if face else None for face in faces]
    boxes = {face["image"]: face["_box"] for face in faces if face}
    return {"faces": public, "pairs": pairs, "_boxes": boxes}


# ------------------------------------------------------------- Verdict ---

def _reason(level: str, polarity: int, title: str, detail: str) -> dict[str, Any]:
    """level: strong | weak | info; polarity: +1 поддерживает подлинность, −1 против, 0 нейтрально."""
    return {"level": level, "polarity": polarity, "title": title, "detail": detail}


def _connected(count: int, edges: list[tuple[int, int]]) -> bool:
    parent = list(range(count))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for a, b in edges:
        parent[find(a)] = find(b)
    return len({find(node) for node in range(count)}) == 1


def series_verdict(mode: str, count: int, pairs: list[dict[str, Any]], exif: dict[str, Any], faces: dict[str, Any] | None) -> dict[str, Any]:
    reasons = list(exif["reasons"])
    label = lambda pair: f"Снимки {pair['a'] + 1} и {pair['b'] + 1}"
    by_status: dict[str, list[dict[str, Any]]] = {}
    for pair in pairs:
        by_status.setdefault(pair["status"], []).append(pair)

    strong_negative = False
    for pair in by_status.get("inconsistent", []):
        if mode == "scene":
            reasons.append(_reason("strong", -1, f"{label(pair)}: геометрия не сходится", pair["detail"]))
            strong_negative = True
    for pair in by_status.get("planar", []):
        reasons.append(_reason("weak", -1 if mode == "scene" else 0, f"{label(pair)}: плоская сцена", pair["detail"]))
    for pair in by_status.get("near_duplicate", []):
        reasons.append(_reason("info", 0, f"{label(pair)}: почти одинаковые кадры", pair["detail"]))
    for pair in by_status.get("consistent_3d", []):
        if pair.get("suspicious_share") is not None and pair["suspicious_share"] >= 0.35:
            reasons.append(_reason("weak", -1, f"{label(pair)}: есть несовпадающие зоны", f"Около {pair['suspicious_share']:.0%} детализированных участков в зоне перекрытия не нашли пары — проверьте выделенные области."))
    consistent_edges = [(pair["a"], pair["b"]) for pair in by_status.get("consistent_3d", [])]
    geometry_ok = count >= 2 and _connected(count, consistent_edges)
    if mode == "scene":
        if geometry_ok:
            reasons.append(_reason("strong", 1, "Ракурсы согласованы", "Все снимки связаны геометрически согласованными парами — так выглядит реальная съёмка одной сцены."))
        elif consistent_edges:
            reasons.append(_reason("weak", 1, "Часть ракурсов согласована", f"Согласованных пар: {len(consistent_edges)} из {len(pairs)}."))

    identity_ok = False
    if faces is not None:
        found = [face for face in faces["faces"] if face]
        if len(found) < 2:
            reasons.append(_reason("info", 0, "Недостаточно лиц", f"Лицо найдено на {len(found)} из {count} снимков."))
        backgrounds_differ = {(pair["a"], pair["b"]) for pair in pairs if pair["status"] == "no_overlap"}
        for pair in faces["pairs"]:
            name = label(pair)
            if pair["low_resolution"]:
                reasons.append(_reason("info", 0, f"{name}: лицо слишком мелкое", "Сравнение личности ненадёжно при лице меньше 60 px."))
                continue
            if pair["cosine"] < DIFFERENT_PERSON_COSINE:
                reasons.append(_reason("strong", -1, f"{name}: разные люди", f"Сходство лиц {pair['cosine']:.2f} — ниже порога {SAME_PERSON_COSINE}."))
                strong_negative = True
            elif not pair["same_person"]:
                reasons.append(_reason("weak", -1, f"{name}: сходство лиц сомнительное", f"Сходство {pair['cosine']:.2f}, порог {SAME_PERSON_COSINE}."))
            pasted = (
                pair["face_ssim"] >= PASTED_FACE_SSIM
                and pair["pose_diff_deg"] is not None and pair["pose_diff_deg"] <= PASTED_FACE_POSE_DEG
                and (pair["a"], pair["b"]) in backgrounds_differ
            )
            if pasted:
                reasons.append(_reason("strong", -1, f"{name}: лицо будто вклеено", f"На разных фонах лицо почти пиксель в пиксель совпадает (SSIM {pair['face_ssim']:.2f}, поворот головы отличается на {pair['pose_diff_deg']}°). У живого человека в разных сценах выражение и ракурс меняются."))
                strong_negative = True
        valid = [pair for pair in faces["pairs"] if not pair["low_resolution"]]
        compared = {pair[key] for pair in valid for key in ("a", "b")}
        identity_ok = len(found) == count and len(compared) == count and bool(valid) and all(pair["same_person"] for pair in valid)
        if identity_ok:
            reasons.append(_reason("weak", 1, "Один и тот же человек", "Лица на всех снимках совпадают. Это не доказывает реальность: AI-персонажи тоже бывают согласованными."))

    if strong_negative:
        verdict = "inconsistent"
        headline = "Снимки не согласуются между собой"
    elif (mode == "scene" and geometry_ok) or (mode == "person" and identity_ok):
        verdict = "consistent"
        headline = "Снимки согласованы" if mode == "scene" else "На всех снимках один человек"
    else:
        verdict = "insufficient"
        headline = "Недостаточно данных для вывода по серии"
    order = {"strong": 0, "weak": 1, "info": 2}
    reasons.sort(key=lambda item: (order[item["level"]], item["polarity"]))
    return {"verdict": verdict, "headline": headline, "reasons": reasons}


def analyze_series(items: list[tuple[str, bytes]], mode: str) -> dict[str, Any]:
    cv2.setNumThreads(1)
    images = [SeriesImage(index, name, raw) for index, (name, raw) in enumerate(items)]
    faces = analyze_faces(images) if mode == "person" else None
    # В режиме «человек» сравниваем фоны без лица, иначе одинаковое лицо выглядит как одинаковая сцена.
    detect_features(images, faces.pop("_boxes") if faces else None)
    pairs = [analyze_pair(a, b) for a, b in combinations(images, 2)]
    exif = exif_series(images)
    verdict = series_verdict(mode, len(images), pairs, exif, faces)
    return {
        "mode": mode,
        "count": len(images),
        "pairs": pairs,
        "exif_series": {key: value for key, value in exif.items() if key != "reasons"},
        "faces": faces,
        **verdict,
    }
