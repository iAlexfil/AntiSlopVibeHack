"""Сборка результата серии поверх multiview-воркера: сводка по снимкам, лицо против фона, рекомендации.

Модуль не импортирует OpenCV: тяжёлая геометрия и лица выполняются только в multiview_worker.py.
"""
from __future__ import annotations

import io
from typing import Any

from PIL import Image, ImageOps

MAX_SERIES_IMAGES = 6
MIN_SERIES_IMAGES = 2
AI_THRESHOLD = 65
FACE_BACKGROUND_MIN_PX = 80
FACE_BACKGROUND_GAP = 0.6


def image_summary(index: int, name: str, result: dict[str, Any]) -> dict[str, Any]:
    probability = result.get("generation_probability") or {}
    local = result.get("local_model") or {}
    return {
        "index": index,
        "filename": name,
        "width": result.get("width"),
        "height": result.get("height"),
        "percent": probability.get("percent"),
        "label": probability.get("label"),
        "headline": result.get("headline"),
        "local_model": local.get("score") if local.get("status") == "ok" else None,
        "findings": [item["title"] for item in result.get("findings", []) if item.get("level") == "strong"],
    }


def face_background_crops(raw: bytes, box: list[int]) -> tuple[bytes, bytes | None]:
    """Кроп лица с запасом и самая большая полоса кадра без лица — для раздельной оценки локальной моделью.

    Фон берём чистым куском исходных пикселей: размытие или заливка лица сами выглядят для модели как артефакт.
    """
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    x, y, w, h = box
    side = 1.8 * max(w, h)
    cx, cy = x + w / 2, y + h / 2
    face = image.crop((max(0, cx - side / 2), max(0, cy - side / 2), min(image.width, cx + side / 2), min(image.height, cy + side / 2)))
    # Запас вокруг лица: волосы, шея, плечи.
    left, top = max(0, int(x - 0.6 * w)), max(0, int(y - 0.8 * h))
    right, bottom = min(image.width, int(x + 1.6 * w)), min(image.height, int(y + 2.5 * h))
    strips = [
        (0, 0, left, image.height), (right, 0, image.width, image.height),
        (0, 0, image.width, top), (0, bottom, image.width, image.height),
    ]
    strips = [strip for strip in strips if min(strip[2] - strip[0], strip[3] - strip[1]) >= 128]
    background = image.crop(max(strips, key=lambda strip: (strip[2] - strip[0]) * (strip[3] - strip[1]))) if strips else None
    encoded: list[bytes | None] = []
    for picture in (face, background):
        if picture is None:
            encoded.append(None)
            continue
        buffer = io.BytesIO()
        picture.save(buffer, "JPEG", quality=95)
        encoded.append(buffer.getvalue())
    return encoded[0], encoded[1]


def _reason(level: str, polarity: int, title: str, detail: str) -> dict[str, Any]:
    return {"level": level, "polarity": polarity, "title": title, "detail": detail}


def finalize(series: dict[str, Any], images: list[dict[str, Any]], face_background: list[dict[str, Any]]) -> dict[str, Any]:
    """Добавляет к вердикту воркера сигналы отдельных снимков и рекомендации действий."""
    reasons = list(series.get("reasons", []))
    scored = [image for image in images if image.get("percent") is not None]
    flagged = [image for image in scored if image["percent"] >= AI_THRESHOLD]
    verdict = series.get("verdict", "insufficient")
    headline = series.get("headline", "")
    if flagged:
        names = ", ".join(str(image["index"] + 1) for image in flagged)
        majority = len(flagged) * 2 > len(scored)
        reasons.insert(0, _reason(
            "strong" if majority else "weak", -1,
            f"Признаки AI на снимках: {names}",
            f"Итоговая оценка ≥ {AI_THRESHOLD}% на {len(flagged)} из {len(scored)} снимков. Согласованность серии этого не отменяет: AI-видео тоже даёт согласованные кадры.",
        ))
        if majority and verdict == "consistent":
            verdict = "insufficient"
            headline = "Снимки согласованы, но детектор видит признаки AI"
    for item in face_background:
        if item.get("face") is None or item.get("background") is None:
            continue
        gap = abs(item["face"] - item["background"])
        if gap >= FACE_BACKGROUND_GAP and max(item["face"], item["background"]) >= AI_THRESHOLD / 100:
            part = "лицо" if item["face"] > item["background"] else "фон"
            reasons.append(_reason(
                "weak", -1, f"Снимок {item['image'] + 1}: лицо и фон оцениваются по-разному",
                f"Модель считает AI скорее {part} (лицо {item['face']:.0%}, фон {item['background']:.0%}). Возможен коллаж или вклейка; сигнал слабый — модель обучена на целых кадрах.",
            ))
    return {**series, "verdict": verdict, "headline": headline, "reasons": reasons,
            "images": images, "face_background": face_background,
            "recommendations": recommendations(series.get("mode", "scene"), verdict, series)}


def recommendations(mode: str, verdict: str, series: dict[str, Any]) -> list[str]:
    statuses = {pair.get("status") for pair in series.get("pairs", [])}
    if verdict == "inconsistent":
        actions = [
            "Не используйте эти снимки как доказательство и не пересылайте их дальше.",
            "Попросите оригиналы файлами или короткое видео с плавной сменой ракурса.",
        ]
        if mode == "person":
            actions.append("Попросите видео-кружок: поворот головы и названный вами жест в реальном времени.")
        return actions
    if verdict == "consistent":
        actions = ["Серия выглядит согласованной — это повышает доверие, но не является доказательством."]
        actions.append("Для важных решений запросите снимок с контрольным кодом на бумаге в кадре.")
        return actions
    actions = []
    if "near_duplicate" in statuses:
        actions.append("Кадры почти одинаковые: попросите снять объект, сместившись на 1–2 шага в сторону.")
    if "planar" in statuses:
        actions.append("Сцена выглядит плоской: убедитесь, что это не съёмка экрана или распечатки — попросите ракурс сбоку.")
    if mode == "person" and not any(face for face in (series.get("faces") or {}).get("faces", [])):
        actions.append("Лица не найдены: нужны снимки, где лицо видно крупно и анфас.")
    actions.append("Добавьте ещё 1–2 снимка той же сцены с заметно другого ракурса.")
    return actions
