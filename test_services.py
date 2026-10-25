import asyncio
from copy import deepcopy
from email.parser import BytesParser
from email.policy import default
import io
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


async def test_openai_vision_adapter() -> None:
    image = Image.new("RGB", (32, 32), "white")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        assert body["model"] == os.getenv("OPENAI_VISION_MODEL", "gpt-6-luna")
        assert body["input"][0]["content"][1]["type"] == "input_image"
        answer = '{"probability":91,"assessment":"likely_ai","summary":"Несогласованная анатомия","signals":["лишняя конечность"]}'
        return httpx.Response(200, json={"output": [{"type": "message", "content": [{"type": "output_text", "text": answer}]}]})

    with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await _openai_vision(client, buffer.getvalue(), "test.png", "image/png")
    assert result["score"] == 0.91
    assert result["verdict"] == "ai"
    assert result["semantic"] is True


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
    with patch.dict(os.environ, {}, clear=True):
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

    with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}, clear=True):
        catalog = {item["id"]: item for item in services.provider_catalog()}
        assert catalog["synthid"]["configured"]
        assert services.RUNNERS["synthid"] is _openai_provenance
        with patch.object(services, "_run_one", new_callable=AsyncMock, side_effect=fake_runner) as runner:
            await services.analyze_with_services(image_bytes(), "image.png", "image/png")
        assert {call.args[0] for call in runner.await_args_list} == {"synthid", "openai_vision"}


def test_provenance_merge_is_one_positive_signal_and_never_negative_evidence() -> None:
    remote = {
        "id": "synthid", "name": "OpenAI SynthID / C2PA", "status": "ok", "score": None,
        "verdict": "ai", "provenance": True, "detail": "Обнаружена водяная метка SynthID.",
    }
    model = {"id": "local_frontier", "status": "ok", "score": 0.2, "verdict": "human"}
    for findings in ([], [{"kind": "c2pa_generative", "level": "strong"}]):
        merged = merge_service_results({"verdict": "clear", "findings": findings}, [model, remote])
        probability = merged["generation_probability"]
        assert probability["sources"] == 2
        assert probability["percent"] == 60
        assert merged["api_summary"]["checked"] == 2
        assert merged["api_summary"]["ai_votes"] == 1
        breakdown = {item["id"]: item for item in merged["probability_breakdown"]}
        assert breakdown["external"]["percent"] is None
        assert breakdown["external"]["source_count"] == 0
        assert breakdown["provenance"]["percent"] == 99
        assert remote["detail"] in breakdown["provenance"]["detail"]

    negative = {**remote, "verdict": "unknown", "detail": "Водяные метки не обнаружены."}
    merged = merge_service_results({"verdict": "clear"}, [negative])
    assert merged["generation_probability"]["percent"] is None
    assert merged["generation_probability"]["sources"] == 0
    assert merged["api_summary"]["checked"] == 1
    assert merged["api_summary"]["ai_votes"] == 0
    merged = merge_service_results({"verdict": "clear"}, [model, negative])
    assert merged["generation_probability"]["percent"] == 20
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
    test_consensus_merge()
    asyncio.run(test_provenance_original_multipart_and_format_sniffing())
    asyncio.run(test_provenance_negative_optional_checks_and_c2pa())
    asyncio.run(test_provenance_unsupported_formats_do_not_send_requests())
    asyncio.run(test_provenance_rejects_malformed_results())
    asyncio.run(test_provenance_http_failures_are_safe_and_not_retried())
    asyncio.run(test_provenance_registry_and_configuration())
    test_provenance_merge_is_one_positive_signal_and_never_negative_evidence()
    print("service adapter and provenance tests passed")
