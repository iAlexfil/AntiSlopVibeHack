"""Private report drafts and explicitly published, expiring report snapshots.

Only this module's small allowlisted document is persisted; original image bytes,
filenames, metadata and raw service responses never enter the database.
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent
DRAFT_TTL_SECONDS = 24 * 60 * 60
PUBLIC_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_RECORDS = 256
MAX_STORAGE_BYTES = 128 * 1024 * 1024
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_PREVIEW_BYTES = 256 * 1024
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


class ReportStoreError(Exception):
    """A report could not be safely saved or loaded; analysis can still succeed."""


class ReportNotFound(ReportStoreError):
    """The opaque draft/public token does not exist or has expired."""


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _text(value: Any, limit: int = 500) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join("".join(char for char in value if char.isprintable() or char.isspace()).split())[:limit]


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any, limit: int = 16) -> list[Any]:
    return value[:limit] if isinstance(value, list) else []


def _score(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(float(value), 2) if 0 <= value <= 100 else None


def _item(label: str, value: Any, detail: Any = "") -> dict[str, str]:
    return {"label": _text(label, 300), "value": _text(value, 1200), "detail": _text(detail, 600)}


def _section(title: str, items: list[dict[str, str]]) -> dict[str, Any]:
    return {"title": title, "items": items[:16]}


def _percent(value: Any) -> str:
    score = _score(value)
    return f"{score:g}%" if score is not None else "Нет численной оценки"


def _metric(value: Any, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value) if minimum <= value <= maximum else None


SIGNALS = {
    "model": ("Локальная ML-модель", "Оценка классификатора всего изображения."),
    "semantic": ("Смысловой анализ", "Визуальные противоречия и особенности содержимого."),
    "external": ("Внешние детекторы", "Сводный сигнал подключённых специализированных сервисов."),
    "provenance": ("Вотермарки и происхождение", "Признаки происхождения; отсутствие метки не подтверждает подлинность."),
    "pixels": ("Пиксельные аномалии", "Неспецифичные эвристики; этот сигнал не входит в итоговую вероятность."),
}
PROVIDERS = {
    "local_frontier": "Локальная ML-модель", "openai_vision": "Смысловой анализ",
    "synthid": "OpenAI SynthID / C2PA", "hive": "Hive", "sightengine": "Sightengine",
    "aiornot": "AI or Not", "wasitai": "WasItAI",
}
STATUSES = {"ok": "Получен ответ", "error": "Ответ недоступен", "not_configured": "Не подключён", "unavailable": "Недоступен"}
PAIR_LABELS = {
    "consistent_3d": "Геометрия согласована", "inconsistent": "Найдены расхождения",
    "planar": "Совпадения объясняются плоскостью", "near_duplicate": "Почти одинаковые кадры",
    "no_overlap": "Недостаточно общих деталей", "weak": "Слабое совпадение",
}


def _single_sections(result: dict[str, Any]) -> list[dict[str, Any]]:
    signals = []
    for raw in _list(result.get("probability_breakdown")):
        signal = _dict(raw)
        specification = SIGNALS.get(signal.get("id"))
        if specification:
            title, detail = specification
            signals.append(_item(title, _percent(signal.get("percent")), detail))
    sections = [_section("Источники сигнала", signals)] if signals else []
    sources = []
    for raw in _list(result.get("api_results")):
        provider = _dict(raw)
        name = PROVIDERS.get(provider.get("id"))
        if not name:
            continue
        status = provider.get("status")
        numeric = _score(provider.get("score")) if status == "ok" else None
        value = _percent(min(99, round(numeric * 100))) if numeric is not None and numeric <= 1 else STATUSES.get(status, "Нет результата")
        if status == "ok" and (provider.get("provenance") or provider.get("id") == "synthid"):
            value = "AI-происхождение обнаружено" if provider.get("verdict") == "ai" else "AI-происхождение не обнаружено"
        sources.append(_item(name, value))
        analysis = _dict(provider.get("analysis")) if provider.get("id") == "openai_vision" and status == "ok" else {}
        observations = []
        if _text(analysis.get("explanation")):
            observations.append(_item("Обоснование", analysis["explanation"]))
        for raw_signal in _list(analysis.get("signals"), 8):
            signal = _dict(raw_signal)
            if _text(signal.get("observation")):
                observations.append(_item(signal["observation"], signal.get("explanation"), signal.get("alternative_explanation")))
        if observations:
            sections.append(_section("Визуальные наблюдения", observations))
    if sources:
        sections.append(_section("Модели и сервисы", sources))
    return sections


def _series_sections(result: dict[str, Any]) -> list[dict[str, Any]]:
    consistency = _dict(result.get("consistency"))
    ai = _dict(result.get("ai_assessment"))
    sections = [_section("Два направления проверки", [
        _item("Согласованность снимков", consistency.get("headline") or consistency.get("label") or "Недостаточно данных", consistency.get("detail")),
        _item("Признаки ИИ", ai.get("headline") or "Оценки ИИ недоступны", ai.get("detail")),
    ])]
    person = result.get("mode") == "person"
    pairs = _person_pairs(result) if person else []
    if not person:
        for raw in _list(result.get("pairs"), 15):
            pair = _dict(raw)
            a, b = pair.get("a"), pair.get("b")
            if not isinstance(a, int) or not isinstance(b, int) or not (0 <= a < MAX_IMAGES and 0 <= b < MAX_IMAGES):
                continue
            pairs.append(_item(f"Снимки {a + 1} и {b + 1}", PAIR_LABELS.get(pair.get("status"), "Нет результата")))
    if pairs:
        sections.append(_section("Сравнение лиц" if person else "Сравнение пар", pairs))
    elif person:
        sections.append(_section("Сравнение лиц", [_item("Результат", "Недостаточно данных для попарного сравнения лиц")]))
    actions = [_item(f"Рекомендация {index + 1}", action) for index, action in enumerate(_list(result.get("recommendations"), 6)) if _text(action)]
    if actions:
        sections.append(_section("Что можно проверить дополнительно", actions))
    return sections


def _person_pairs(result: dict[str, Any]) -> list[dict[str, str]]:
    """Render the existing worker rules without persisting raw reasons or EXIF."""
    backgrounds_differ = set()
    for raw in _list(result.get("pairs"), 15):
        pair = _dict(raw)
        a, b = pair.get("a"), pair.get("b")
        if type(a) is int and type(b) is int and pair.get("status") == "no_overlap":
            backgrounds_differ.add((a, b))
    items = []
    for raw in _list(_dict(result.get("faces")).get("pairs"), 15):
        pair = _dict(raw)
        a, b = pair.get("a"), pair.get("b")
        if type(a) is not int or type(b) is not int or not (0 <= a < MAX_IMAGES and 0 <= b < MAX_IMAGES):
            continue
        cosine = _metric(pair.get("cosine"), -1, 1)
        details = []
        if pair.get("low_resolution"):
            value = "Лица слишком мелкие для надёжного сравнения"
            details.append("Хотя бы одно лицо меньше 60 px в копии для анализа; вывод о личности не сделан.")
        elif cosine is None:
            value = "Сходство лиц не удалось оценить"
        else:
            # Mirror multiview.series_verdict: use the unrounded same_person flag
            # where available, while its strict different-person rule uses cosine.
            same_person = pair.get("same_person")
            if cosine < 0.25:
                value = "Разные люди"
            elif same_person is True or (not isinstance(same_person, bool) and cosine >= 0.363):
                value = "Один и тот же человек"
            else:
                value = "Сходство лиц неоднозначно"
            details.append("Лица не меньше 60 px в копии для анализа.")
        if cosine is not None:
            details.append(f"Сходство {cosine:.3f}; порог одного человека 0.363, разных людей — ниже 0.250.")
        ssim = _metric(pair.get("face_ssim"), -1, 1)
        pose = _metric(pair.get("pose_diff_deg"), 0, 180)
        pasted = (not pair.get("low_resolution") and ssim is not None and ssim >= 0.85
                  and pose is not None and pose <= 4.0 and (a, b) in backgrounds_differ)
        if pasted:
            value += ". Лицо будто вклеено"
            details.append(f"SSIM лица {ssim:.3f} ≥ 0.85, разница позы {pose:g}° ≤ 4°; общие детали фона не найдены.")
        items.append(_item(f"Снимки {a + 1} и {b + 1}", value, " ".join(details)))
    return items


def _preview(raw: bytes, edge: int, label: str, score: Any) -> dict[str, Any]:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ReportStoreError("Не удалось подготовить изображение отчёта")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
            if width * height > MAX_IMAGE_PIXELS:
                raise ReportStoreError("Изображение отчёта превышает допустимый размер")
            if image.getexif().get(274, 1) in (5, 6, 7, 8):
                width, height = height, width
            image.draft("RGB", (edge * 2, edge * 2))
            image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
            rotated = ImageOps.exif_transpose(image)
            if rotated.mode in {"RGBA", "LA"} or "transparency" in rotated.info:
                transparent = rotated.convert("RGBA")
                rotated = Image.new("RGB", transparent.size, "white")
                rotated.paste(transparent, mask=transparent.getchannel("A"))
            else:
                rotated = rotated.convert("RGB")
            # A fresh pixel-only image prevents EXIF, XMP, ICC and comments from being saved.
            clean = Image.frombytes("RGB", rotated.size, rotated.tobytes())
            for quality in (82, 72, 62, 50, 40):
                buffer = io.BytesIO()
                clean.save(buffer, "JPEG", quality=quality)
                data = buffer.getvalue()
                if len(data) <= MAX_PREVIEW_BYTES:
                    return {"label": label, "preview": "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii"),
                            "score": _score(score), "width": width, "height": height}
    except ReportStoreError:
        raise
    except (OSError, ValueError, MemoryError, Image.DecompressionBombError) as error:
        raise ReportStoreError("Не удалось подготовить изображение отчёта") from error
    raise ReportStoreError("Предпросмотр отчёта превышает допустимый размер")


def _snapshot(kind: str, result: dict[str, Any], images: list[bytes], created: float) -> dict[str, Any]:
    if kind not in {"single", "series"} or not isinstance(result, dict):
        raise ReportStoreError("Некорректный тип отчёта")
    if not isinstance(images, list) or not 1 <= len(images) <= MAX_IMAGES or (kind == "single" and len(images) != 1):
        raise ReportStoreError("Некорректное число изображений отчёта")
    score = _score(_dict(result.get("generation_probability")).get("percent")) if kind == "single" else None
    if kind == "single":
        label, tone = (("Высокая вероятность ИИ", "high") if score is not None and score >= 65 else
                       ("Низкая вероятность ИИ", "low") if score is not None and score <= 35 else
                       ("Неоднозначный результат", "mid") if score is not None else ("Недостаточно данных", "unknown"))
        headline = label
        sections = _single_sections(result)
        summary = "Вероятность генерации или существенного изменения изображения генеративным ИИ."
        if _dict(result.get("generation_probability")).get("label") == "Низкая согласованность":
            label, headline, tone = "Низкая согласованность", "Детекторы сильно расходятся", "mid"
            summary = "Детекторы сильно расходятся — итоговый процент ненадёжен."
            sections.insert(0, _section("Согласованность источников", [
                _item("Предупреждение", "Низкая согласованность", "Оценки источников сильно различаются; итоговый процент ненадёжен.")]))
        image_scores = [score]
    else:
        consistency = _dict(result.get("consistency"))
        verdict = consistency.get("verdict", result.get("verdict"))
        label = _text(consistency.get("label"), 300) or {"consistent": "Снимки согласованы", "inconsistent": "Найдены расхождения"}.get(verdict, "Недостаточно данных для сравнения")
        headline = _text(consistency.get("headline"), 300) or label
        tone = {"consistent": "low", "inconsistent": "high"}.get(verdict, "mid")
        summary = "Проверка согласованности и признаков ИИ показана отдельно."
        sections = _series_sections(result)
        summaries = _list(result.get("images"), MAX_IMAGES)
        image_scores = [_dict(summaries[index]).get("percent") if index < len(summaries) else None for index in range(len(images))]
    return {
        "version": 1, "kind": kind, "title": "Отчёт AntiSlop" + (" — серия снимков" if kind == "series" else ""),
        "created_at": _iso(created), "headline": headline, "label": label, "score": score, "tone": tone,
        "summary": summary,
        "images": [_preview(raw, 800 if kind == "single" else 480, "Изображение" if kind == "single" else f"Снимок {index + 1}", image_scores[index]) for index, raw in enumerate(images)],
        "sections": sections[:8],
    }


@contextmanager
def _database() -> Iterator[sqlite3.Connection]:
    connection = None
    try:
        configured = os.environ.get("ANTISLOP_REPORTS_DIR")
        directory = Path(configured).expanduser() if configured else ROOT / ".antislop-reports"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / "reports.sqlite3"
        connection = sqlite3.connect(path, timeout=5)
        os.chmod(path, 0o600)
        connection.row_factory = sqlite3.Row
        # DELETE journaling bounds auxiliary files and avoids an accumulating WAL.
        connection.execute("PRAGMA journal_mode=DELETE")
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        connection.execute(f"PRAGMA max_page_count={MAX_STORAGE_BYTES // page_size}")
        connection.execute("""CREATE TABLE IF NOT EXISTS reports (
            draft_id TEXT PRIMARY KEY, public_token TEXT UNIQUE,
            created_at REAL NOT NULL, draft_expires_at REAL NOT NULL, public_expires_at REAL,
            snapshot TEXT NOT NULL, snapshot_bytes INTEGER NOT NULL
        )""")
        yield connection
    except ReportStoreError:
        raise
    except (sqlite3.Error, OSError, ValueError) as error:
        raise ReportStoreError("Хранилище отчётов временно недоступно") from error
    finally:
        if connection is not None:
            connection.close()


def _validate_token(token: str) -> None:
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise ReportNotFound("Отчёт не найден или срок хранения истёк")


def _remove_expired(connection: sqlite3.Connection, now: float) -> None:
    connection.execute("""DELETE FROM reports WHERE
        (public_token IS NULL AND draft_expires_at <= ?) OR
        (public_token IS NOT NULL AND public_expires_at <= ?)""", (now, now))


def create_draft(kind: str, result: dict[str, Any], images: list[bytes]) -> str:
    """Save a private 24-hour snapshot; does not create a public URL."""
    created = time.time()
    document = _snapshot(kind, result, images, created)
    encoded = json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    size = len(encoded.encode("utf-8"))
    if size > MAX_DOCUMENT_BYTES:
        raise ReportStoreError("Отчёт превышает допустимый размер")
    draft_id = secrets.token_urlsafe(32)
    with _database() as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            _remove_expired(connection, created)
            count, stored = connection.execute("SELECT COUNT(*), COALESCE(SUM(snapshot_bytes), 0) FROM reports").fetchone()
            if count >= MAX_RECORDS or stored + size > MAX_STORAGE_BYTES:
                raise ReportStoreError("Хранилище отчётов заполнено. Попробуйте позже")
            connection.execute("INSERT INTO reports VALUES (?, NULL, ?, ?, NULL, ?, ?)",
                               (draft_id, created, created + DRAFT_TTL_SECONDS, encoded, size))
    return draft_id


def publish(draft_id: str) -> dict[str, str]:
    """Publish once, preserving the same public token and expiry on later calls."""
    _validate_token(draft_id)
    now = time.time()
    with _database() as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            _remove_expired(connection, now)
            row = connection.execute("SELECT public_token, draft_expires_at, public_expires_at FROM reports WHERE draft_id = ?", (draft_id,)).fetchone()
            if row is None:
                raise ReportNotFound("Отчёт не найден или срок хранения истёк")
            token, expires = row["public_token"], row["public_expires_at"]
            if token is None:
                if row["draft_expires_at"] <= now:
                    raise ReportNotFound("Отчёт не найден или срок хранения истёк")
                token = secrets.token_urlsafe(32)
                while token == draft_id:
                    token = secrets.token_urlsafe(32)
                expires = now + PUBLIC_TTL_SECONDS
                connection.execute("UPDATE reports SET public_token = ?, public_expires_at = ? WHERE draft_id = ?", (token, expires, draft_id))
    return {"path": f"/r/{token}", "pdf_path": f"/api/reports/{token}/pdf", "expires_at": _iso(expires)}


def _get(token: str, public: bool) -> dict[str, Any]:
    _validate_token(token)
    column = "public_token" if public else "draft_id"
    expiry = "public_expires_at" if public else "draft_expires_at"
    with _database() as connection:
        row = connection.execute(f"SELECT snapshot, {expiry} AS expires FROM reports WHERE {column} = ?", (token,)).fetchone()
        if row is None or row["expires"] is None or row["expires"] <= time.time():
            raise ReportNotFound("Отчёт не найден или срок хранения истёк")
        try:
            snapshot = json.loads(row["snapshot"])
        except (TypeError, ValueError) as error:
            raise ReportStoreError("Сохранённый отчёт недоступен") from error
        if not isinstance(snapshot, dict):
            raise ReportStoreError("Сохранённый отчёт недоступен")
        snapshot["expires_at"] = _iso(row["expires"])
        return snapshot


def get_public(token: str) -> dict[str, Any]:
    return _get(token, public=True)


def get_draft(draft_id: str) -> dict[str, Any]:
    return _get(draft_id, public=False)
