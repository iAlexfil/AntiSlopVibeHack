from __future__ import annotations

import io
import json
import math
import os
import resource
import sys
import time
from pathlib import Path


MEMORY_LIMIT_MB = 768
CPU_LIMIT_SECONDS = 20
MAX_INPUT_BYTES = 20 * 1024 * 1024


def _set_limits() -> None:
    memory = MEMORY_LIMIT_MB * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_LIMIT_SECONDS, CPU_LIMIT_SECONDS + 2))
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    os.environ.setdefault("MALLOC_ARENA_MAX", "2")


def _preprocess(raw: bytes):
    import numpy as np
    from PIL import Image

    image = Image.open(io.BytesIO(raw)).convert("RGB")
    width, height = image.size
    scale = 440 / min(width, height)
    resized = image.resize((round(width * scale), round(height * scale)), Image.Resampling.BICUBIC)
    left = (resized.width - 384) // 2
    top = (resized.height - 384) // 2
    crop = resized.crop((left, top, left + 384, top + 384))
    values = np.asarray(crop, dtype=np.float32) / 255.0
    mean = np.asarray([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.asarray([0.229, 0.224, 0.225], dtype=np.float32)
    values = (values - mean) / std
    return values.transpose(2, 0, 1)[None].astype(np.float32)


def main() -> None:
    _set_limits()
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise ValueError("invalid image size")

    import onnxruntime as ort

    model_path = Path(sys.argv[1]).resolve()
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.enable_cpu_mem_arena = False
    started = time.monotonic()
    session = ort.InferenceSession(str(model_path), sess_options=options, providers=["CPUExecutionProvider"])
    tensor = _preprocess(raw)
    input_name = session.get_inputs()[0].name
    output_name = session.get_outputs()[0].name
    logit = float(session.run([output_name], {input_name: tensor})[0].reshape(-1)[0])
    # Frozen bias-only calibrator published with the model.
    calibrated_logit = logit - 0.7403357915937764
    probability = 1.0 / (1.0 + math.exp(-max(-50.0, min(50.0, calibrated_logit))))
    peak_rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(json.dumps({
        "probability": round(probability, 6),
        "logit": round(logit, 6),
        "threshold": 0.65,
        "latency_ms": round((time.monotonic() - started) * 1000),
        "peak_rss_mb": round(peak_rss_mb, 1),
        "memory_limit_mb": MEMORY_LIMIT_MB,
        "input_size": 384,
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error": f"{type(error).__name__}: {error}"}))
        raise SystemExit(1)
