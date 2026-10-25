import io
from datetime import datetime

import cv2
import numpy as np
from PIL import Image

import multiview
import series


def _texture(width: int, height: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    image = np.zeros((height, width, 3), dtype=np.uint8)
    for _ in range(900):
        x, y = int(rng.integers(0, width)), int(rng.integers(0, height))
        color = tuple(int(c) for c in rng.integers(0, 255, 3))
        if rng.random() < 0.5:
            cv2.circle(image, (x, y), int(rng.integers(2, 14)), color, -1)
        else:
            cv2.rectangle(image, (x, y), (x + int(rng.integers(3, 20)), y + int(rng.integers(3, 20))), color, -1)
    return image


def _png(array: np.ndarray, exif: Image.Exif | None = None) -> bytes:
    buffer = io.BytesIO()
    image = Image.fromarray(array)
    if exif is not None:
        image.save(buffer, "JPEG", quality=95, exif=exif)
    else:
        image.save(buffer, "PNG")
    return buffer.getvalue()


def _layered_view(shift_background: int, shift_foreground: int) -> np.ndarray:
    """Две плоскости на разной глубине: при сдвиге камеры ближняя смещается в ту же сторону, но сильнее."""
    background = _texture(760, 480, 1)
    foreground = _texture(380, 300, 2)
    view = background[:, shift_background:shift_background + 640].copy()
    view[90:390, 120 + shift_foreground:500 + shift_foreground] = foreground
    return view


def _pair(first: bytes, second: bytes, mode: str = "scene") -> dict:
    return multiview.analyze_series([("a.png", first), ("b.png", second)], mode)


def test_scene_geometry() -> None:
    near, far = _png(_layered_view(0, 0)), _png(_layered_view(14, -50))
    result = _pair(near, far)
    assert result["pairs"][0]["status"] == "consistent_3d", {k: v for k, v in result["pairs"][0].items() if k != "visual"}
    assert result["verdict"] == "consistent"
    # Плоскости смещаются в разные стороны — физически невозможно для точек перед камерой.
    impossible = _pair(near, _png(_layered_view(14, 60)))["pairs"][0]
    assert impossible["status"] == "inconsistent" and impossible["cheirality"]["ratio"] < 0.75

    base = _layered_view(0, 0)
    height, width = base.shape[:2]
    rng = np.random.default_rng(5)
    dx = cv2.GaussianBlur(rng.normal(0, 1, (height, width)).astype(np.float32), (0, 0), 25)
    dy = cv2.GaussianBlur(rng.normal(0, 1, (height, width)).astype(np.float32), (0, 0), 25)
    dx *= 16 / np.abs(dx).max()
    dy *= 16 / np.abs(dy).max()
    xx, yy = np.meshgrid(np.arange(width, dtype=np.float32), np.arange(height, dtype=np.float32))
    drifted = cv2.remap(base, xx + dx + 20, yy + dy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    result = _pair(_png(base), _png(drifted))
    assert result["pairs"][0]["status"] == "inconsistent", {k: v for k, v in result["pairs"][0].items() if k != "visual"}
    assert result["verdict"] == "inconsistent"

    homography = np.array([[0.9, 0.08, 30], [-0.05, 0.95, 20], [0.0002, 0.0001, 1]])
    planar = cv2.warpPerspective(base, homography, (width, height), borderMode=cv2.BORDER_REFLECT)
    assert _pair(_png(base), _png(planar))["pairs"][0]["status"] == "planar"
    assert _pair(_png(base), _png(base))["pairs"][0]["status"] == "near_duplicate"
    unrelated = _pair(_png(base), _png(_texture(640, 480, 99)))
    assert unrelated["pairs"][0]["status"] == "no_overlap" and unrelated["verdict"] == "insufficient"
    assert unrelated["pairs"][0].get("visual") is None


def test_exif_series() -> None:
    def exif(model: str, taken: str) -> Image.Exif:
        data = Image.Exif()
        data[271] = "Apple"
        data[272] = model
        data.get_ifd(0x8769)[36867] = taken
        return data

    view = _layered_view(0, 0)
    images = [
        multiview.SeriesImage(0, "IMG_4512.JPG", _png(view, exif("iPhone 15", "2026:10:25 12:00:00"))),
        multiview.SeriesImage(1, "IMG_4513.JPG", _png(view, exif("iPhone 15", "2026:10:25 12:00:09"))),
    ]
    assert images[0].exif["taken"] == datetime(2026, 10, 25, 12, 0, 0)
    report = multiview.exif_series(images)
    titles = {reason["title"] for reason in report["reasons"]}
    assert report["time_span_s"] == 9 and report["sequential_names"]
    assert {"Одна камера", "Снято подряд", "Номера файлов подряд"} <= titles
    images[1] = multiview.SeriesImage(1, "x.jpg", _png(view, exif("Pixel 9", "2026:10:20 12:00:00")))
    assert "Разные устройства" in {reason["title"] for reason in multiview.exif_series(images)["reasons"]}


def test_face_rules() -> None:
    face = cv2.GaussianBlur(_texture(112, 112, 7)[..., 0], (3, 3), 0)
    shifted = np.roll(face, 2, axis=1)
    assert multiview._registered_face_ssim(face, shifted) > 0.9
    assert multiview._registered_face_ssim(face, _texture(112, 112, 8)[..., 0]) < 0.5

    exif = {"reasons": []}
    pairs = [{"a": 0, "b": 1, "status": "no_overlap"}]
    pasted = {"faces": [{"image": 0}, {"image": 1}], "pairs": [{"a": 0, "b": 1, "cosine": 0.95, "same_person": True, "face_ssim": 0.95, "pose_diff_deg": 1.0, "low_resolution": False}]}
    assert multiview.series_verdict("person", 2, pairs, exif, pasted)["verdict"] == "inconsistent"
    natural = {"faces": [{"image": 0}, {"image": 1}], "pairs": [{"a": 0, "b": 1, "cosine": 0.7, "same_person": True, "face_ssim": 0.3, "pose_diff_deg": 25.0, "low_resolution": False}]}
    assert multiview.series_verdict("person", 2, pairs, exif, natural)["verdict"] == "consistent"
    other = {"faces": [{"image": 0}, {"image": 1}], "pairs": [{"a": 0, "b": 1, "cosine": 0.1, "same_person": False, "face_ssim": 0.2, "pose_diff_deg": 10.0, "low_resolution": False}]}
    assert multiview.series_verdict("person", 2, pairs, exif, other)["verdict"] == "inconsistent"


def test_finalize_and_crops() -> None:
    worker = {"mode": "scene", "verdict": "consistent", "headline": "ok", "reasons": [], "pairs": [{"status": "consistent_3d"}]}
    images = [{"index": 0, "percent": 92}, {"index": 1, "percent": 88}]
    result = series.finalize(worker, images, [])
    assert result["verdict"] == "insufficient" and result["reasons"][0]["polarity"] == -1
    assert series.finalize(worker, [{"index": 0, "percent": 5}, {"index": 1, "percent": 8}], [])["verdict"] == "consistent"
    collage = series.finalize(worker, [{"index": 0, "percent": 5}, {"index": 1, "percent": 8}], [{"image": 0, "face": 0.02, "background": 0.9}])
    assert any("лицо и фон" in reason["title"] for reason in collage["reasons"])
    face_bytes, background_bytes = series.face_background_crops(_png(_texture(800, 600, 3)), [350, 200, 100, 120])
    assert Image.open(io.BytesIO(face_bytes)).width >= 150 and background_bytes is not None
    assert series.face_background_crops(_png(_texture(200, 200, 3)), [10, 10, 180, 180])[1] is None


if __name__ == "__main__":
    test_scene_geometry()
    test_exif_series()
    test_face_rules()
    test_finalize_and_crops()
    print("multiview tests passed")
