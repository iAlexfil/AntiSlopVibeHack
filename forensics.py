from __future__ import annotations

import base64
import hashlib
import io
import json
import math
from datetime import datetime, timezone
from typing import Any

import numpy as np
from PIL import Image, ImageFilter


MAX_ANALYSIS_EDGE = 1024
MAX_MAP_EDGE = 640
MAX_FFT_EDGE = 512


def _thumbnail(image: Image.Image, edge: int) -> Image.Image:
    preview = image.convert("RGB")
    preview.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    return preview


def _map_data_url(values: np.ndarray, palette: str = "heat") -> str:
    values = np.nan_to_num(values.astype(np.float32), copy=False)
    high = float(np.percentile(values, 99.2)) if values.size else 0.0
    scaled = np.clip(values / (high + 1e-6), 0, 1)
    if palette == "spectrum":
        red = np.clip(scaled * 2.2, 0, 1)
        green = np.clip((scaled - 0.18) * 1.5, 0, 1)
        blue = np.clip((1 - scaled) * 0.45 + scaled * 0.25, 0, 1)
    else:
        red = np.clip(scaled * 2.5, 0, 1)
        green = np.clip((scaled - 0.25) * 2.0, 0, 1)
        blue = np.clip((scaled - 0.75) * 4.0, 0, 1)
    rgb = (np.stack((red, green, blue), axis=-1) * 255).astype(np.uint8)
    output = Image.fromarray(rgb, "RGB")
    output.thumbnail((MAX_MAP_EDGE, MAX_MAP_EDGE), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    output.save(buffer, "JPEG", quality=78, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _ela(image: Image.Image) -> tuple[dict[str, Any], str]:
    preview = _thumbnail(image, MAX_ANALYSIS_EDGE)
    compressed = io.BytesIO()
    preview.save(compressed, "JPEG", quality=90, subsampling=0)
    compressed.seek(0)
    recompressed = Image.open(compressed).convert("RGB")
    source = np.asarray(preview, dtype=np.int16)
    second = np.asarray(recompressed, dtype=np.int16)
    delta = np.max(np.abs(source - second), axis=2).astype(np.float32)
    p95 = float(np.percentile(delta, 95))
    is_jpeg = (image.format or "").upper() in {"JPEG", "JPG"}
    return {
        "id": "ela",
        "name": "ELA",
        "value": round(p95, 2),
        "unit": "p95 delta",
        "status": "review" if is_jpeg and p95 > 18 else "normal" if is_jpeg else "not_applicable",
        "detail": "Неравномерные яркие зоны могут указывать на монтаж или повторное JPEG‑сжатие; это не специфично для ИИ." if is_jpeg else "Исходник не JPEG: карта показана справочно и не участвует в оценке.",
    }, _map_data_url(delta)


def _noise(image: Image.Image) -> tuple[dict[str, Any], str]:
    preview = _thumbnail(image, MAX_ANALYSIS_EDGE).convert("L")
    gray = np.asarray(preview, dtype=np.float32)
    blurred = np.asarray(preview.filter(ImageFilter.GaussianBlur(radius=1.15)), dtype=np.float32)
    residual = np.abs(gray - blurred)
    height, width = residual.shape
    regions = []
    for row in range(4):
        for col in range(4):
            block = residual[row * height // 4:(row + 1) * height // 4, col * width // 4:(col + 1) * width // 4]
            if block.size:
                regions.append(float(np.mean(block)))
    consistency = float(np.std(regions) / (np.mean(regions) + 1e-6)) if regions else 0.0
    return {
        "id": "noise",
        "name": "Шумовой остаток",
        "value": round(consistency, 3),
        "unit": "variation",
        "status": "review" if consistency > 0.7 else "normal",
        "detail": "Сильные различия шума между областями бывают при композитинге, ретуши и генеративном редактировании.",
    }, _map_data_url(residual)


def _frequency(image: Image.Image) -> tuple[dict[str, Any], str]:
    preview = _thumbnail(image, MAX_FFT_EDGE).convert("L")
    gray = np.asarray(preview, dtype=np.float32)
    gray -= float(np.mean(gray))
    window_y = np.hanning(gray.shape[0]).astype(np.float32)
    window_x = np.hanning(gray.shape[1]).astype(np.float32)
    spectrum = np.fft.fftshift(np.fft.fft2(gray * window_y[:, None] * window_x[None, :]))
    magnitude = np.log1p(np.abs(spectrum)).astype(np.float32)
    height, width = magnitude.shape
    cy, cx = height // 2, width // 2
    radius = max(3, min(height, width) // 32)
    magnitude[max(0, cy - radius):cy + radius + 1, max(0, cx - radius):cx + radius + 1] = 0
    positive = magnitude[magnitude > 0]
    peak_ratio = float(np.percentile(positive, 99.85) / (np.median(positive) + 1e-6)) if positive.size else 0.0
    return {
        "id": "frequency",
        "name": "Частотный спектр",
        "value": round(peak_ratio, 2),
        "unit": "peak ratio",
        "status": "review" if peak_ratio > 2.8 else "normal",
        "detail": "Выраженные симметричные пики могут появляться после апскейлеров, периодических текстур и некоторых генераторов.",
    }, _map_data_url(magnitude, "spectrum")


def _repetition(image: Image.Image) -> tuple[dict[str, Any], str]:
    preview = _thumbnail(image, 256).convert("L")
    gray = np.asarray(preview, dtype=np.float32)
    blurred = np.asarray(preview.filter(ImageFilter.GaussianBlur(radius=2)), dtype=np.float32)
    high_pass = gray - blurred
    block_size = 16
    height, width = high_pass.shape
    rows, cols = height // block_size, width // block_size
    if rows == 0 or cols == 0:
        return {
            "id": "repetition", "name": "Повторяющиеся паттерны", "value": 0,
            "unit": "clone ratio", "status": "not_applicable",
            "detail": "Изображение слишком маленькое для блочного сравнения.",
        }, _map_data_url(np.zeros_like(high_pass), "spectrum")
    cropped = high_pass[:rows * block_size, :cols * block_size]
    blocks = cropped.reshape(rows, block_size, cols, block_size).transpose(0, 2, 1, 3).reshape(rows * cols, -1)
    blocks -= np.mean(blocks, axis=1, keepdims=True)
    norms = np.linalg.norm(blocks, axis=1, keepdims=True)
    valid = norms[:, 0] > 1
    blocks /= norms + 1e-6
    similarity = blocks @ blocks.T
    for index in range(rows * cols):
        row, col = divmod(index, cols)
        for near_row in range(max(0, row - 1), min(rows, row + 2)):
            for near_col in range(max(0, col - 1), min(cols, col + 2)):
                similarity[index, near_row * cols + near_col] = -1
    best = np.max(similarity, axis=1)
    best[~valid] = 0
    repeat_score = float(np.mean(best > 0.96))
    block_map = best.reshape(rows, cols)
    repetition_map = np.repeat(np.repeat(block_map, block_size, axis=0), block_size, axis=1)
    return {
        "id": "repetition",
        "name": "Повторяющиеся паттерны",
        "value": round(repeat_score, 3),
        "unit": "autocorrelation",
        "status": "review" if repeat_score > 0.12 else "normal",
        "detail": "Доля удалённых блоков с почти идентичным высокочастотным рисунком; также реагирует на плитку и орнаменты.",
    }, _map_data_url(repetition_map, "spectrum")


def _hashes(image: Image.Image, raw: bytes) -> dict[str, str]:
    small = image.convert("L").resize((32, 32), Image.Resampling.LANCZOS)
    values = np.asarray(small, dtype=np.float32)
    size = 32
    positions = np.arange(size, dtype=np.float32)
    frequencies = positions[:, None]
    transform = np.cos((math.pi / size) * (positions + 0.5) * frequencies)
    transform[0] *= 1 / math.sqrt(2)
    transform *= math.sqrt(2 / size)
    dct = transform @ values @ transform.T
    low = dct[:8, :8].copy()
    median = float(np.median(low.flat[1:]))
    bits = (low > median).flatten()
    phash = f"{sum(int(bit) << index for index, bit in enumerate(bits)):016x}"

    dimage = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    dvalues = np.asarray(dimage)
    dbits = (dvalues[:, 1:] > dvalues[:, :-1]).flatten()
    dhash = f"{sum(int(bit) << index for index, bit in enumerate(dbits)):016x}"
    return {"sha256": hashlib.sha256(raw).hexdigest(), "phash": phash, "dhash": dhash}


def _exif_checks(metadata: dict[str, str]) -> list[dict[str, str]]:
    lower = {key.lower(): value for key, value in metadata.items()}
    make = next((value for key, value in lower.items() if key.endswith(":make")), None)
    model = next((value for key, value in lower.items() if key.endswith(":model")), None)
    software = next((value for key, value in lower.items() if "software" in key), None)
    captured = next((value for key, value in lower.items() if "datetimeoriginal" in key), None)
    checks = []
    if make or model:
        checks.append({"name": "Камера", "status": "present", "detail": " / ".join(filter(None, (make, model)))})
    else:
        checks.append({"name": "Камера", "status": "absent", "detail": "Make/Model отсутствуют — это нормально для пересохранённых файлов."})
    if software:
        checks.append({"name": "Software", "status": "present", "detail": software[:160]})
    if captured:
        status = "present"
        detail = captured[:160]
        try:
            parsed = datetime.strptime(captured[:19], "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if parsed > datetime.now(timezone.utc).replace(microsecond=0):
                status, detail = "review", f"Дата съёмки находится в будущем: {captured[:19]}"
        except ValueError:
            pass
        checks.append({"name": "Дата съёмки", "status": status, "detail": detail})
    return checks


def _walk_validation(value: Any, found: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        if "code" in value and isinstance(value.get("code"), str):
            found.append({"code": value["code"], "success": value.get("success")})
        for item in value.values():
            _walk_validation(item, found)
    elif isinstance(value, list):
        for item in value:
            _walk_validation(item, found)


def _generative_c2pa_actions(manifest: dict[str, Any]) -> bool:
    source_types = (
        "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia",
        "http://cv.iptc.org/newscodes/digitalsourcetype/compositeWithTrainedAlgorithmicMedia",
    )
    assertions = manifest.get("assertions")
    if not isinstance(assertions, list):
        return False
    for assertion in assertions:
        if not isinstance(assertion, dict) or assertion.get("label") not in {"c2pa.actions", "c2pa.actions.v2"}:
            continue
        data = assertion.get("data")
        if not isinstance(data, dict):
            continue
        actions = data.get("actions", [])
        templates = data.get("templates", []) if assertion["label"] == "c2pa.actions.v2" else []
        if not isinstance(actions, list) or not isinstance(templates, list):
            continue
        for action in actions:
            if not isinstance(action, dict) or not isinstance(action.get("action"), str):
                continue
            source_type = None
            # C2PA v2 applies wildcard templates, named templates, then the action's own fields.
            for name in ("*", action["action"]):
                for template in templates:
                    if isinstance(template, dict) and template.get("action") == name and "digitalSourceType" in template:
                        source_type = template["digitalSourceType"]
            if action.get("digitalSourceType", source_type) in source_types:
                return True
    return False


def verify_c2pa(raw: bytes, mime_type: str) -> dict[str, Any]:
    try:
        import c2pa
    except ImportError:
        return {"status": "unavailable", "found": False, "detail": "Установите c2pa-python"}

    try:
        settings = c2pa.Settings.from_dict({"verify": {"remote_manifest_fetch": False}})
        with c2pa.Context(settings) as context:
            with c2pa.Reader(mime_type, io.BytesIO(raw), context=context) as reader:
                store = json.loads(reader.json())
    except Exception as error:
        if "ManifestNotFound" in type(error).__name__ or "no JUMBF data" in str(error):
            return {"status": "none", "found": False, "detail": "Подписанный C2PA-манифест не найден"}
        return {"status": "error", "found": False, "detail": str(error)[:240]}

    active_label = store.get("active_manifest")
    manifest = (store.get("manifests") or {}).get(active_label, {})
    validations: list[dict[str, Any]] = []
    _walk_validation(store.get("validation_status") or store.get("validation_results") or manifest, validations)
    codes = [item["code"] for item in validations]
    failures = [
        item for item in validations
        if item.get("success") is False or any(token in item["code"].lower() for token in ("invalid", "mismatch", "malformed"))
    ]
    untrusted = [item for item in validations if "untrusted" in item["code"].lower()]
    signature_valid = any(code == "claimSignature.validated" for code in codes)
    generative = _generative_c2pa_actions(manifest)
    claim_info = manifest.get("claim_generator_info") or []
    generator = claim_info[0].get("name") if claim_info and isinstance(claim_info[0], dict) else manifest.get("claim_generator")
    status = "invalid" if failures else "untrusted" if untrusted else "valid" if signature_valid else "present"
    return {
        "status": status,
        "found": True,
        "signature_valid": signature_valid,
        "generative": generative,
        "generator": generator,
        "active_manifest": active_label,
        "validation_codes": codes[:16],
        "detail": "Подпись подтверждена" if signature_valid else "Манифест найден, но доверенная цепочка сертификатов не подтверждена" if untrusted else "Манифест найден; статус подписи не определён",
    }


def run_forensics(image: Image.Image, raw: bytes, metadata: dict[str, str]) -> dict[str, Any]:
    mime_type = Image.MIME.get(image.format or "", "application/octet-stream")
    ela, ela_map = _ela(image)
    noise, noise_map = _noise(image)
    frequency, frequency_map = _frequency(image)
    repetition, repetition_map = _repetition(image)
    edge = min(MAX_ANALYSIS_EDGE, max(image.width, image.height))
    # All numerical stages are downscaled first. Peak working set stays well below 128 MB.
    estimated_peak_mb = round((edge * edge * 4 * 8) / (1024 * 1024), 1)
    return {
        "signals": [ela, noise, frequency, repetition],
        "maps": [
            {"id": "ela", "name": "ELA", "image": ela_map},
            {"id": "noise", "name": "Шум", "image": noise_map},
            {"id": "frequency", "name": "Спектр", "image": frequency_map},
            {"id": "repetition", "name": "Повторы", "image": repetition_map},
        ],
        "hashes": _hashes(image, raw),
        "exif_checks": _exif_checks(metadata),
        "c2pa": verify_c2pa(raw, mime_type),
        "resource_budget": {
            "max_analysis_edge": MAX_ANALYSIS_EDGE,
            "max_fft_edge": MAX_FFT_EDGE,
            "estimated_peak_mb": estimated_peak_mb,
            "ml_model_loaded": False,
        },
    }
