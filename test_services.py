import asyncio
from copy import deepcopy
from email.parser import BytesParser
from email.policy import default
import io
import json
import os
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

import services
from services import _aiornot, _hive, _openai_provenance, _openai_vision, _sightengine, _wasitai, merge_service_results


async def run_adapter_tests() -> None:
    credentials = {
        "HIVE_API_KEY": "test",
        "SIGHTENGINE_API_USER": "test",
        "SIGHTENGINE_API_SECRET": "test",
        "AIORNOT_API_KEY": "test",
        "WASITAI_API_KEY": "test",
    }

    responses = {
        "api.thehive.ai": {
            "status": [{"response": {"output": [{"classes": [
                {"class": "ai_generated", "score": 0.91},
                {"class": "flux", "score": 0.83},
            ]}]}}],
        },
        "api.sightengine.com": {
            "status": "success",
            "type": {"ai_generated": 0.82, "ai_generators": {"gpt": 0.76, "flux": 0.1}},
        },
        "api.aiornot.com": {
            "report": {"ai_generated": {
                "verdict": "ai",
                "ai": {"is_detected": True, "confidence": 0.88},
                "generator": {"midjourney": {"is_detected": True, "confidence": 0.72}},
            }},
        },
        "api.wasitai.com": {
            "classifiedImage": {
                "status": "ok",
                "finalVerdictWasItAI": True,
                "finalVerdictConfidenceScore": 79,
                "finalVerdictHumanFriendlyDescription": "AI detected",
            },
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses[request.url.host])

    with patch.dict(os.environ, credentials):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            data = b"image"
            results = await asyncio.gather(
                _hive(client, data, "test.png", "image/png"),
                _sightengine(client, data, "test.png", "image/png"),
                _aiornot(client, data, "test.png", "image/png"),
                _wasitai(client, data, "test.png", "image/png"),
            )

    assert [item["verdict"] for item in results] == ["ai", "ai", "ai", "ai"]
    assert [round(item["score"], 2) for item in results] == [0.91, 0.82, 0.88, 0.79]
    assert results[0]["generator"] == "flux"
    assert results[1]["generator"] == "gpt"
    assert results[2]["generator"] == "midjourney"


def test_consensus_merge() -> None:
    local = {"verdict": "clear", "headline": "", "confidence": 0}
    api_results = [
        {"id": "hive", "status": "ok", "verdict": "ai", "score": 0.9},
        {"id": "sightengine", "status": "ok", "verdict": "ai", "score": 0.8},
        {"id": "wasitai", "status": "ok", "verdict": "human", "score": 0.2},
    ]
    merged = merge_service_results(local, api_results)
    assert merged["verdict"] == "detected"
    assert merged["api_summary"]["ai_votes"] == 2
    assert merged["confidence"] == 63
    assert merged["generation_probability"]["percent"] == 63
    breakdown = {item["id"]: item for item in merged["probability_breakdown"]}
    assert breakdown["external"]["percent"] == 63
    assert breakdown["model"]["percent"] is None
    failed = merge_service_results({"verdict": "clear"}, [
        {"id": "local_frontier", "status": "error", "detail": "Локальная модель превысила лимит памяти"},
    ])
    model = next(item for item in failed["probability_breakdown"] if item["id"] == "model")
    assert model["status"] == "error" and model["percent"] is None
    assert model["detail"] == "Локальная модель превысила лимит памяти"


def visual_answer() -> dict:
    return {
        "schema_version": "1.0", "probability": 91, "assessment": "likely_ai",
        "explanation": "Несогласованная анатомия", "signals": [{
            "category": "anatomy", "observation": "Лишняя конечность",
            "explanation": "Видно неоднозначное соединение лапы с телом.",
            "alternative_explanation": "Возможен монтаж или перекрытие объектов.",
            "strength": "strong", "region": {"x1": 0.1, "y1": 0.2, "x2": 0.5, "y2": 0.8},
        }], "limitations": ["Уменьшенная копия изображения"],
    }


def sse(event: dict) -> str:
    return "data: " + json.dumps(event) + "\n\n"


async def test_openai_vision_adapter() -> None:
    answer = visual_answer()
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url == "https://api.openai.com/v1/responses"
        assert request.headers["Authorization"] == "Bearer oauth-only"
        body = json.loads(request.content)
        assert body["model"] == "gpt-6-luna"
        assert body["store"] is False and body["stream"] is True
        assert "max_output_tokens" not in body
        assert body["text"]["format"]["strict"] is True
        image_url = body["input"][0]["content"][1]["image_url"]
        assert image_url.startswith("data:image/jpeg;base64,")
        event = {"type": "response.completed", "response": {"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(answer)}]},
        ]}}
        return httpx.Response(200, text=sse(event), headers={"Content-Type": "text/event-stream"})

    with patch.dict(os.environ, {"OPENAI_API_KEY": "synthid-only", "CHATGPT_VISION_MODEL": "gpt-6-luna"}), \
            patch.object(services.chatgpt_auth.ChatGPTAuth, "access_token", new_callable=AsyncMock, return_value="oauth-only"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await _openai_vision(client, image_bytes(), "test.png", "image/png")
            assert result["score"] == 0.91 and result["verdict"] == "ai"
            assert result["semantic"] is True and result["analysis"] == answer
            assert result["auth_mode"] == "chatgpt_plan"
            answer.update(probability=None, assessment="insufficient_detail", signals=[])
            result = await _openai_vision(client, image_bytes(), "test.png", "image/png")
            assert result["score"] is None and result["verdict"] == "unknown"
            merged = merge_service_results({"verdict": "clear"}, [{**result, "id": "openai_vision"}])
            assert merged["generation_probability"]["percent"] is None
    assert len(seen) == 2


async def test_vision_failures_do_not_fall_back_or_leak_secrets() -> None:
    failures = [
        (200, sse({"type": "error", "code": "subscription_sharing_usage_limit_exceeded", "message": "private-secret"})),
        (200, sse({"type": "response.failed", "response": {"error": {
            "code": "subscription_sharing_usage_limit_exceeded", "message": "private-secret"}}})),
        (200, sse({"type": "response.incomplete", "response": {"error": None}})),
        (200, sse({"type": "response.output_text.delta", "delta": "private-secret"})),
        (200, sse({"type": "response.completed", "response": {"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "private-secret"}]}]}})),
        (200, "data: " + "x" * (2 * 1024 * 1024) + "\n\n"),
        (401, "private-secret"), (429, "private-secret"),
    ]
    for code, body in failures:
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(code, text=body)
        with patch.object(services.chatgpt_auth.ChatGPTAuth, "access_token", new_callable=AsyncMock, return_value="private-secret"):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await services._run_one("openai_vision", _openai_vision, client, image_bytes(), "x.png", "image/png")
        assert len(calls) == 1
        assert result["status"] == "error" and "private-secret" not in str(result)
        assert "https://" not in result["detail"]
    with patch.object(services.chatgpt_auth.ChatGPTAuth, "access_token", new_callable=AsyncMock,
                      side_effect=services.chatgpt_auth.AuthError("Требуется повторный вход.")):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError("No API call allowed")))) as client:
            result = await services._run_one("openai_vision", _openai_vision, client, image_bytes(), "x.png", "image/png")
            assert result["detail"] == "Требуется повторный вход."


async def test_vision_stream_accepts_text_only_after_completion() -> None:
    answer = json.dumps(visual_answer(), ensure_ascii=False)
    chunks = [
        sse({"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": answer[:30]}),
        sse({"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": answer[30:]}),
    ]
    completed = sse({"type": "response.completed", "response": {"status": "completed", "output": []}})
    done = sse({"type": "response.output_text.done", "output_index": 0, "content_index": 0, "text": answer})
    for stream in ("".join(chunks) + completed, "".join(chunks) + done + completed, done + completed):
        assert await services._vision_stream(httpx.Response(200, text=stream)) == answer
    for terminal in ("", sse({"type": "response.incomplete", "response": {"error": None}}),
                     sse({"type": "error", "code": "subscription_sharing_usage_limit_exceeded"})):
        try:
            await services._vision_stream(httpx.Response(200, text="".join(chunks) + done + terminal))
        except ValueError:
            pass
        else:
            raise AssertionError("Partial or failed streamed text was accepted")


def test_vision_contract_rejects_bad_results() -> None:
    from vision_contract import VisualAnalysis
    from pydantic import ValidationError
    invalid = []
    for changes in ({"probability": True}, {"probability": 101}, {"probability": None},
                    {"schema_version": "2.0"}, {"extra": "secret"}):
        invalid.append({**visual_answer(), **changes})
    for region in ({"x1": 0.5, "y1": 0.1, "x2": 0.1, "y2": 0.8},
                   {"x1": -0.1, "y1": 0.1, "x2": 0.5, "y2": 0.8}):
        value = visual_answer()
        value["signals"][0]["region"] = region
        invalid.append(value)
    for value in invalid:
        try:
            VisualAnalysis.model_validate(value)
        except ValidationError:
            pass
        else:
            raise AssertionError("Invalid visual result accepted")


def test_vision_preview_keeps_orientation_and_bounds():
    import base64
    image = Image.new("RGB", (2000, 1000), "white")
    output = io.BytesIO()
    exif = Image.Exif()
    exif[274] = 6
    image.save(output, "JPEG", exif=exif)
    data_url = services._vision_data_url(output.getvalue())
    with Image.open(io.BytesIO(base64.b64decode(data_url.split(",", 1)[1]))) as preview:
        assert preview.size == (640, 1280)
        assert preview.getexif().get(274) is None


def provenance_payload(outcome: str = "not_detected") -> dict:
    return {
        "id": "cpc_test",
        "object": "content_provenance_check",
        "results": [
            {"type": "synthid", "outcome": outcome, "model": None, "generated_at": None},
            {"type": "c2pa", "outcome": "not_detected", "model": None, "generated_at": None,
             "issuer": None, "validation_state": "not_present"},
        ],
    }


def image_bytes(image_format: str = "PNG") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (16, 16), "#888888").save(output, image_format)
    return output.getvalue()


async def test_provenance_original_multipart_and_format_sniffing() -> None:
    for image_format, content_type in (("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")):
        data = image_bytes(image_format)
        payload = provenance_payload("detected")
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            assert request.method == "POST"
            assert str(request.url) == "https://api.openai.com/v1/content_provenance_checks"
            assert request.headers["authorization"] == "Bearer test"
            assert request.headers["content-type"].startswith("multipart/form-data;")
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content
            )
            parts = list(message.iter_parts())
            assert len(parts) == 1
            assert parts[0].get_param("name", header="content-disposition") == "file"
            assert parts[0].get_content_type() == content_type
            assert parts[0].get_payload(decode=True) == data
            return httpx.Response(200, json=payload)

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await _openai_provenance(client, data, "spoofed.gif", "image/gif")
        assert len(calls) == 1
        assert result["status"] == "ok"
        assert result["provenance"] is True
        assert result["score"] is None
        assert result["verdict"] == "ai"
        assert result["checks"] == payload["results"]


async def test_provenance_negative_optional_checks_and_c2pa() -> None:
    negative = provenance_payload()
    synthid_only = provenance_payload("detected")
    synthid_only["results"] = synthid_only["results"][:1]
    c2pa_only = provenance_payload()
    c2pa_only["results"] = [{
        "type": "c2pa", "outcome": "detected", "model": None, "generated_at": None,
        "issuer": None, "validation_state": "valid",
    }]
    trusted_c2pa = deepcopy(c2pa_only)
    trusted_c2pa["results"][0]["validation_state"] = "trusted"
    for payload, verdict in ((negative, "unknown"), (synthid_only, "ai"), (c2pa_only, "ai"), (trusted_c2pa, "ai")):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=payload)
            )) as client:
                result = await _openai_provenance(client, image_bytes(), "image.png", "image/png")
        assert result["verdict"] == verdict
        assert result["score"] is None
        assert result["checks"] == payload["results"]


async def test_provenance_unsupported_formats_do_not_send_requests() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unsupported image was sent to OpenAI")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for image_format in ("GIF", "BMP", "TIFF"):
            result = await _openai_provenance(client, image_bytes(image_format), "spoofed.png", "image/png")
            assert result["status"] == "unsupported"
            assert result.get("score") is None
            assert result.get("verdict") != "human"


async def test_provenance_rejects_malformed_results() -> None:
    bad_payloads = [None, [], {}, {"object": "other", "results": provenance_payload()["results"]}]
    for bad_results in (None, [], {}, [None], [{"type": "other", "outcome": "detected"}],
                        [{"type": "synthid", "outcome": "human"}],
                        [{"type": "synthid", "outcome": "detected", "model": 42}],
                        [{"type": "synthid", "outcome": "detected", "generated_at": []}],
                        [{"type": "c2pa", "outcome": "detected", "validation_state": "invalid"}],
                        [{"type": "c2pa", "outcome": "detected", "validation_state": "not_present"}],
                        [{"type": "c2pa", "outcome": "detected", "validation_state": "trusted", "issuer": []}]):
        bad_payloads.append({"object": "content_provenance_check", "results": bad_results})
    for payload in bad_payloads:
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=payload)
            )) as client:
                try:
                    await _openai_provenance(client, image_bytes(), "image.png", "image/png")
                except ValueError:
                    pass
                else:
                    raise AssertionError(f"malformed provenance response was accepted: {payload!r}")


async def test_provenance_http_failures_are_safe_and_not_retried() -> None:
    for status_code in (400, 401, 403, 404, 429, 500, 503, None):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if status_code is None:
                raise httpx.ReadTimeout("private-token-in-error", request=request)
            return httpx.Response(status_code, json={"error": {"message": "private-token-in-error"}})

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await services._run_one(
                    "synthid", _openai_provenance, client, image_bytes(), "image.png", "image/png"
                )
        assert len(calls) == 1
        assert result["id"] == "synthid"
        assert result["status"] == ("unavailable" if status_code == 404 else "error")
        assert "private-token-in-error" not in result["detail"]
        assert "https://" not in result["detail"]
        assert any("а" <= character <= "я" for character in result["detail"].lower())


async def test_provenance_registry_and_configuration() -> None:
    with patch.dict(os.environ, {}, clear=True), patch.object(services.chatgpt_auth, "status", return_value={"configured": False, "mode": "chatgpt_plan"}):
        catalog = {item["id"]: item for item in services.provider_catalog()}
        assert not catalog["synthid"]["configured"]
        assert catalog["synthid"]["required_env"] == ["OPENAI_API_KEY"]
        assert not catalog["synthid"].get("unavailable")
        with patch.object(services, "_run_one", new_callable=AsyncMock) as runner:
            results = await services.analyze_with_services(image_bytes(), "image.png", "image/png")
            runner.assert_not_awaited()
        assert next(item for item in results if item["id"] == "synthid")["status"] == "not_configured"

    async def fake_runner(provider_id, *args):
        return {"id": provider_id, "status": "ok", "verdict": "unknown", "score": None}

    with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}, clear=True), patch.object(services.chatgpt_auth, "status", return_value={"configured": False, "mode": "chatgpt_plan"}):
        catalog = {item["id"]: item for item in services.provider_catalog()}
        assert catalog["synthid"]["configured"]
        assert services.RUNNERS["synthid"] is _openai_provenance
        with patch.object(services, "_run_one", new_callable=AsyncMock, side_effect=fake_runner) as runner:
            await services.analyze_with_services(image_bytes(), "image.png", "image/png")
        assert {call.args[0] for call in runner.await_args_list} == {"synthid"}


def test_confirmed_provenance_overrides_models() -> None:
    remote = {
        "id": "synthid", "name": "OpenAI SynthID / C2PA", "status": "ok", "score": None,
        "verdict": "ai", "provenance": True, "detail": "Обнаружена водяная метка SynthID.",
        "checks": [{"type": "synthid", "outcome": "detected"}],
    }
    models = [
        {"id": "local_frontier", "status": "ok", "score": 0.01, "verdict": "human"},
        {"id": "openai_vision", "status": "ok", "score": 0.18, "verdict": "human"},
    ]
    valid_c2pa = {"status": "valid", "signature_valid": True, "generative": True}
    cases = [({"verdict": "clear"}, remote, "SynthID")]
    cases.extend((
        {"verdict": "clear"},
        {**remote, "checks": [{"type": "c2pa", "outcome": "detected", "validation_state": state}]},
        "C2PA (OpenAI)",
    ) for state in ("trusted", "valid"))
    cases.append(({"verdict": "clear", "forensics": {"c2pa": valid_c2pa}}, None, "C2PA"))
    cases.append(({
        "verdict": "clear", "forensics": {"c2pa": valid_c2pa},
        "findings": [{"kind": "c2pa_generative", "level": "strong"}, {"kind": "generation_metadata", "level": "strong"}],
    }, {**remote, "checks": remote["checks"] + [{"type": "c2pa", "outcome": "detected", "validation_state": "trusted"}]}, "SynthID"))
    for local, provenance, source in cases:
        results = deepcopy(models + ([provenance] if provenance else []))
        expected_results = deepcopy(results)
        merged = merge_service_results(deepcopy(local), results)
        probability = merged["generation_probability"]
        assert probability["decisive_source"] == source
        assert probability["sources"] == 1
        assert probability["percent"] == 99
        assert probability["label"] == "Подтверждено происхождение"
        assert "ненадёжен" not in probability["detail"]
        assert merged["api_summary"]["score"] == 0.99
        assert merged["verdict"] == "detected"
        assert merged["headline"] == "Обнаружена метка " + source
        assert merged["api_results"] == expected_results, "Individual model results remain available without changed scores"
        breakdown = {item["id"]: item for item in merged["probability_breakdown"]}
        assert breakdown["model"]["percent"] == 1
        assert breakdown["semantic"]["percent"] == 18
        assert breakdown["external"]["percent"] is None
        assert breakdown["external"]["source_count"] == 0
        assert breakdown["provenance"]["percent"] == 99
    merged = merge_service_results({"verdict": "clear"}, [remote])
    assert merged["generation_probability"]["percent"] == 99
    assert merged["generation_probability"]["sources"] == 1


def test_unconfirmed_provenance_never_overrides_or_lowers_models() -> None:
    remote = {
        "id": "synthid", "name": "OpenAI SynthID / C2PA", "status": "ok", "score": None,
        "verdict": "ai", "provenance": True, "detail": "Результат проверки происхождения.",
    }
    models = [
        {"id": "local_frontier", "status": "ok", "score": 0.01, "verdict": "human"},
        {"id": "openai_vision", "status": "ok", "score": 0.18, "verdict": "human"},
    ]
    unconfirmed_remote = [
        remote, {**remote, "checks": []},
        {**remote, "verdict": "unknown", "checks": [{"type": "synthid", "outcome": "not_detected"}]},
    ]
    unconfirmed_remote.extend(
        {**remote, "checks": [{"type": "c2pa", "outcome": "detected", "validation_state": state}]}
        for state in ("untrusted", "invalid", None)
    )
    unconfirmed_remote.append({**remote, "checks": [{"type": "c2pa", "outcome": "not_detected", "validation_state": "trusted"}]})
    unconfirmed_remote.extend(
        {**remote, "status": status, "checks": [{"type": "synthid", "outcome": "detected"}]}
        for status in ("error", "unavailable", "unsupported", "not_configured")
    )
    unconfirmed_local = [
        {},
        {"forensics": {"c2pa": {"status": "valid", "signature_valid": True, "generative": False}}},
        {"forensics": {"c2pa": {"status": "valid", "generative": True}}},
    ]
    unconfirmed_local.extend(
        {"forensics": {"c2pa": {"status": status, "signature_valid": signature, "generative": True}}}
        for status, signature in (("valid", False), ("untrusted", True), ("invalid", False), ("error", True))
    )
    for local in unconfirmed_local:
        local["verdict"] = "clear"
        local["findings"] = [{"kind": "c2pa_generative", "level": "strong"}]
    for local, provenance in [({"verdict": "clear"}, item) for item in unconfirmed_remote] + [(item, None) for item in unconfirmed_local]:
        merged = merge_service_results(deepcopy(local), deepcopy(models + ([provenance] if provenance else [])))
        probability = merged["generation_probability"]
        assert probability["decisive_source"] is None
        assert probability["percent"] == 10
        assert probability["sources"] == 2

    for kind in ("provider", "generation_metadata"):
        merged = merge_service_results({"verdict": "clear", "findings": [{"kind": kind, "level": "strong"}]}, deepcopy(models))
        assert merged["generation_probability"]["decisive_source"] is None
        assert merged["generation_probability"]["percent"] == 38
        assert merged["generation_probability"]["sources"] == 3

    divergent = [models[0], {**models[1], "score": 0.95, "verdict": "ai"}]
    merged = merge_service_results({"verdict": "clear"}, divergent)
    assert merged["generation_probability"]["label"] == "Низкая согласованность"
    assert merged["generation_probability"]["decisive_source"] is None

    negative = {**remote, "verdict": "unknown", "detail": "Водяные метки не обнаружены."}
    merged = merge_service_results({"verdict": "clear"}, [negative])
    assert merged["generation_probability"]["percent"] is None
    assert merged["generation_probability"]["sources"] == 0
    assert merged["api_summary"]["checked"] == 1
    assert merged["api_summary"]["ai_votes"] == 0
    merged = merge_service_results({"verdict": "clear"}, [models[0], negative])
    assert merged["generation_probability"]["percent"] == 1
    assert merged["generation_probability"]["sources"] == 1
    for status in ("error", "unavailable", "unsupported", "not_configured"):
        skipped = {**remote, "status": status, "detail": f"Проверка: {status}"}
        merged = merge_service_results({"verdict": "clear"}, [skipped])
        assert merged["generation_probability"]["percent"] is None
        provenance = next(item for item in merged["probability_breakdown"] if item["id"] == "provenance")
        assert skipped["detail"] in provenance["detail"]


if __name__ == "__main__":
    asyncio.run(run_adapter_tests())
    asyncio.run(test_openai_vision_adapter())
    asyncio.run(test_vision_failures_do_not_fall_back_or_leak_secrets())
    asyncio.run(test_vision_stream_accepts_text_only_after_completion())
    test_vision_contract_rejects_bad_results()
    test_vision_preview_keeps_orientation_and_bounds()
    test_consensus_merge()
    asyncio.run(test_provenance_original_multipart_and_format_sniffing())
    asyncio.run(test_provenance_negative_optional_checks_and_c2pa())
    asyncio.run(test_provenance_unsupported_formats_do_not_send_requests())
    asyncio.run(test_provenance_rejects_malformed_results())
    asyncio.run(test_provenance_http_failures_are_safe_and_not_retried())
    asyncio.run(test_provenance_registry_and_configuration())
    test_confirmed_provenance_overrides_models()
    test_unconfirmed_provenance_never_overrides_or_lowers_models()
    print("service adapter and provenance tests passed")
