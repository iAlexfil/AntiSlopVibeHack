"""Источники evaluation-данных: частичная выгрузка OpenFake и локальные папки.

Формат manifest.jsonl (одна запись на изображение):
  id, path, label (1 = AI, 0 = real), source, generator, release_date, sha256, license, origin
"""
from __future__ import annotations

import hashlib
import io
import json
import random
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "eval_data"

OPENFAKE_REPO = "ComplexDataLab/OpenFake"
OPENFAKE_LICENSE = "CC-BY-NC-4.0"
OPENFAKE_TEST_SHARDS = 13
OPENFAKE_COLUMNS = ["image", "label", "model", "type", "release_date", "row_id", "image_sha256"]
# Реальные источники OpenFake, которые выбранная модель видела при обучении (см. AGENTS.md, раздел 5).
SEEN_REAL_SOURCES = {"imagenet"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".bmp"}


class HttpRangeFile(io.RawIOBase):
    """Seekable file-like поверх HTTP Range: pyarrow читает только footer и нужные column chunks."""

    def __init__(self, url: str) -> None:
        super().__init__()
        request = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(request, timeout=60) as response:
            self.url = response.url  # подписанный CDN URL после редиректа
            self.size = int(response.headers["Content-Length"])
        self.position = 0
        self.downloaded = 0

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = 0) -> int:
        base = {0: 0, 1: self.position, 2: self.size}[whence]
        self.position = base + offset
        return self.position

    def read(self, size: int = -1) -> bytes:
        end = self.size if size < 0 else min(self.size, self.position + size)
        if end <= self.position:
            return b""
        headers = {"Range": f"bytes={self.position}-{end - 1}"}
        for attempt in range(4):
            try:
                with urllib.request.urlopen(urllib.request.Request(self.url, headers=headers), timeout=120) as response:
                    chunk = response.read()
                break
            except (urllib.error.URLError, TimeoutError):
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
        self.position += len(chunk)
        self.downloaded += len(chunk)
        return chunk

    def readinto(self, buffer: Any) -> int:
        chunk = self.read(len(buffer))
        buffer[:len(chunk)] = chunk
        return len(chunk)


def _openfake_revision() -> str:
    with urllib.request.urlopen(f"https://huggingface.co/api/datasets/{OPENFAKE_REPO}", timeout=60) as response:
        return json.load(response)["sha"]


def _image_suffix(data: bytes) -> str:
    image_format = (Image.open(io.BytesIO(data)).format or "bin").lower()
    return {"jpeg": "jpg", "tiff": "tif"}.get(image_format, image_format)


def fetch_openfake(row_groups: int = 3, budget_mb: int = 450, seed: int = 0, out_dir: Path | None = None) -> Path:
    """Скачивает несколько row group'ов из test split OpenFake с оригинальными байтами изображений.

    Один row group ~148 изображений и ~100 МБ. Выгрузка останавливается до превышения budget_mb.
    """
    import pyarrow.parquet as pq  # опциональная зависимость, см. requirements-eval.txt

    out_dir = out_dir or DATA_DIR / "openfake"
    images_dir = out_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.jsonl"
    known = {entry["id"] for entry in read_manifest(manifest_path)} if manifest_path.exists() else set()
    revision = _openfake_revision()
    rng = random.Random(seed)
    shards = rng.sample(range(OPENFAKE_TEST_SHARDS), k=min(row_groups, OPENFAKE_TEST_SHARDS))
    while len(shards) < row_groups:
        shards.append(rng.randrange(OPENFAKE_TEST_SHARDS))

    budget = budget_mb * 1024 * 1024
    downloaded = 0
    added = 0
    with manifest_path.open("a", encoding="utf-8") as manifest:
        for shard in shards:
            name = f"core/test-{shard:05d}-of-{OPENFAKE_TEST_SHARDS:05d}.parquet"
            remote = HttpRangeFile(f"https://huggingface.co/datasets/{OPENFAKE_REPO}/resolve/{revision}/{name}")
            parquet = pq.ParquetFile(remote)
            group = rng.randrange(parquet.metadata.num_row_groups)
            group_size = parquet.metadata.row_group(group).total_byte_size
            if downloaded + group_size > budget:
                print(f"Пропуск {name}#{group}: превысит бюджет {budget_mb} МБ")
                continue
            print(f"Загрузка {name} row group {group} (~{group_size / 2**20:.0f} МБ)…", flush=True)
            before = remote.downloaded
            table = parquet.read_row_group(group, columns=OPENFAKE_COLUMNS)
            downloaded += remote.downloaded - before
            for row in table.to_pylist():
                data = row["image"]["bytes"]
                sha256 = hashlib.sha256(data).hexdigest()
                if sha256 in known:
                    continue
                if row["image_sha256"] and row["image_sha256"] != sha256:
                    print(f"  sha256 не совпадает для {row['row_id']}, пропуск")
                    continue
                path = images_dir / f"{sha256}.{_image_suffix(data)}"
                path.write_bytes(data)
                entry = {
                    "id": sha256,
                    "path": str(path.relative_to(ROOT)),
                    "label": 1 if row["label"] == "fake" else 0,
                    "source": "openfake",
                    "generator": row["model"],
                    "media_type": row["type"],
                    "release_date": None if str(row["release_date"]).lower() in ("none", "", "unknown") else row["release_date"],
                    "sha256": sha256,
                    "license": OPENFAKE_LICENSE,
                    "origin": f"{OPENFAKE_REPO}@{revision[:12]}:{name}#rg{group}:{row['row_id']}",
                }
                manifest.write(json.dumps(entry, ensure_ascii=False) + "\n")
                known.add(sha256)
                added += 1
    print(f"Добавлено {added} изображений, скачано {downloaded / 2**20:.0f} МБ → {manifest_path}")
    return manifest_path


def read_manifest(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def folder_manifest(folder: Path) -> list[dict[str, Any]]:
    """Локальный набор: folder/real/** и folder/ai/** (или fake/). Подпапка ai/<генератор>/ задаёт generator."""
    entries = []
    for class_name, label in (("real", 0), ("ai", 1), ("fake", 1)):
        class_dir = folder / class_name
        if not class_dir.is_dir():
            continue
        for path in sorted(class_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            relative = path.relative_to(class_dir)
            sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append({
                "id": sha256,
                "path": str(path.resolve()),
                "label": label,
                "source": folder.name,
                "generator": relative.parts[0] if len(relative.parts) > 1 else ("real" if label == 0 else "unknown"),
                "media_type": "real" if label == 0 else "image",
                "release_date": None,
                "sha256": sha256,
                "license": "local",
                "origin": str(path),
            })
    return entries


def load_entries(manifests: list[Path], folders: list[Path]) -> Iterator[dict[str, Any]]:
    seen: set[str] = set()
    for entry in [*(e for m in manifests for e in read_manifest(m)), *(e for f in folders for e in folder_manifest(f))]:
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])
        path = Path(entry["path"])
        entry["path"] = str(path if path.is_absolute() else ROOT / path)
        yield entry
