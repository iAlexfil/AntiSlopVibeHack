from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from PIL import Image, ImageOps

import chatgpt_auth
from vision_contract import VISION_PROMPT, VisualAnalysis


ProviderResult = dict[str, Any]
ProviderCall = Callable[[httpx.AsyncClient, bytes, str, str], Awaitable[ProviderResult]]

TIMEOUT_SECONDS = 25.0
VISION_GATE = asyncio.Semaphore(1)

PROVIDER_CATALOG = (
    {
        "id": "openai_vision",
        "name": "ChatGPT Vision",
        "env": (),
        "docs": "https://developers.openai.com/siwc/token-sharing-open-source",
    },
    {
        "id": "hive",
        "name": "Hive",
        "env": ("HIVE_API_KEY",),
        "docs": "https://docs.thehive.ai/reference/ai-generated-image-and-video-detection-1",
    },
    {
        "id": "sightengine",
        "name": "Sightengine",
        "env": ("SIGHTENGINE_API_USER", "SIGHTENGINE_API_SECRET"),
        "docs": "https://sightengine.com/docs/ai-generated-image-detection",
    },
    {
        "id": "aiornot",
        "name": "AI or Not",
        "env": ("AIORNOT_API_KEY",),
        "docs": "https://docs.aiornot.com/api-reference/reports-by-modality/image",
    },
    {
        "id": "wasitai",
        "name": "WasItAI",
        "env": ("WASITAI_API_KEY",),
        "docs": "https://wasitai.com/api/documentation",
    },
    {
        "id": "illuminarty",
        "name": "Illuminarty",
        "env": (),
        "docs": "https://illuminarty.ai/",
        "unavailable": "Нет публичного контракта API — доступ выдаётся по договорённости.",
    },
    {
        "id": "synthid",
        "name": "OpenAI SynthID / C2PA",
        "env": ("OPENAI_API_KEY",),
        "docs": "https://developers.openai.com/api/docs/guides/content-provenance",
    },
)


def provider_catalog() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for provider in PROVIDER_CATALOG:
        required = provider["env"]
        configured = bool(required) and all(os.getenv(key) for key in required)
        auth = chatgpt_auth.status() if provider["id"] == "openai_vision" else None
        if auth:
            configured = auth["configured"]
        result.append({
            "id": provider["id"],
            "name": provider["name"],
            "configured": configured,
            "docs": provider["docs"],
            "required_env": list(required),
            "unavailable": provider.get("unavailable"),
            "detail": auth.get("detail") if auth else None,
        })
    return result


def _best_generator(generators: dict[str, Any] | None) -> str | None:
    if not generators:
        return None
    scores: list[tuple[str, float]] = []
    for name, value in generators.items():
        if isinstance(value, dict):
            value = value.get("confidence", 0)
        if isinstance(value, (int, float)):
            scores.append((name, float(value)))
    if not scores:
        return None
    name, score = max(scores, key=lambda item: item[1])
    return name.replace("_", " ") if score >= 0.2 else None


def _normalized(name: str, score: float, generator: str | None = None, detail: str | None = None) -> ProviderResult:
    score = max(0.0, min(1.0, float(score)))
    return {
        "name": name,
        "status": "ok",
        "score": round(score, 4),
        "verdict": "ai" if score >= 0.5 else "human",
        "generator": generator,
        "detail": detail or ("Вероятно AI" if score >= 0.5 else "Вероятно оригинал"),
    }


def _vision_data_url(data: bytes) -> str:
    with Image.open(io.BytesIO(data)) as image:
        image.thumbnail((1280, 1280))
        image = ImageOps.exif_transpose(image).convert("RGB")
        output = io.BytesIO()
        image.save(output, "JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")


async def _vision_stream(response: httpx.Response) -> str:
    event_lines = []
    text_parts: dict[tuple[int, int], str] = {}
    total = 0
    async for line in response.aiter_lines():
        total += len(line.encode("utf-8"))
        if total > 2 * 1024 * 1024:
            raise ValueError("Vision stream is too large")
        if line.startswith("data:"):
            event_lines.append(line[5:].lstrip(" "))
        elif not line and event_lines:
            raw = "\n".join(event_lines)
            event_lines = []
            if raw == "[DONE]":
                break
            event = json.loads(raw)
            kind = event.get("type")
            if kind in {"response.output_text.delta", "response.output_text.done"}:
                key = (event.get("output_index", 0), event.get("content_index", 0))
                if any(type(index) is not int or index < 0 for index in key):
                    raise ValueError("Invalid Vision text index")
                text = event["delta" if kind == "response.output_text.delta" else "text"]
                if not isinstance(text, str):
                    raise ValueError("Invalid Vision text")
                text_parts[key] = text_parts.get(key, "") + text if kind == "response.output_text.delta" else text
            if kind in {"response.failed", "response.incomplete", "error"}:
                code = event.get("code") or ((event.get("response") or {}).get("error") or event.get("error") or {}).get("code")
                if code in {"subscription_sharing_usage_limit_exceeded", "subscription_sharing_usage_unavailable"}:
                    raise ValueError("Лимит плана ChatGPT для AntiSlop недоступен или исчерпан. Платный API не использован.")
                raise ValueError("OpenAI Vision не завершил анализ. Повторите позже.")
            if kind == "response.completed":
                completed = event["response"]
                if completed.get("status") != "completed":
                    raise ValueError("OpenAI Vision не завершил анализ. Повторите позже.")
                texts = [part["text"] for item in completed.get("output", []) if item.get("type") == "message"
                         for part in item.get("content", []) if part.get("type") == "output_text"]
                # Plan OAuth streams may omit output from the completed event.
                return "".join(texts) if texts else "".join(text_parts[key] for key in sorted(text_parts))
    raise ValueError("Поток OpenAI Vision прерван до завершения анализа")


async def _openai_vision(client: httpx.AsyncClient, data: bytes, filename: str, mime_type: str) -> ProviderResult:
    del filename, mime_type
    model = os.getenv("CHATGPT_VISION_MODEL", "gpt-6-luna")
    async with asyncio.timeout(90), VISION_GATE:
        auth = chatgpt_auth.ChatGPTAuth(chatgpt_auth.DIRECTORY / "credentials.json")
        try:
            token = await auth.access_token()
        finally:
            await auth.close()
        payload = {
            "model": model, "store": False, "stream": True,
            "instructions": VISION_PROMPT,
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": "Проверь изображение и укажи наблюдаемые признаки."},
                {"type": "input_image", "image_url": await asyncio.to_thread(_vision_data_url, data)},
            ]}],
            "text": {"format": {"type": "json_schema", "name": "visual_analysis", "strict": True,
                                "schema": VisualAnalysis.model_json_schema()}},
        }
        async with client.stream("POST", "https://api.openai.com/v1/responses",
                                 headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=60) as response:
            response.raise_for_status()
            raw = await _vision_stream(response)
    analysis = VisualAnalysis.model_validate_json(raw).model_dump()
    score = analysis["probability"]
    result = (_normalized("ChatGPT Vision", score / 100, detail=analysis["explanation"])
              if score is not None else {"name": "ChatGPT Vision", "status": "ok", "score": None,
                                         "verdict": "unknown", "detail": analysis["explanation"]})
    result.update(model=model, semantic=True, analysis=analysis, reasoning_summary=analysis["explanation"], auth_mode="chatgpt_plan")
    return result


async def _openai_provenance(client: httpx.AsyncClient, data: bytes, filename: str, mime_type: str) -> ProviderResult:
    del filename, mime_type
    name = "OpenAI SynthID / C2PA"
    with Image.open(io.BytesIO(data)) as image:
        image_format = image.format
    media_types = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
    if image_format not in media_types:
        return {
            "name": name, "status": "unsupported", "provenance": True,
            "detail": "Проверка SynthID / C2PA через OpenAI поддерживает только исходные PNG, JPEG и WebP.",
        }

    # Проверяем оригинальные байты: ресайз и перекодирование могут уничтожить метки.
    response = await client.post(
        "https://api.openai.com/v1/content_provenance_checks",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        files={"file": (f"image.{image_format.lower()}", data, media_types[image_format])},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("object") != "content_provenance_check" or not isinstance(payload.get("results"), list):
        raise ValueError("OpenAI вернул некорректный ответ проверки происхождения")

    checks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in payload["results"]:
        if not isinstance(item, dict):
            raise ValueError("OpenAI вернул некорректную проверку происхождения")
        signal_type = item.get("type")
        if signal_type not in ("synthid", "c2pa"):
            continue
        if signal_type in seen or item.get("outcome") not in ("detected", "not_detected"):
            raise ValueError("OpenAI вернул некорректный результат проверки происхождения")
        seen.add(signal_type)
        check = {"type": signal_type, "outcome": item["outcome"]}
        fields = ["model", "generated_at"]
        if signal_type == "c2pa":
            state = item.get("validation_state")
            if state not in ("trusted", "valid", "invalid", "not_present") or (item["outcome"] == "detected" and state not in ("trusted", "valid")):
                raise ValueError("OpenAI вернул противоречивый результат C2PA")
            check["validation_state"] = state
            fields.append("issuer")
        for field in fields:
            value = item.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError("OpenAI вернул некорректные сведения о происхождении")
            check[field] = value[:240] if value is not None else None
        checks.append(check)
    if not checks:
        raise ValueError("OpenAI не вернул применимых проверок SynthID / C2PA")

    detected = any(item["outcome"] == "detected" for item in checks)
    descriptions = [
        f"{'SynthID' if item['type'] == 'synthid' else 'C2PA'}: "
        + ("обнаружен" if item["outcome"] == "detected" else "не обнаружен")
        for item in checks
    ]
    return {
        "name": name, "status": "ok", "score": None,
        "verdict": "ai" if detected else "unknown", "provenance": True,
        "checks": checks,
        "detail": "; ".join(descriptions) + ". Проверяются только поддерживаемые метки OpenAI; отсутствие меток не исключает AI-генерацию.",
    }


async def _hive(client: httpx.AsyncClient, data: bytes, filename: str, mime_type: str) -> ProviderResult:
    response = await client.post(
        "https://api.thehive.ai/api/v2/task/sync",
        headers={"Authorization": f"Token {os.environ['HIVE_API_KEY']}"},
        files={"media": (filename, data, mime_type)},
    )
    response.raise_for_status()
    payload = response.json()
    entries = payload.get("status") or []
    output = (((entries[0] if entries else {}).get("response") or {}).get("output") or [{}])[0]
    classes = output.get("classes") or []
    scores = {item.get("class"): float(item.get("score", 0)) for item in classes if item.get("class")}
    score = scores.get("ai_generated", 0.0)
    ignored = {"ai_generated", "not_ai_generated", "none", "inconclusive", "inconclusive_video", "deepfake"}
    sources = {name: value for name, value in scores.items() if name not in ignored}
    return _normalized("Hive", score, _best_generator(sources))


async def _sightengine(client: httpx.AsyncClient, data: bytes, filename: str, mime_type: str) -> ProviderResult:
    response = await client.post(
        "https://api.sightengine.com/1.0/check.json",
        data={
            "models": "genai",
            "api_user": os.environ["SIGHTENGINE_API_USER"],
            "api_secret": os.environ["SIGHTENGINE_API_SECRET"],
        },
        files={"media": (filename, data, mime_type)},
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        raise ValueError("Sightengine вернул статус failure")
    result = payload.get("type") or {}
    return _normalized(
        "Sightengine",
        float(result.get("ai_generated", 0)),
        _best_generator(result.get("ai_generators")),
    )


async def _aiornot(client: httpx.AsyncClient, data: bytes, filename: str, mime_type: str) -> ProviderResult:
    response = await client.post(
        "https://api.aiornot.com/v2/image/sync",
        headers={"Authorization": f"Bearer {os.environ['AIORNOT_API_KEY']}"},
        params={"only": "ai_generated"},
        files={"image": (filename, data, mime_type)},
    )
    response.raise_for_status()
    report = (response.json().get("report") or {}).get("ai_generated") or {}
    ai = report.get("ai") or {}
    score = float(ai.get("confidence", 0))
    verdict = report.get("verdict")
    if verdict == "unknown":
        result = _normalized("AI or Not", score, _best_generator(report.get("generator")), "Сервис не уверен")
        result["verdict"] = "unknown"
        return result
    return _normalized("AI or Not", score, _best_generator(report.get("generator")))


async def _wasitai(client: httpx.AsyncClient, data: bytes, filename: str, mime_type: str) -> ProviderResult:
    response = await client.post(
        "https://api.wasitai.com/live/api/classifiers/v1/image/classify/upload-file",
        headers={"api-key": os.environ["WASITAI_API_KEY"]},
        files={"file": (filename, data, mime_type)},
    )
    response.raise_for_status()
    classified = response.json().get("classifiedImage") or {}
    if classified.get("status") == "error":
        status = classified.get("statusInfo") or {}
        raise ValueError(status.get("description") or "WasItAI не смог обработать изображение")
    basic = classified.get("classifyResult") or {}
    is_ai = classified.get("finalVerdictWasItAI")
    if is_ai is None:
        is_ai = basic.get("result") == "ai"
    confidence = classified.get("finalVerdictConfidenceScore")
    if confidence is None:
        confidence = basic.get("confidenceScore", 0)
    confidence = float(confidence) / 100
    score = confidence if is_ai else 1 - confidence
    detail = classified.get("finalVerdictHumanFriendlyDescription") or basic.get("humanFriendlyDescription")
    return _normalized("WasItAI", score, detail=detail)


RUNNERS: dict[str, ProviderCall] = {
    "openai_vision": _openai_vision,
    "synthid": _openai_provenance,
    "hive": _hive,
    "sightengine": _sightengine,
    "aiornot": _aiornot,
    "wasitai": _wasitai,
}


async def _run_one(
    provider_id: str,
    runner: ProviderCall,
    client: httpx.AsyncClient,
    data: bytes,
    filename: str,
    mime_type: str,
) -> ProviderResult:
    started = time.monotonic()
    name = next(item["name"] for item in PROVIDER_CATALOG if item["id"] == provider_id)
    try:
        result = await runner(client, data, filename, mime_type)
        result["latency_ms"] = round((time.monotonic() - started) * 1000)
        result["id"] = provider_id
        return result
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, TimeoutError) as error:
        status = "error"
        detail = str(error)[:240] or "Ошибка API"
        if provider_id == "openai_vision":
            if isinstance(error, (httpx.TimeoutException, TimeoutError)):
                detail = "Истекло время смыслового анализа. Повторите позже."
            elif isinstance(error, httpx.HTTPStatusError):
                detail = f"OpenAI Vision отклонил запрос (HTTP {error.response.status_code}). Проверьте доступ и лимиты выбранного способа входа."
            elif isinstance(error, httpx.HTTPError):
                detail = "Не удалось связаться с OpenAI Vision."
            elif not isinstance(error, chatgpt_auth.AuthError) and str(error) not in {
                "Лимит плана ChatGPT для AntiSlop недоступен или исчерпан. Платный API не использован.",
                "OpenAI Vision не завершил анализ. Повторите позже.",
                "Поток OpenAI Vision прерван до завершения анализа",
            }:
                detail = "OpenAI Vision вернул некорректный или незавершённый результат."
        if provider_id == "synthid":
            if isinstance(error, httpx.HTTPStatusError):
                code = error.response.status_code
                detail = {
                    400: "OpenAI отклонил файл: он повреждён, не поддерживается или заблокирован.",
                    401: "OpenAI не принял API-ключ. Проверьте серверную настройку OPENAI_API_KEY.",
                    403: "OpenAI запретил доступ к проверке происхождения для этого проекта.",
                    404: "Проверка происхождения OpenAI недоступна этой организации.",
                    429: "Достигнут лимит проверок происхождения OpenAI. Повторите позже.",
                }.get(code, f"Проверка происхождения OpenAI временно недоступна (HTTP {code}).")
                status = "unavailable" if code == 404 else "error"
            elif isinstance(error, httpx.TimeoutException):
                detail = "OpenAI не ответил на проверку происхождения за отведённое время."
            elif isinstance(error, httpx.HTTPError):
                detail = "Не удалось связаться с OpenAI для проверки происхождения."
            else:
                detail = "OpenAI вернул некорректный результат проверки происхождения."
        return {
            "id": provider_id,
            "name": name,
            "status": status,
            "detail": detail,
            "latency_ms": round((time.monotonic() - started) * 1000),
        }


async def analyze_with_services(data: bytes, filename: str, mime_type: str) -> list[ProviderResult]:
    configured = {item["id"] for item in provider_catalog() if item["configured"]}
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
        tasks = [
            _run_one(provider_id, runner, client, data, filename, mime_type)
            for provider_id, runner in RUNNERS.items()
            if provider_id in configured
        ]
        completed = await asyncio.gather(*tasks) if tasks else []

    by_id = {item["id"]: item for item in completed}
    results: list[ProviderResult] = []
    for provider in provider_catalog():
        provider_id = provider["id"]
        if provider_id in by_id:
            results.append(by_id[provider_id])
        elif provider.get("unavailable"):
            results.append({
                "id": provider_id,
                "name": provider["name"],
                "status": "unavailable",
                "detail": provider["unavailable"],
            })
        else:
            results.append({
                "id": provider_id,
                "name": provider["name"],
                "status": "not_configured",
                "detail": provider.get("detail") or "Добавьте ключи в .env",
            })
    return results


def _confirmed_provenance(local: dict[str, Any], results: list[ProviderResult]) -> str | None:
    remote = next((item for item in results if item.get("id") == "synthid" and item.get("status") == "ok"), None)
    checks = remote.get("checks", []) if remote else []
    if any(check.get("type") == "synthid" and check.get("outcome") == "detected" for check in checks):
        return "SynthID"
    if any(check.get("type") == "c2pa" and check.get("outcome") == "detected"
           and check.get("validation_state") in {"trusted", "valid"} for check in checks):
        return "C2PA (OpenAI)"
    c2pa = (local.get("forensics") or {}).get("c2pa") or {}
    if c2pa.get("status") == "valid" and c2pa.get("signature_valid") is True and c2pa.get("generative") is True:
        return "C2PA"
    return None


def merge_service_results(local: dict[str, Any], api_results: list[ProviderResult]) -> dict[str, Any]:
    completed = [item for item in api_results if item.get("status") == "ok"]
    successful = [item for item in completed if item.get("id") != "synthid" and not item.get("provenance") and item.get("verdict") != "unknown"]
    failed = [item for item in api_results if item.get("status") == "error"]
    decisive_source = _confirmed_provenance(local, api_results)
    scores = [float(item["score"]) for item in successful]
    provenance_score: float | None = None
    finding_kinds = {item.get("kind") for item in local.get("findings", []) if item.get("level") == "strong"}
    if finding_kinds & {"provider", "generation_metadata"}:
        provenance_score = 0.95

    probability_inputs = [0.99] if decisive_source else list(scores)
    if not decisive_source and provenance_score is not None:
        probability_inputs.append(provenance_score)
    consensus = sum(probability_inputs) / len(probability_inputs) if probability_inputs else None
    ai_votes = sum(score >= 0.5 for score in scores)
    local["api_results"] = api_results
    local["api_summary"] = {
        "checked": len(completed),
        "failed": len(failed),
        "configured": sum(item.get("status") not in {"not_configured", "unavailable"} or item.get("latency_ms") is not None for item in api_results),
        "score": round(consensus, 4) if consensus is not None else None,
        "ai_votes": sum(item.get("verdict") == "ai" for item in completed),
    }

    source_names = [str(item.get("name", "детектор")) for item in successful]
    if provenance_score is not None:
        source_names.append("метаданные происхождения")
    if consensus is None:
        percent = None
        probability_label = "Недостаточно данных"
        probability_detail = "Ни одна вероятностная модель не ответила; отсутствие меток не означает 0%."
    else:
        # Не показываем абсолютные 0/100: любой детектор допускает ошибки и стареет.
        percent = max(1, min(99, round(consensus * 100)))
        spread = max(probability_inputs) - min(probability_inputs) if len(probability_inputs) > 1 else 0
        if decisive_source:
            probability_label = "Подтверждено происхождение"
        elif spread >= 0.45:
            probability_label = "Низкая согласованность"
        elif len(probability_inputs) == 1:
            probability_label = "Один источник оценки"
        else:
            probability_label = "Сводная оценка"
        probability_detail = f"Основание: {decisive_source}. Оценки моделей не влияют на итог." if decisive_source else "Основание: " + ", ".join(source_names)
        if spread >= 0.45:
            probability_detail += ". Детекторы сильно расходятся — процент ненадёжен."

        if local["verdict"] == "detected" or decisive_source or ai_votes > len(scores) / 2 or consensus >= 0.65:
            local["verdict"] = "detected"
            local["headline"] = f"Обнаружена метка {decisive_source}" if decisive_source else "Есть признаки AI‑происхождения"
        elif ai_votes == 0 and consensus <= 0.35:
            local["verdict"] = "clear"
            local["headline"] = "Скорее не сгенерировано"
        else:
            local["verdict"] = "review"
            local["headline"] = "Детекторы расходятся во мнениях"
        local["confidence"] = percent
    local["generation_probability"] = {
        "percent": percent,
        "label": probability_label,
        "detail": probability_detail,
        "sources": len(probability_inputs),
        "decisive_source": decisive_source,
    }
    local["probability_breakdown"] = _probability_breakdown(local, api_results)
    return local


def _percentage(score: float) -> int:
    return max(0, min(99, round(score * 100)))


def _probability_breakdown(local: dict[str, Any], results: list[ProviderResult]) -> list[dict[str, Any]]:
    successful = [item for item in results if item.get("status") == "ok"]
    model = next((item for item in successful if item.get("id") == "local_frontier"), None)
    model_result = next((item for item in results if item.get("id") == "local_frontier"), None)
    semantic = next((item for item in successful if item.get("id") == "openai_vision"), None)
    external = [
        item for item in successful
        if item.get("id") not in {"local_frontier", "openai_vision", "synthid"} and not item.get("provenance") and item.get("verdict") != "unknown"
    ]

    finding_kinds = {str(item.get("kind")) for item in local.get("findings", [])}
    confirmed = _confirmed_provenance(local, results)
    if confirmed:
        provenance = 99
        provenance_detail = f"Подтверждено происхождение: {confirmed}."
    elif finding_kinds & {"provider", "generation_metadata"}:
        provenance = 95
        provenance_detail = "В файле найдены сигнатуры генератора или параметры генерации."
    elif "corner" in finding_kinds:
        provenance = 40
        provenance_detail = "Эвристика заметила возможный бейдж в углу; нужна ручная проверка."
    elif finding_kinds & {"credentials", "c2pa_invalid", "c2pa_generative"}:
        provenance = 20
        provenance_detail = "Следы происхождения есть, но они не подтверждают генерацию."
    else:
        provenance = 0
        provenance_detail = "AI-сигнатуры, C2PA-указание и видимый бейдж не найдены."

    remote_provenance = next((item for item in results if item.get("id") == "synthid"), None)
    if remote_provenance:
        if _confirmed_provenance({}, [remote_provenance]):
            provenance = 99
            provenance_detail = remote_provenance["detail"]
        else:
            provenance_detail += " OpenAI SynthID / C2PA: " + remote_provenance.get("detail", "нет результата")
    else:
        provenance_detail += " Внешняя проверка SynthID / C2PA не выполнялась."

    forensic_signals = [
        signal for signal in (local.get("forensics") or {}).get("signals", [])
        if signal.get("status") != "not_applicable"
    ]
    if forensic_signals:
        signal_scores = {"normal": 0.05, "review": 0.65, "error": 0.35}
        pixel_score = sum(signal_scores.get(str(signal.get("status")), 0.15) for signal in forensic_signals) / len(forensic_signals)
        reviewed = sum(signal.get("status") == "review" for signal in forensic_signals)
        pixel_detail = (
            f"Подозрительных эвристик: {reviewed} из {len(forensic_signals)}. "
            "Это индекс аномалий, а не самостоятельное доказательство."
        )
        pixel_percent: int | None = _percentage(pixel_score)
    else:
        pixel_percent = None
        pixel_detail = "Формат или размер изображения не позволил рассчитать индекс."

    external_score = sum(float(item["score"]) for item in external) / len(external) if external else None
    return [
        {
            "id": "model",
            "title": "Локальная ML-модель",
            "status": model_result.get("status") if model_result else "unavailable",
            "percent": _percentage(float(model["score"])) if model else None,
            "detail": model_result.get("detail") if model_result else "Локальная модель не ответила.",
            "source_count": 1 if model else 0,
        },
        {
            "id": "semantic",
            "title": "Смысловой анализ",
            "percent": _percentage(float(semantic["score"])) if semantic and semantic.get("score") is not None else None,
            "detail": semantic.get("detail") if semantic else "Смысловой анализ не выполнялся или не ответил. Нужен вход через ChatGPT.",
            "source_count": 1 if semantic else 0,
        },
        {
            "id": "external",
            "title": "Внешние детекторы",
            "percent": _percentage(external_score) if external_score is not None else None,
            "detail": f"Среднее по {len(external)} подключённым сервисам." if external else "Ни один специализированный внешний API не настроен.",
            "source_count": len(external),
        },
        {
            "id": "provenance",
            "title": "Вотермарки и происхождение",
            "percent": provenance,
            "detail": provenance_detail,
            "source_count": 1 + int(bool(remote_provenance and remote_provenance.get("status") == "ok")),
        },
        {
            "id": "pixels",
            "title": "Пиксельные аномалии",
            "percent": pixel_percent,
            "detail": pixel_detail,
            "source_count": len(forensic_signals),
        },
    ]
