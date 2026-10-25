"""Сводный отчёт: общие метрики, срезы по генераторам, источникам реальных фото и лаундерингу."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from evaluation.datasets import SEEN_REAL_SOURCES
from evaluation.metrics import summarize

THRESHOLD = 0.65  # общий порог интерфейса и локальной модели, AGENTS.md раздел 4
MODEL_CUTOFF = "2026-08"  # дата выпуска выбранной модели; позже вышедшие генераторы она не видела
DETECTORS = ("final", "local_model", "pixels", "provenance", "semantic", "external")
DETECTOR_TITLES = {
    "final": "Итоговая вероятность",
    "local_model": "Локальная ML-модель",
    "pixels": "Пиксельные аномалии",
    "provenance": "Вотермарки и происхождение",
    "semantic": "Смысловой анализ",
    "external": "Внешние детекторы",
}


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _released_after(row: dict[str, Any], cutoff: str) -> bool:
    date = str(row.get("release_date") or "")
    return bool(re.match(r"^\d{4}-\d{2}", date)) and date[:7] > cutoff


def _scored(rows: list[dict[str, Any]], detector: str) -> tuple[list[int], list[float], int]:
    usable = [row for row in rows if row.get("status") == "ok" and row.get(detector) is not None]
    return [row["label"] for row in usable], [float(row[detector]) for row in usable], len(rows) - len(usable)


def build_report(entries: list[dict[str, Any]], predictions: dict[tuple[str, str], dict[str, Any]], variants: list[str]) -> dict[str, Any]:
    by_id = {entry["id"]: entry for entry in entries}
    rows_by_variant: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (entry_id, variant), row in predictions.items():
        if entry_id in by_id and variant in variants:
            rows_by_variant[variant].append({**by_id[entry_id], **row})

    report: dict[str, Any] = {"threshold": THRESHOLD, "model_cutoff": MODEL_CUTOFF, "variants": {}}
    for variant in variants:
        rows = rows_by_variant.get(variant, [])
        variant_report: dict[str, Any] = {
            "rejected": sum(row.get("status") == "rejected" for row in rows),
            "detectors": {},
            "slices": {},
        }
        for detector in DETECTORS:
            labels, scores, missing = _scored(rows, detector)
            if labels:
                variant_report["detectors"][detector] = {**summarize(labels, scores, THRESHOLD), "missing": missing}

        clean = [row for row in rows if not (row["label"] == 0 and row["generator"] in SEEN_REAL_SOURCES)]
        unseen = [row for row in rows if row["label"] == 0 or _released_after(row, MODEL_CUTOFF)]
        for name, subset in (("без реальных фото из обучения модели", clean), ("генераторы новее модели + реальные", unseen)):
            labels, scores, _ = _scored(subset, "final")
            if labels and 0 < sum(labels) < len(labels):
                variant_report["slices"][name] = summarize(labels, scores, THRESHOLD)

        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[row["generator"]].append(row)
        per_group = {}
        for generator, subset in sorted(groups.items()):
            label = subset[0]["label"]
            final_labels, final_scores, _ = _scored(subset, "final")
            local_labels, local_scores, _ = _scored(subset, "local_model")
            final_hits = sum((score >= THRESHOLD) == bool(label) for score in final_scores)
            local_hits = sum((score >= THRESHOLD) == bool(label) for score in local_scores)
            per_group[generator] = {
                "label": label,
                "n": len(subset),
                "release_date": next((row.get("release_date") for row in subset if row.get("release_date")), None),
                # Для AI-групп это recall, для реальных — specificity (1 − FPR).
                "final_correct": final_hits / len(final_scores) if final_scores else None,
                "local_correct": local_hits / len(local_scores) if local_scores else None,
                "final_mean": sum(final_scores) / len(final_scores) if final_scores else None,
            }
        variant_report["groups"] = per_group
        report["variants"][variant] = variant_report
    return report


def render_markdown(report: dict[str, Any], meta: dict[str, Any]) -> str:
    lines = [
        "# AntiSlop — отчёт оценки",
        "",
        f"- Дата: {meta['created']}",
        f"- Версия пайплайна: `{meta['pipeline_version']}`",
        f"- Внешние API: {'включены' if meta['remote'] else 'выключены (только локальный анализ)'}",
        f"- Источники: {', '.join(meta['sources'])}",
        f"- Порог решения: {report['threshold']:.2f} (зона «высокая вероятность AI» в интерфейсе)",
        "",
        "> Результаты — скрининговые метрики на небольшой выборке, а не гарантия качества. "
        "Выбранная модель обучалась в том числе на OpenFake и ImageNet, поэтому общие цифры на OpenFake завышены; "
        "смотрите срезы «без реальных фото из обучения» и «генераторы новее модели».",
        "",
    ]
    for variant, data in report["variants"].items():
        lines += [f"## Вариант: `{variant}`", ""]
        if data["rejected"]:
            lines += [f"Отклонено сервером (лимиты размера/формата): {data['rejected']}", ""]
        lines += [
            "| Детектор | n (AI/real) | ROC-AUC [95% CI] | PR-AUC | Recall@0.65 | FPR@0.65 | Bal. acc | FPR при recall 95% | ECE | без ответа |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for detector, stats in data["detectors"].items():
            at = stats["at_threshold"]
            ci = stats["roc_auc_ci95"]
            ci_text = f" [{ci[0]:.3f}–{ci[1]:.3f}]" if ci else ""
            r95 = stats["at_recall_95"]
            lines.append(
                f"| {DETECTOR_TITLES[detector]} | {stats['n_ai']}/{stats['n_real']} | {_fmt(stats['roc_auc'])}{ci_text} | "
                f"{_fmt(stats['pr_auc'])} | {_pct(at['recall'])} | {_pct(at['fpr'])} | {_pct(at['balanced_accuracy'])} | "
                f"{_pct(r95['fpr']) if r95 else '—'} | {_fmt(stats['ece'])} | {stats['missing']} |"
            )
        if data["slices"]:
            lines += ["", "**Срезы итоговой вероятности**", "", "| Срез | n (AI/real) | ROC-AUC | Recall@0.65 | FPR@0.65 |", "|---|---|---|---|---|"]
            for name, stats in data["slices"].items():
                at = stats["at_threshold"]
                lines.append(f"| {name} | {stats['n_ai']}/{stats['n_real']} | {_fmt(stats['roc_auc'])} | {_pct(at['recall'])} | {_pct(at['fpr'])} |")
        lines += [
            "",
            "**По генераторам и источникам** (для AI — доля пойманных, для реальных — доля верно признанных реальными)",
            "",
            "| Группа | Класс | n | Релиз | Итог: верно | Модель: верно | Средний итог |",
            "|---|---|---|---|---|---|---|",
        ]
        for name, group in sorted(data["groups"].items(), key=lambda item: (item[1]["label"], item[1]["final_correct"] or 0)):
            lines.append(
                f"| {name} | {'AI' if group['label'] else 'real'} | {group['n']} | {group['release_date'] or '—'} | "
                f"{_pct(group['final_correct'])} | {_pct(group['local_correct'])} | {_pct(group['final_mean'])} |"
            )
        lines.append("")
    lines += [
        "## Как читать",
        "",
        "- **ROC-AUC** — насколько хорошо score разделяет классы независимо от порога (0.5 = случайно, 1 = идеально).",
        "- **Recall@0.65 / FPR@0.65** — сколько AI поймано и сколько реальных фото ложно помечено при пороге интерфейса.",
        "- **FPR при recall 95%** — цена в ложных срабатываниях, если требовать ловить 95% AI.",
        "- **ECE** — ошибка калибровки: насколько проценты похожи на настоящие вероятности.",
        "- Группы с n < 20 дают очень шумные проценты; не подбирайте пороги под них.",
        "",
    ]
    return "\n".join(lines)


def write_report(report: dict[str, Any], meta: dict[str, Any], out_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = out_dir / stamp
    target.mkdir(parents=True, exist_ok=True)
    (target / "report.json").write_text(json.dumps({"meta": meta, **report}, ensure_ascii=False, indent=2), encoding="utf-8")
    (target / "report.md").write_text(render_markdown(report, meta), encoding="utf-8")
    return target / "report.md"
