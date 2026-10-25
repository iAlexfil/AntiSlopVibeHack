"""Изолированный процесс анализа серии: те же принципы, что у ml_worker.py (лимиты памяти/CPU, один поток).

stdin: строка JSON {"mode": "scene|person", "names": [...], "sizes": [...]}\n, затем байты файлов подряд.
stdout: JSON-результат multiview.analyze_series или {"error": ...}.
"""
from __future__ import annotations

import hashlib
import json
import os
import resource
import sys
import time

MEMORY_LIMIT_MB = 768
CPU_LIMIT_SECONDS = 40
MAX_IMAGES = 6
MAX_INPUT_BYTES = MAX_IMAGES * 20 * 1024 * 1024 + 65536


def _set_limits() -> None:
    memory = MEMORY_LIMIT_MB * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_LIMIT_SECONDS, CPU_LIMIT_SECONDS + 2))
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "OPENCV_FOR_THREADS_NUM"):
        os.environ.setdefault(name, "1")
    os.environ.setdefault("MALLOC_ARENA_MAX", "2")


def main() -> None:
    _set_limits()
    stream = sys.stdin.buffer
    header = json.loads(stream.readline(65536).decode("utf-8"))
    sizes = [int(size) for size in header["sizes"]]
    if not 2 <= len(sizes) <= MAX_IMAGES or sum(sizes) > MAX_INPUT_BYTES:
        raise ValueError("invalid series size")
    items = [(str(name), stream.read(size)) for name, size in zip(header["names"], sizes)]

    import multiview
    if header["mode"] == "person":
        for path, expected in ((multiview.YUNET_PATH, multiview.YUNET_SHA256), (multiview.SFACE_PATH, multiview.SFACE_SHA256)):
            if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise RuntimeError(f"Модель {path.name} отсутствует или checksum не совпадает")

    started = time.monotonic()
    result = multiview.analyze_series(items, header["mode"])
    result["latency_ms"] = round((time.monotonic() - started) * 1000)
    result["peak_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    result["memory_limit_mb"] = MEMORY_LIMIT_MB
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error": f"{type(error).__name__}: {error}"}, ensure_ascii=False))
        raise SystemExit(1)
