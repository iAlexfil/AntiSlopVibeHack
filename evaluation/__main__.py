"""CLI оценки качества.

  .venv/bin/python -m evaluation fetch --row-groups 3          # ~300 МБ оригиналов из OpenFake test
  .venv/bin/python -m evaluation run                           # прогон + отчёт в eval_reports/
  .venv/bin/python -m evaluation run --folder my_set           # свой набор: my_set/real, my_set/ai/<генератор>
  .venv/bin/python -m evaluation run --variants original,jpeg75,social1080
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from evaluation.datasets import DATA_DIR, fetch_openfake, load_entries
from evaluation.report import build_report, write_report
from evaluation.runner import VARIANTS, pipeline_version, run

ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT / "eval_reports"


def stratified_sample(entries: list[dict], limit: int) -> list[dict]:
    """Поровну AI/real, внутри класса — по кругу по генераторам/источникам, чтобы редкие группы попали в выборку."""
    def round_robin(items: list[dict]) -> list[dict]:
        groups: dict[str, list[dict]] = {}
        for entry in items:
            groups.setdefault(entry["generator"], []).append(entry)
        queues = list(groups.values())
        ordered = []
        while any(queues):
            ordered += [queue.pop(0) for queue in queues if queue]
        return ordered

    ai = round_robin([entry for entry in entries if entry["label"] == 1])
    real = round_robin([entry for entry in entries if entry["label"] == 0])
    mixed = [entry for pair in zip(ai, real) for entry in pair]
    return mixed[:limit]


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluation", description="Оценка качества AntiSlop")
    commands = parser.add_subparsers(dest="command", required=True)

    fetch = commands.add_parser("fetch", help="скачать часть OpenFake test split")
    fetch.add_argument("--row-groups", type=int, default=3, help="число блоков по ~148 изображений / ~100 МБ")
    fetch.add_argument("--budget-mb", type=int, default=450, help="жёсткий лимит загрузки")
    fetch.add_argument("--seed", type=int, default=0)

    run_parser = commands.add_parser("run", help="прогнать пайплайн и построить отчёт")
    run_parser.add_argument("--manifest", type=Path, action="append", default=[], help="manifest.jsonl (по умолчанию OpenFake)")
    run_parser.add_argument("--folder", type=Path, action="append", default=[], help="папка с подпапками real/ и ai/")
    run_parser.add_argument("--variants", default="original,jpeg75,social1080", help=f"через запятую из: {', '.join(VARIANTS)}")
    run_parser.add_argument("--limit", type=int, help="ограничить число изображений (для быстрой проверки)")
    run_parser.add_argument("--remote", action="store_true", help="вызывать внешние API (отправляет изображения наружу, тратит квоты)")

    args = parser.parse_args()
    if args.command == "fetch":
        fetch_openfake(args.row_groups, args.budget_mb, args.seed)
        return 0

    manifests = args.manifest or ([DATA_DIR / "openfake" / "manifest.jsonl"] if not args.folder else [])
    missing = [path for path in manifests if not path.exists()]
    if missing:
        print(f"Нет manifest: {', '.join(map(str, missing))}. Сначала: python -m evaluation fetch", file=sys.stderr)
        return 1
    variants = [name.strip() for name in args.variants.split(",") if name.strip()]
    unknown = [name for name in variants if name not in VARIANTS]
    if unknown:
        print(f"Неизвестные варианты: {', '.join(unknown)}", file=sys.stderr)
        return 1
    entries = list(load_entries(manifests, args.folder))
    if args.limit:
        entries = stratified_sample(entries, args.limit)
    if not entries:
        print("Набор пуст", file=sys.stderr)
        return 1
    if args.remote:
        print("ВНИМАНИЕ: изображения будут отправлены во внешние API с настроенными ключами.")

    version = pipeline_version()
    suffix = "-remote" if args.remote else ""
    predictions = run(entries, variants, REPORTS_DIR / f"predictions-{version}{suffix}.jsonl", args.remote)
    meta = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "pipeline_version": version,
        "remote": args.remote,
        "sources": sorted({entry["source"] for entry in entries}),
        "images": len(entries),
    }
    path = write_report(build_report(entries, predictions, variants), meta, REPORTS_DIR)
    print(f"Отчёт: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
