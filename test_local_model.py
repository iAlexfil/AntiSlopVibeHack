"""Worker resource supervision and bounded preprocessing regression checks."""
import io
import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import psutil
from PIL import Image

import local_model
import ml_worker


def image_bytes(size=(640, 480), image_format='PNG'):
    image = Image.new('RGB', size, '#887766')
    stream = io.BytesIO()
    image.save(stream, image_format)
    return stream.getvalue()


def test_platform_limits():
    for platform in ('darwin', 'linux'):
        with patch.object(ml_worker.sys, 'platform', platform), patch.object(ml_worker.resource, 'setrlimit') as limit:
            ml_worker._set_limits()
        resources = [call.args[0] for call in limit.call_args_list]
        assert ml_worker.resource.RLIMIT_CPU in resources
        assert (ml_worker.resource.RLIMIT_AS in resources) == (platform != 'darwin')


def test_preprocess_is_bounded_and_preserves_center_crop():
    random = np.random.default_rng(41)
    for width, height in ((640, 480), (1001, 333), (320, 320), (300, 800)):
        original = Image.fromarray(random.integers(0, 256, (height, width, 3), dtype=np.uint8))
        stream = io.BytesIO()
        original.save(stream, 'PNG')
        scale = 440 / min(width, height)
        resized = original.resize((round(width * scale), round(height * scale)), Image.Resampling.BICUBIC)
        left, top = (resized.width - 384) // 2, (resized.height - 384) // 2
        old = np.asarray(resized.crop((left, top, left + 384, top + 384)), dtype=np.float32) / 255
        old = ((old - [0.485, 0.456, 0.406]) / [0.229, 0.224, 0.225]).transpose(2, 0, 1)[None]
        new = ml_worker._preprocess(stream.getvalue())
        assert new.shape == (1, 3, 384, 384)
        assert np.max(np.abs(new - old)) < 1e-6
    for size in ((1, 10000), (10000, 1)):
        tensor = ml_worker._preprocess(image_bytes(size))
        assert tensor.shape == (1, 3, 384, 384) and np.isfinite(tensor).all()
    with patch.object(ml_worker, 'MAX_IMAGE_PIXELS', 10):
        try:
            ml_worker._preprocess(image_bytes((10, 10)))
        except ValueError:
            pass
        else:
            raise AssertionError('Oversized decoded image accepted')


class Worker:
    pid = 123
    def __init__(self, block=False):
        self.returncode = None
        self.killed = False
        self.calls = []
        self.block = block
        self.terminated = threading.Event()
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def poll(self): return self.returncode
    def kill(self):
        self.killed = True
        self.returncode = -9
        self.terminated.set()
    def communicate(self, input=None, timeout=None):
        self.calls.append((input, timeout))
        if self.block:
            assert self.terminated.wait(2), 'Worker must be stopped before communication is joined'
        if not self.killed:
            self.returncode = 0
        return b'{}', b''


def test_watchdog_cleanup_and_stdin():
    for situation in ('ok', 'memory', 'denied', 'gone', 'timeout'):
        worker = Worker(block=situation in ('memory', 'denied', 'timeout'))
        monitored = Mock()
        monitored.memory_info.return_value = SimpleNamespace(rss=100)
        if situation == 'memory':
            monitored.memory_info.return_value.rss = 1024 * 1024 * 1024
        elif situation == 'denied':
            monitored.memory_info.side_effect = psutil.AccessDenied(123)
        elif situation == 'gone':
            monitored.memory_info.side_effect = psutil.NoSuchProcess(123)
        with patch.object(local_model.subprocess, 'Popen', return_value=worker), \
                patch.object(local_model.psutil, 'Process', return_value=monitored), \
                patch.object(local_model, 'WORKER_TIMEOUT', -1 if situation == 'timeout' else 28):
            try:
                local_model._run_worker(b'image', {})
            except (MemoryError, psutil.AccessDenied, subprocess.TimeoutExpired):
                assert situation in ('memory', 'denied', 'timeout')
            else:
                assert situation in ('ok', 'gone')
        assert worker.killed == (situation in ('memory', 'denied', 'timeout'))
        assert worker.calls == [(b'image', None)], 'One call must send stdin and drain both output pipes'


def test_large_stdin_survives_watchdog_checks():
    with tempfile.TemporaryDirectory() as directory:
        script = Path(directory) / 'worker.py'
        script.write_text('import sys, time, json\ntime.sleep(0.2)\ndata=sys.stdin.buffer.read()\nprint(json.dumps({"bytes":len(data)}))\n')
        data = b'x' * (5 * 1024 * 1024)
        with patch.object(local_model, 'WORKER_PATH', script), patch.object(local_model, 'WORKER_TIMEOUT', 2):
            result = local_model._run_worker(data, os.environ.copy())
        assert result.returncode == 0
        assert json.loads(result.stdout)['bytes'] == len(data), 'A delayed reader must receive the entire image'


def test_real_watchdog_stops_worker():
    with tempfile.TemporaryDirectory() as directory:
        script = Path(directory) / 'worker.py'
        script.write_text('import sys, time\nsys.stdin.buffer.read()\ndata=bytearray(64*1024*1024)\ntime.sleep(5)\n')
        with patch.object(local_model, 'WORKER_PATH', script), patch.object(local_model, 'MEMORY_LIMIT_MB', 32):
            try:
                local_model._run_worker(b'image', os.environ.copy())
            except MemoryError:
                pass
            else:
                raise AssertionError('Worker exceeding RSS limit was not stopped')


def test_real_inference():
    for image_format in ('PNG', 'JPEG'):
        result = local_model.run_local_model(image_bytes(image_format=image_format))
        assert result['status'] == 'ok', result
        assert 0 <= result['score'] <= 1
        assert 0 < result['peak_rss_mb'] < result['memory_limit_mb'] == 768
        assert result['latency_ms'] < 28000
        print(f"{image_format}: {result['latency_ms']} ms, peak RSS {result['peak_rss_mb']} MiB")


if __name__ == '__main__':
    test_platform_limits()
    test_preprocess_is_bounded_and_preserves_center_crop()
    test_watchdog_cleanup_and_stdin()
    test_large_stdin_survives_watchdog_checks()
    test_real_watchdog_stops_worker()
    test_real_inference()
    print('local model resource and inference tests passed')
