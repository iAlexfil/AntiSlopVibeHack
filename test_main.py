import asyncio
import io
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image, PngImagePlugin

with patch("dotenv.load_dotenv"):
    import main

analyze_image = main.analyze_image


def png_with_metadata(key: str | None = None, value: str | None = None) -> bytes:
    image = Image.new("RGB", (128, 128), "#888888")
    output = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    if key and value:
        info.add_text(key, value)
    image.save(output, "PNG", pnginfo=info)
    return output.getvalue()


def test_detects_stable_diffusion_metadata() -> None:
    result = analyze_image(png_with_metadata("parameters", "Steps: 25, CFG scale: 7, Model: Stable Diffusion"), "sample.png")
    assert result["verdict"] == "detected"
    assert "Stable Diffusion" in result["providers"]


def test_clean_image_has_no_strong_signal() -> None:
    result = analyze_image(png_with_metadata(), "clean.png")
    assert result["verdict"] == "clear"
    assert result["confidence"] == 0


async def test_auth_status_is_local_and_safe() -> None:
    with patch.object(main, "vision_auth_status", return_value={
        "configured": False, "mode": "chatgpt_plan", "model": "gpt-6-luna", "detail": "Нужен вход",
    }):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            assert (await client.get("/health")).json() == {"status": "ok"}
            response = await client.get("/api/vision/auth")
            assert response.status_code == 200
            assert response.json()["configured"] is False
            assert set(response.json()) == {"configured", "mode", "model", "detail"}


async def test_remote_analysis_can_be_disabled() -> None:
    data = png_with_metadata()
    local_result = {"id": "local_frontier", "status": "error", "detail": "Модель недоступна"}
    provenance_result = {
        "id": "synthid", "name": "OpenAI SynthID / C2PA", "status": "ok", "score": None,
        "verdict": "unknown", "provenance": True, "detail": "Водяные метки не обнаружены.",
    }
    with patch.object(main, "_run_local_model_limited", new_callable=AsyncMock, return_value=local_result), \
            patch.object(main, "analyze_with_services", new_callable=AsyncMock,
                         return_value=[provenance_result]) as remote_runner:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            response = await client.post(
                "/api/analyze", params={"filename": "sample.png", "remote": "false"},
                content=data, headers={"Content-Type": "image/png"},
            )
            assert response.status_code == 200
            remote_runner.assert_not_awaited()
            result = response.json()
            assert [item["id"] for item in result["api_results"]] == ["local_frontier"]
            assert result["generation_probability"]["percent"] is None

            response = await client.post(
                "/api/analyze", params={"filename": "sample.png", "remote": "true"},
                content=data, headers={"Content-Type": "image/png"},
            )
            assert response.status_code == 200
            remote_runner.assert_awaited_once_with(data, "sample.png", "image/png")
            result = response.json()
            assert next(item for item in result["api_results"] if item["id"] == "synthid") == provenance_result
            assert result["api_summary"]["checked"] == 1
            assert result["generation_probability"]["percent"] is None


if __name__ == "__main__":
    test_detects_stable_diffusion_metadata()
    test_clean_image_has_no_strong_signal()
    asyncio.run(test_auth_status_is_local_and_safe())
    asyncio.run(test_remote_analysis_can_be_disabled())
    print("image analysis and remote opt-out tests passed")
