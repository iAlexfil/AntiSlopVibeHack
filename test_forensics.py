import io
import json
import struct
import zlib
from unittest.mock import patch

from fastapi import HTTPException
from PIL import Image

from forensics import run_forensics, verify_c2pa
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


def test_c2pa_generative_requires_an_exact_action_source_type() -> None:
    ai_type = "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"
    composite_type = "http://cv.iptc.org/newscodes/digitalsourcetype/compositeWithTrainedAlgorithmicMedia"
    real_type = "http://cv.iptc.org/newscodes/digitalsourcetype/digitalCapture"

    def assertion(actions, templates=None, label="c2pa.actions.v2"):
        data = {"actions": actions}
        if templates is not None:
            data["templates"] = templates
        return {"label": label, "data": data}

    cases = [
        (assertion([{"action": "c2pa.created", "digitalSourceType": source}], label=label), True)
        for source in (ai_type, composite_type) for label in ("c2pa.actions", "c2pa.actions.v2")
    ]
    cases.extend([
        (assertion([{"action": "c2pa.created", "description": ai_type}]), False),
        (assertion([{"action": "c2pa.created", "digitalSourceType": real_type, "description": ai_type}]), False),
    ])
    cases.extend((assertion([{"action": "c2pa.created", "digitalSourceType": source}]), False)
                 for source in ("trainedAlgorithmicMedia", "https://untrusted.example/trainedAlgorithmicMedia", ai_type + "-description", real_type, None, [ai_type]))
    cases.extend([
        (assertion([{"action": "c2pa.created", "digitalSourceType": ai_type}], label="example.metadata"), False),
        ({"label": "c2pa.ingredient.v3", "data": {"description": ai_type}}, False),
        (assertion([{"action": "c2pa.created"}], [{"action": "c2pa.edited", "digitalSourceType": ai_type}]), False),
        (assertion([{"action": "c2pa.created"}], [{"action": "c2pa.created", "digitalSourceType": ai_type}]), True),
        (assertion([{"action": "c2pa.created"}], [{"action": "*", "digitalSourceType": ai_type}]), True),
        (assertion([{"action": "c2pa.created", "digitalSourceType": real_type}], [{"action": "*", "digitalSourceType": ai_type}]), False),
        (assertion([{"action": "c2pa.created"}], [{"action": "c2pa.created", "digitalSourceType": real_type}, {"action": "*", "digitalSourceType": ai_type}]), False),
        (assertion([{"action": "c2pa.created"}], [{"action": "c2pa.created", "digitalSourceType": real_type}, {"action": "c2pa.created", "digitalSourceType": ai_type}]), True),
        (assertion([{"action": "c2pa.created"}], [{"action": "*", "digitalSourceType": ai_type}], label="c2pa.actions"), False),
        (assertion([{"action": "c2pa.created", "parameters": {"digitalSourceType": ai_type}}]), False),
    ])
    for value, expected in cases:
        store = {
            "active_manifest": "active",
            "manifests": {
                "active": {"title": ai_type, "assertions": [value]},
                "inactive": {"assertions": [assertion([{"action": "c2pa.created", "digitalSourceType": ai_type}])]},
            },
            "validation_status": [{"code": "claimSignature.validated", "success": True}],
        }
        with patch("c2pa.Reader") as reader:
            reader.return_value.__enter__.return_value.json.return_value = json.dumps(store)
            result = verify_c2pa(b"reader fixture", "image/png")
        assert result["generative"] is expected, value
        assert result["status"] == "valid"
        assert result["signature_valid"] is True


if __name__ == "__main__":
    test_forensic_report_is_bounded()
    test_oversized_image_rejected_before_load()
    test_c2pa_generative_requires_an_exact_action_source_type()
    print("forensic tests: 3/3 passed")
