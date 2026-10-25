"""Прогон evaluation-набора через тот же пайплайн, что и /api/analyze."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
PIPELINE_FILES = ("main.py", "services.py", "forensics.py", "local_model.py", "ml_worker.py")


def _to_rgb(image: Image.Image) -> Image.Image:
    return image.convert("RGB") if image.mode not in ("RGB", "L") else image


def _jpeg(quality: int) -> Callable[[bytes], tuple[bytes, str]]:
    def transform(data: bytes) -> tuple[bytes, str]:
        buffer = io.BytesIO()
        _to_rgb(Image.open(io.BytesIO(data))).save(buffer, "JPEG", quality=quality)
        return buffer.getvalue(), "image/jpeg"
    return transform


def _webp(data: bytes) -> tuple[bytes, str]:
    buffer = io.BytesIO()
    _to_rgb(Image.open(io.BytesIO(data))).save(buffer, "WEBP", quality=80)
    return buffer.getvalue(), "image/webp"


def _resize_half(data: bytes) -> tuple[bytes, str]:
    image = _to_rgb(Image.open(io.BytesIO(data)))
    image = image.resize((max(1, image.width // 2), max(1, image.height // 2)), Image.Resampling.BICUBIC)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue(), "image/png"


def _screenshot_like(data: bytes) -> tuple[bytes, str]:
    """Типичный путь соцсетей: ресайз до 1080 по длинной стороне + JPEG 85."""
    image = _to_rgb(Image.open(io.BytesIO(data)))
    image.thumbnail((1080, 1080), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    return buffer.getvalue(), "image/jpeg"


VARIANTS: dict[str, Callable[[bytes], tuple[bytes, str]] | None] = {
    "original": None,
    "jpeg75": _jpeg(75),
    "webp80": _webp,
    "resize50": _resize_half,
    "social1080": _screenshot_like,
}


def pipeline_version() -> str:
    """Хеш кода пайплайна и модели: кэш предсказаний инвалидируется при любом изменении."""
    digest = hashlib.sha256()
    for name in PIPELINE_FILES:
        digest.update((ROOT / name).read_bytes())
    for model in sorted((ROOT / "models").glob("*.onnx")):
        digest.update(model.name.encode())
        digest.update(str(model.stat().st_size).encode())
    return digest.hexdigest()[:12]


def _prediction(entry: dict[str, Any], variant: str, result: dict[str, Any], elapsed: float) -> dict[str, Any]:
    breakdown = {item["id"]: item.get("percent") for item in result.get("probability_breakdown", [])}
    local = result.get("local_model") or {}
    final = (result.get("generation_probability") or {}).get("percent")
    return {
        "id": entry["id"],
        "variant": variant,
        "status": "ok",
        "label": entry["label"],
        "final": final / 100 if final is not None else None,
        "local_model": local.get("score") if local.get("status") == "ok" else None,
        "local_model_status": local.get("status"),
        "semantic": breakdown.get("semantic") / 100 if breakdown.get("semantic") is not None else None,
        "external": breakdown.get("external") / 100 if breakdown.get("external") is not None else None,
        "provenance": breakdown.get("provenance") / 100 if breakdown.get("provenance") is not None else None,
        "pixels": breakdown.get("pixels") / 100 if breakdown.get("pixels") is not None else None,
        "verdict": result.get("verdict"),
        "elapsed_s": round(elapsed, 3),
    }


def load_predictions(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    if not path.exists():
        return {}
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {(row["id"], row["variant"]): row for row in rows}


async def run_predictions(
    entries: list[dict[str, Any]],
    variants: list[str],
    predictions_path: Path,
    remote: bool = False,
    limit: int | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Последовательно анализирует изображения; уже посчитанные пары (id, variant) берутся из кэша."""
    from main import run_pipeline  # импорт здесь: main загружает .env и FastAPI-приложение

    cached = load_predictions(predictions_path)
    todo = [(entry, variant) for entry in entries[:limit] for variant in variants if (entry["id"], variant) not in cached]
    print(f"Изображений: {len(entries[:limit])}, вариантов: {len(variants)}, в кэше: {len(cached)}, к расчёту: {len(todo)}")
    predictions_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with predictions_path.open("a", encoding="utf-8") as output:
        for index, (entry, variant) in enumerate(todo, 1):
            source = Path(entry["path"]).read_bytes()
            transform = VARIANTS[variant]
            data, mime_type = transform(source) if transform else (source, Image.MIME.get(Image.open(io.BytesIO(source)).format, "application/octet-stream"))
            begin = time.monotonic()
            try:
                result = await run_pipeline(data, Path(entry["path"]).name, mime_type, remote)
                row = _prediction(entry, variant, result, time.monotonic() - begin)
            except HTTPException as error:
                row = {"id": entry["id"], "variant": variant, "status": "rejected", "label": entry["label"], "detail": error.detail}
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
            output.flush()
            cached[(entry["id"], variant)] = row
            if index % 10 == 0 or index == len(todo):
                rate = (time.monotonic() - started) / index
                print(f"  {index}/{len(todo)} · {rate:.1f} с/изобр. · осталось ~{rate * (len(todo) - index) / 60:.0f} мин", flush=True)
    return cached


def run(entries: list[dict[str, Any]], variants: list[str], predictions_path: Path, remote: bool = False, limit: int | None = None) -> dict[tuple[str, str], dict[str, Any]]:
    return asyncio.run(run_predictions(entries, variants, predictions_path, remote, limit))
