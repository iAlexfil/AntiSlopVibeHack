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


def consistency_assessment(result: dict[str, Any], count: int) -> dict[str, Any]:
    """Описывает сравнение кадров независимо от AI-оценок отдельных снимков."""
    verdict = result.get("verdict", "insufficient")
    person = result.get("mode") == "person"
    pairs = result.get("pairs", [])
    faces = result.get("faces") or {}
    comparable = ([pair for pair in faces.get("pairs", []) if not pair.get("low_resolution")]
                  if person else [pair for pair in pairs if pair.get("status") not in {"no_overlap", "weak"}])
    checked = {pair[key] for pair in comparable for key in ("a", "b") if key in pair}
    assessment = {
        "verdict": verdict, "kind": verdict, "label": "", "headline": "", "detail": "",
        "checked_images": len(checked), "total_images": count,
        "checked_pairs": len(comparable), "total_pairs": count * (count - 1) // 2,
    }
    if verdict == "consistent":
        assessment.update(
            label="Один и тот же человек" if person else "Ракурсы согласованы",
            headline="Лица на снимках совпадают" if person else "Снимки связаны общей геометрией",
            detail="Сравнение лиц выполнено для всех снимков." if person else "Все снимки связаны геометрически согласованными парами.",
        )
    elif verdict == "inconsistent":
        assessment.update(label="Найдены расхождения", headline="Есть противоречия между снимками",
                          detail="Ниже показаны пары и признаки, на которых обнаружены расхождения.")
    elif person:
        found = [face for face in faces.get("faces", []) if face]
        if len(found) < count:
            assessment.update(kind="faces_missing", label="Не на всех кадрах видно лицо",
                              headline=f"Лицо найдено на {len(found)} из {count} снимков",
                              detail="Для сравнения нужен один человек с хорошо видимым лицом на каждом снимке.")
        elif any(face.get("low_resolution") for face in found):
            assessment.update(kind="faces_small", label="Лица слишком мелкие",
                              headline="Часть лиц не удалось надёжно сравнить",
                              detail="Добавьте более крупные портреты без сильного размытия. Менять фон не требуется.")
        else:
            assessment.update(kind="identity_uncertain", label="Сходство лиц неоднозначно",
                              headline="Не все лица удалось уверенно сопоставить",
                              detail="Попробуйте чёткие снимки одного человека с небольшим поворотом головы.")
    else:
        statuses = {pair.get("status") for pair in pairs}
        if statuses == {"near_duplicate"}:
            assessment.update(kind="near_duplicate", label="Кадры почти одинаковые",
                              headline="Изображения совпадают, но нового ракурса нет",
                              detail="Сместитесь в сторону при съёмке, сохранив общие предметы в кадре.")
        elif statuses and statuses <= {"planar", "near_duplicate"}:
            assessment.update(kind="planar", label="Общие детали найдены",
                              headline="Для проверки объёма не хватает смещения камеры",
                              detail="Совпадения объясняются плоской поверхностью или поворотом на месте. Нужен ракурс со смещением в сторону.")
        elif any(pair.get("status") == "consistent_3d" for pair in pairs):
            assessment.update(kind="partial", label="Согласована часть серии",
                              headline="Не все снимки удалось связать между собой",
                              detail="Посмотрите пары ниже: добавьте промежуточный ракурс между несвязанными снимками.")
        elif statuses == {"no_overlap"}:
            assessment.update(kind="no_overlap", label="Общих деталей не найдено",
                              headline="Не удалось сопоставить содержимое снимков",
                              detail="Нужны снимки с перекрытием: одни и те же предметы должны быть видны на нескольких кадрах.")
        else:
            assessment.update(kind="weak", label="Совпадений недостаточно для геометрии",
                              headline="Общие детали не дали надёжного сравнения ракурсов",
                              detail="Попробуйте более чёткие кадры с меньшей разницей ракурсов и общими предметами.")
    return assessment


def finalize(series: dict[str, Any], images: list[dict[str, Any]], face_background: list[dict[str, Any]]) -> dict[str, Any]:
    """Добавляет к вердикту воркера сигналы отдельных снимков и рекомендации действий."""
    reasons = list(series.get("reasons", []))
    scored = [image for image in images if image.get("percent") is not None]
    flagged = [image for image in scored if image["percent"] >= AI_THRESHOLD]
    consistency = consistency_assessment(series, series.get("count", len(images)))
    ai_assessment = {
        "status": "flagged" if flagged else "not_flagged" if scored else "unavailable",
        "headline": (f"Признаки ИИ на {len(flagged)} из {len(scored)} снимков" if flagged else
                     "Высоких оценок ИИ не получено" if scored else "Оценки ИИ недоступны"),
        "detail": ("Оценки отдельных снимков показаны ниже. Они не меняют результат сравнения ракурсов или лиц."
                   if scored else "Детекторы не вернули численные оценки. Сравнение кадров показано отдельно."),
        "flagged_images": len(flagged), "checked_images": len(scored), "total_images": len(images),
    }
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
            "consistency": consistency, "ai_assessment": ai_assessment,
            "images": images, "face_background": face_background,
            "recommendations": recommendations(series.get("mode", "scene"), consistency["verdict"], series)}


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
    if mode == "person":
        return ["Добавьте чёткие снимки того же человека с крупным лицом и небольшим поворотом головы.",
                "Если в кадре несколько людей, выберите снимки, где нужный человек один."]
    actions.append("Добавьте ещё 1–2 снимка той же сцены с заметно другого ракурса и общими предметами в кадре.")
    return actions
