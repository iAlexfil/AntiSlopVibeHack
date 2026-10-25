from __future__ import annotations

import asyncio
import io
import re
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import ExifTags, Image, UnidentifiedImageError
from dotenv import load_dotenv

from forensics import run_forensics
from local_model import run_local_model
from services import analyze_with_services, merge_service_results, provider_catalog
from chatgpt_auth import status as vision_auth_status


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000

load_dotenv(ROOT / ".env")

app = FastAPI(title="AntiSlop", version="0.2.0")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
LOCAL_MODEL_GATE = asyncio.Semaphore(1)


PROVIDER_MARKERS: dict[str, tuple[str, ...]] = {
    "Midjourney": ("midjourney", "midjourney bot"),
    "OpenAI / DALL·E": ("dall-e", "dall·e", "openai image", "gpt-image"),
    "Stable Diffusion": ("stable diffusion", "automatic1111", "comfyui", "invokeai", "sdxl", "fooocus"),
    "Adobe Firefly": ("adobe firefly", "firefly"),
    "Google Imagen / Gemini": ("google imagen", "imagen 3", "imagen 4", "gemini"),
    "Ideogram": ("ideogram",),
    "Leonardo AI": ("leonardo.ai", "leonardo ai"),
    "Runway": ("runwayml", "runway ai"),
    "NovelAI": ("novelai",),
    "Flux": ("black forest labs", "flux.1", "flux1"),
    "Canva AI": ("canva ai", "magic media"),
    "Microsoft Designer": ("microsoft designer", "bing image creator"),
    "NightCafe": ("nightcafe",),
    "Playground AI": ("playground ai",),
}

GENERATION_TERMS = (
    "ai generated", "ai-generated", "generated with ai", "generative ai", "text-to-image",
    "txt2img", "img2img", "negative prompt", "sampler", "cfg scale", "seed", "steps",
    "model hash", "checkpoint",
)

CREDENTIAL_MARKERS = (
    "c2pa", "content credentials", "contentauth", "jumbf", "manifest_store",
    "adobe:urn:aaid:sc:us",
)


def _flatten(value: Any) -> list[str]:
    if isinstance(value, bytes):
        return [value.decode("utf-8", errors="ignore")]
    if isinstance(value, (str, int, float, bool)):
        return [str(value)]
    if isinstance(value, dict):
        result: list[str] = []
        for key, item in value.items():
            result.extend((str(key), *_flatten(item)))
        return result
    if isinstance(value, (list, tuple, set)):
        result = []
        for item in value:
            result.extend(_flatten(item))
        return result
    return []


def _metadata(image: Image.Image) -> tuple[dict[str, str], str]:
    result: dict[str, str] = {}
    for key, value in image.info.items():
        if key.lower() in {"icc_profile", "exif"}:
            continue
        flat = " · ".join(_flatten(value))
        if flat:
            result[str(key)] = flat[:1200]
    try:
        for key, value in image.getexif().items():
            label = ExifTags.TAGS.get(key, str(key))
            flat = " · ".join(_flatten(value))
            if flat:
                result[f"EXIF:{label}"] = flat[:1200]
    except (AttributeError, TypeError, ValueError):
        pass
    searchable = "\n".join(f"{key}: {value}" for key, value in result.items())
    return result, searchable


def _binary_text(data: bytes) -> str:
    # Манифесты и параметры генерации часто лежат в читаемых блоках PNG/JPEG/WebP.
    chunks = re.findall(rb"[\x20-\x7e]{4,}", data)
    ascii_text = "\n".join(chunk.decode("utf-8", errors="ignore") for chunk in chunks)
    utf16_text = data.decode("utf-16-le", errors="ignore")
    return f"{ascii_text}\n{utf16_text[:400_000]}"[:1_200_000]


def _corner_badge_signal(image: Image.Image) -> dict[str, Any]:
    preview = image.convert("L")
    preview.thumbnail((800, 800))
    values = np.asarray(preview, dtype=np.float32) / 255.0
    height, width = values.shape
    if height < 80 or width < 80:
        return {"detected": False, "score": 0.0, "corner": None}
    edge_x = np.abs(np.diff(values, axis=1, prepend=values[:, :1]))
    edge_y = np.abs(np.diff(values, axis=0, prepend=values[:1, :]))
    edges = edge_x + edge_y
    crop_h, crop_w = max(24, height // 5), max(24, width // 5)
    corners = {
        "сверху слева": edges[:crop_h, :crop_w],
        "сверху справа": edges[:crop_h, -crop_w:],
        "снизу слева": edges[-crop_h:, :crop_w],
        "снизу справа": edges[-crop_h:, -crop_w:],
    }
    center = edges[height // 4:height * 3 // 4, width // 4:width * 3 // 4]
    baseline = float(np.mean(center)) + 1e-6
    name, region = max(corners.items(), key=lambda item: float(np.mean(item[1])))
    ratio = float(np.mean(region)) / baseline
    detected = ratio > 2.55 and float(np.mean(region)) > 0.095
    return {"detected": detected, "score": round(ratio, 2), "corner": name if detected else None}


def analyze_image(data: bytes, filename: str) -> dict[str, Any]:
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(413, "Файл больше 20 МБ")
    if not data:
        raise HTTPException(400, "Пустой файл")
    try:
        image = Image.open(io.BytesIO(data))
        if image.width * image.height > MAX_IMAGE_PIXELS:
            raise HTTPException(413, "Изображение больше 40 мегапикселей")
        image.load()
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise HTTPException(415, "Это не поддерживаемое изображение") from error

    metadata, metadata_text = _metadata(image)
    forensics = run_forensics(image, data, metadata)
    haystack = f"{metadata_text}\n{_binary_text(data)}".lower()
    findings: list[dict[str, Any]] = []
    providers: list[str] = []

    credential_hits = sorted({marker for marker in CREDENTIAL_MARKERS if marker in haystack})
    if credential_hits:
        findings.append({
            "kind": "credentials", "level": "info", "title": "Найдены Content Credentials / C2PA",
            "detail": "В контейнере есть признаки манифеста происхождения: " + ", ".join(credential_hits), "weight": 3,
        })
    c2pa_result = forensics["c2pa"]
    if c2pa_result.get("generative"):
        findings.append({
            "kind": "c2pa_generative", "level": "strong", "title": "C2PA указывает на генеративный источник",
            "detail": "Подписанный манифест содержит тип источника trained algorithmic media.", "weight": 70,
        })
    elif c2pa_result.get("status") == "invalid":
        findings.append({
            "kind": "c2pa_invalid", "level": "weak", "title": "Проблема проверки C2PA",
            "detail": "Манифест найден, но содержит ошибки валидации или недоверенную цепочку.", "weight": 10,
        })
    for provider, markers in PROVIDER_MARKERS.items():
        hit = next((marker for marker in markers if marker in haystack), None)
        if hit:
            providers.append(provider)
            findings.append({
                "kind": "provider", "level": "strong", "title": provider,
                "detail": f"Сигнатура «{hit}» найдена в метаданных или структуре файла.", "weight": 46,
            })
    term_hits = sorted({term for term in GENERATION_TERMS if term in haystack})
    if term_hits:
        findings.append({
            "kind": "generation_metadata", "level": "strong", "title": "Параметры генерации",
            "detail": "Найдены характерные поля: " + ", ".join(term_hits[:7]), "weight": 42,
        })
    software = next((value for key, value in metadata.items() if "software" in key.lower()), None)
    if software and not any(item["kind"] == "provider" for item in findings):
        findings.append({
            "kind": "software", "level": "info", "title": "Указан редактор изображения",
            "detail": software[:180], "weight": 4,
        })
    corner = _corner_badge_signal(image)
    if corner["detected"]:
        findings.append({
            "kind": "corner", "level": "weak", "title": "Возможный видимый бейдж",
            "detail": f"Повышенная детализация {corner['corner']}. Проверьте этот участок на превью.", "weight": 12,
        })
    strong = [item for item in findings if item["level"] == "strong"]
    weak = [item for item in findings if item["level"] == "weak"]
    score = min(99, sum(int(item["weight"]) for item in findings))
    if strong:
        verdict, headline, confidence = "detected", "Есть признаки AI‑происхождения", max(72, score)
    elif weak:
        verdict, headline, confidence = "review", "Нужна ручная проверка", min(58, max(24, score))
    else:
        verdict, headline, confidence = "clear", "Явные водяные знаки не найдены", 0

    return {
        "filename": filename, "format": image.format or "unknown", "width": image.width,
        "height": image.height, "mode": image.mode, "size_bytes": len(data), "verdict": verdict,
        "headline": headline, "confidence": confidence, "providers": providers, "findings": findings,
        "forensics": forensics,
        "metadata": [{"key": key, "value": value[:260]} for key, value in list(metadata.items())[:12]],
        "checks": [
            {"name": "EXIF / XMP / IPTC", "status": "done"},
            {"name": "PNG text / JPEG comments", "status": "done"},
            {"name": "C2PA / Content Credentials", "status": "done"},
            {"name": "Сигнатуры AI‑сервисов", "status": "done"},
            {"name": "Угловые бейджи", "status": "manual"},
            {"name": "Закрытые невидимые метки", "status": "unavailable"},
        ],
    }


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/providers")
async def providers() -> list[dict[str, Any]]:
    return provider_catalog()


@app.get("/api/vision/auth")
async def vision_auth() -> dict[str, Any]:
    return vision_auth_status()


async def _read_limited_body(request: Request) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_FILE_SIZE:
            raise HTTPException(413, "Файл больше 20 МБ")
        chunks.append(chunk)
    return b"".join(chunks)


async def _run_local_model_limited(data: bytes) -> dict[str, Any]:
    # Не допускаем несколько 200+ МБ ONNX-воркеров одновременно.
    async with LOCAL_MODEL_GATE:
        return await asyncio.to_thread(run_local_model, data)


@app.post("/api/analyze")
async def analyze(request: Request, filename: str = "image", remote: bool = True) -> JSONResponse:
    content_length = request.headers.get("content-length")
    try:
        if content_length and int(content_length) > MAX_FILE_SIZE:
            raise HTTPException(413, "Файл больше 20 МБ")
    except ValueError as error:
        raise HTTPException(400, "Некорректный Content-Length") from error
    data = await _read_limited_body(request)
    result = analyze_image(data, filename)
    local_model_task = _run_local_model_limited(data)
    if remote:
        mime_type = request.headers.get("content-type", "application/octet-stream").split(";", 1)[0]
        local_result, api_results = await asyncio.gather(
            local_model_task,
            analyze_with_services(data, filename, mime_type),
        )
    else:
        local_result = await local_model_task
        api_results = []
    result["local_model"] = local_result
    result = merge_service_results(result, [local_result, *api_results])
    return JSONResponse(result)


@app.exception_handler(HTTPException)
async def http_error(_: Request, error: HTTPException) -> JSONResponse:
    return JSONResponse({"detail": error.detail}, status_code=error.status_code)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
