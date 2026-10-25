from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import psutil

from ml_worker import MAX_INPUT_BYTES, MEMORY_LIMIT_MB


ROOT = Path(__file__).resolve().parent
MODEL_PATH = ROOT / "models" / "frontier-detector-fp16.onnx"
MODEL_SHA256 = "d75791ba2fa59146025d342cfaafa9ddeab24af117642a94f752ee4c1619375d"
WORKER_PATH = ROOT / "ml_worker.py"
WORKER_TIMEOUT = 28
MEMORY_POLL_SECONDS = 0.05


def _run_worker(data: bytes, environment: dict[str, str]) -> subprocess.CompletedProcess:
    command = [sys.executable, str(WORKER_PATH), str(MODEL_PATH)]
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=environment) as process:
        try:
            monitored = psutil.Process(process.pid)
            deadline = time.monotonic() + WORKER_TIMEOUT
            pending = data
            while True:
                try:
                    if monitored.memory_info().rss > MEMORY_LIMIT_MB * 1024 * 1024:
                        raise MemoryError
                except psutil.NoSuchProcess:
                    pass  # The worker may have exited before the next RSS sample.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, WORKER_TIMEOUT)
                try:
                    stdout, stderr = process.communicate(input=pending, timeout=min(MEMORY_POLL_SECONDS, remaining))
                    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    pending = None
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()


@lru_cache(maxsize=1)
def model_status() -> dict[str, Any]:
    if not MODEL_PATH.exists():
        return {"available": False, "detail": "Модель не загружена"}
    digest = hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest()
    if digest != MODEL_SHA256:
        return {"available": False, "detail": "Checksum модели не совпадает"}
    return {
        "available": True,
        "path": str(MODEL_PATH),
        "sha256": digest,
        "size_mb": round(MODEL_PATH.stat().st_size / (1024 * 1024), 1),
    }


def run_local_model(data: bytes) -> dict[str, Any]:
    identity = {"id": "local_frontier", "name": "Frontier ViT (локально)"}
    if not data or len(data) > MAX_INPUT_BYTES:
        return {**identity, "status": "error", "detail": "Некорректный размер изображения для локальной модели"}
    status = model_status()
    if not status["available"]:
        return {**identity, "status": "unavailable", "detail": status["detail"]}
    environment = os.environ.copy()
    environment.update({
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "MALLOC_ARENA_MAX": "2",
    })
    try:
        process = _run_worker(data, environment)
    except subprocess.TimeoutExpired:
        return {**identity, "status": "error", "detail": "Локальная модель превысила таймаут 28 секунд"}
    except MemoryError:
        return {**identity, "status": "error", "detail": f"Локальная модель превысила лимит памяти {MEMORY_LIMIT_MB} МБ"}
    except psutil.Error:
        return {**identity, "status": "error", "detail": "Не удалось контролировать память локальной модели"}
    except OSError:
        return {**identity, "status": "error", "detail": "Не удалось запустить локальную модель"}
    try:
        payload = json.loads(process.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {**identity, "status": "error", "detail": process.stderr.decode("utf-8", errors="ignore")[-240:] or "Некорректный ответ ML-воркера"}
    if process.returncode != 0 or "error" in payload:
        return {**identity, "status": "error", "detail": payload.get("error") or "ML-воркер завершился с ошибкой"}
    probability = float(payload["probability"])
    return {
        "status": "ok",
        **identity,
        "score": round(probability, 4),
        "verdict": "ai" if probability >= float(payload["threshold"]) else "human",
        "threshold": payload["threshold"],
        "latency_ms": payload["latency_ms"],
        "peak_rss_mb": payload["peak_rss_mb"],
        "memory_limit_mb": payload["memory_limit_mb"],
        "input_size": payload["input_size"],
        "model_size_mb": status["size_mb"],
        "detail": "Локальная ONNX-модель; скрининговый сигнал, а не доказательство происхождения.",
    }
