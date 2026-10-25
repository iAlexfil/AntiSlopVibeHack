import io
import struct
import zlib

from fastapi import HTTPException
from PIL import Image

from forensics import run_forensics
from main import analyze_image


def _png_chunk(name: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + name + payload + struct.pack(">I", zlib.crc32(name + payload) & 0xFFFFFFFF)


def oversized_png_header() -> bytes:
    header = struct.pack(">IIBBBBB", 7000, 7000, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header) + _png_chunk(b"IEND", b"")


def test_forensic_report_is_bounded() -> None:
    image = Image.new("RGB", (2048, 1536), "#777777")
    output = io.BytesIO()
    image.save(output, "JPEG", quality=90)
    report = run_forensics(image, output.getvalue(), {})
    assert report["resource_budget"]["max_analysis_edge"] == 1024
    assert report["resource_budget"]["max_fft_edge"] == 512
    assert report["resource_budget"]["ml_model_loaded"] is False
    assert len(report["signals"]) == 4
    assert all(item["image"].startswith("data:image/jpeg;base64,") for item in report["maps"])
    assert report["c2pa"]["status"] == "none"


def test_oversized_image_rejected_before_load() -> None:
    try:
        analyze_image(oversized_png_header(), "bomb.png")
    except HTTPException as error:
        assert error.status_code == 413
    else:
        raise AssertionError("oversized image was accepted")


if __name__ == "__main__":
    test_forensic_report_is_bounded()
    test_oversized_image_rejected_before_load()
    print("forensic tests: 2/2 passed")
