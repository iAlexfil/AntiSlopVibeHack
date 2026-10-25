import asyncio
import io
import os

import httpx
from PIL import Image

from services import _aiornot, _hive, _openai_vision, _sightengine, _wasitai, merge_service_results


async def run_adapter_tests() -> None:
    os.environ.update({
        "HIVE_API_KEY": "test",
        "SIGHTENGINE_API_USER": "test",
        "SIGHTENGINE_API_SECRET": "test",
        "AIORNOT_API_KEY": "test",
        "WASITAI_API_KEY": "test",
    })

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
    os.environ["OPENAI_API_KEY"] = "test"
    image = Image.new("RGB", (32, 32), "white")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        assert body["model"] == os.getenv("OPENAI_VISION_MODEL", "gpt-6-luna")
        assert body["input"][0]["content"][1]["type"] == "input_image"
        answer = '{"probability":91,"assessment":"likely_ai","summary":"Несогласованная анатомия","signals":["лишняя конечность"]}'
        return httpx.Response(200, json={"output": [{"type": "message", "content": [{"type": "output_text", "text": answer}]}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _openai_vision(client, buffer.getvalue(), "test.png", "image/png")
    assert result["score"] == 0.91
    assert result["verdict"] == "ai"
    assert result["semantic"] is True


if __name__ == "__main__":
    asyncio.run(run_adapter_tests())
    asyncio.run(test_openai_vision_adapter())
    test_consensus_merge()
    print("service adapter tests: 7/7 passed")
