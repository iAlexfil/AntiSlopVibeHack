import io
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

from evaluation.datasets import folder_manifest
from evaluation.metrics import average_precision, confusion, fpr_at_recall, roc_auc, summarize
from evaluation.report import build_report, render_markdown
from evaluation.runner import VARIANTS


def test_metrics() -> None:
    labels = np.array([0, 0, 1, 1])
    assert roc_auc(labels, np.array([0.1, 0.4, 0.35, 0.8])) == 0.75
    assert roc_auc(labels, np.array([0.1, 0.2, 0.8, 0.9])) == 1.0
    assert roc_auc(labels, np.array([0.5, 0.5, 0.5, 0.5])) == 0.5
    assert roc_auc(np.array([1, 1]), np.array([0.1, 0.2])) is None
    assert abs(average_precision(labels, np.array([0.1, 0.4, 0.35, 0.8])) - (1 + 2 / 3) / 2) < 1e-9
    stats = confusion(labels, np.array([0.1, 0.7, 0.66, 0.2]), 0.65)
    assert (stats["tp"], stats["fp"], stats["tn"], stats["fn"]) == (1, 1, 1, 1)
    assert stats["recall"] == 0.5 and stats["fpr"] == 0.5
    assert fpr_at_recall(labels, np.array([0.1, 0.4, 0.35, 0.8]), 1.0) == {"threshold": 0.35, "recall": 1.0, "fpr": 0.5}
    summary = summarize([0, 1, 0, 1], [0.2, 0.9, 0.3, 0.7], 0.65)
    assert summary["roc_auc"] == 1.0 and summary["at_threshold"]["balanced_accuracy"] == 1.0


def test_folder_manifest_and_report() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "custom"
        for relative, color in (("real/a.png", (10, 20, 30)), ("ai/flux/b.png", (200, 10, 10)), ("ai/c.png", (5, 5, 5))):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (64, 48), color).save(path)
        entries = folder_manifest(root)
        assert sorted((entry["label"], entry["generator"]) for entry in entries) == [(0, "real"), (1, "flux"), (1, "unknown")]

    predictions = {
        (entry["id"], "original"): {"id": entry["id"], "variant": "original", "status": "ok", "label": entry["label"],
                                    "final": 0.9 if entry["label"] else 0.1, "local_model": 0.8 if entry["label"] else 0.3,
                                    "pixels": 0.05, "provenance": 0.0, "semantic": None, "external": None}
        for entry in entries
    }
    report = build_report(entries, predictions, ["original"])
    assert report["variants"]["original"]["detectors"]["final"]["roc_auc"] == 1.0
    assert "semantic" not in report["variants"]["original"]["detectors"]
    assert report["variants"]["original"]["groups"]["flux"]["final_correct"] == 1.0
    assert "Итоговая вероятность" in render_markdown(report, {"created": "x", "pipeline_version": "x", "remote": False, "sources": ["custom"]})


def test_variants() -> None:
    buffer = io.BytesIO()
    Image.new("RGBA", (101, 77), (1, 2, 3, 128)).save(buffer, "PNG")
    for name, transform in VARIANTS.items():
        if transform is None:
            continue
        data, mime_type = transform(buffer.getvalue())
        image = Image.open(io.BytesIO(data))
        assert mime_type.startswith("image/"), name
        assert image.width <= 101, name


if __name__ == "__main__":
    test_metrics()
    test_folder_manifest_and_report()
    test_variants()
    print("evaluation tests passed")
