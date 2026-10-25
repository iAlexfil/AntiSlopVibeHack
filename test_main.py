import io

from PIL import Image, PngImagePlugin

from main import analyze_image


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
