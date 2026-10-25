"""Метрики бинарной классификации без sklearn: label 1 = AI, 0 = реальное."""
from __future__ import annotations

from typing import Any

import numpy as np


def roc_auc(labels: np.ndarray, scores: np.ndarray) -> float | None:
    """ROC-AUC через ранговую статистику Манна–Уитни с учётом равных score."""
    labels = np.asarray(labels, dtype=int)
    scores = np.asarray(scores, dtype=float)
    positives = int(labels.sum())
    negatives = len(labels) - positives
    if positives == 0 or negatives == 0:
        return None
    order = np.argsort(scores, kind="mergesort")
    sorted_scores = scores[order]
    ranks = np.empty(len(scores), dtype=float)
    start = 0
    while start < len(scores):
        end = start
        while end + 1 < len(scores) and sorted_scores[end + 1] == sorted_scores[start]:
            end += 1
        ranks[order[start:end + 1]] = (start + end) / 2 + 1
        start = end + 1
    rank_sum = ranks[labels == 1].sum()
    return float((rank_sum - positives * (positives + 1) / 2) / (positives * negatives))


def average_precision(labels: np.ndarray, scores: np.ndarray) -> float | None:
    """PR-AUC как average precision (шаговая интерполяция, как в sklearn)."""
    labels = np.asarray(labels, dtype=int)
    scores = np.asarray(scores, dtype=float)
    positives = int(labels.sum())
    if positives == 0:
        return None
    order = np.argsort(-scores, kind="mergesort")
    sorted_scores = scores[order]
    sorted_labels = labels[order]
    true_positives = np.cumsum(sorted_labels)
    predicted = np.arange(1, len(labels) + 1)
    # Берём только последние позиции групп равных score, чтобы ties не зависели от порядка.
    last_of_group = np.r_[sorted_scores[1:] != sorted_scores[:-1], True]
    precision = true_positives[last_of_group] / predicted[last_of_group]
    recall = true_positives[last_of_group] / positives
    previous_recall = np.r_[0.0, recall[:-1]]
    return float(np.sum((recall - previous_recall) * precision))


def confusion(labels: np.ndarray, scores: np.ndarray, threshold: float) -> dict[str, Any]:
    labels = np.asarray(labels, dtype=int)
    predicted = np.asarray(scores, dtype=float) >= threshold
    tp = int(np.sum(predicted & (labels == 1)))
    fp = int(np.sum(predicted & (labels == 0)))
    tn = int(np.sum(~predicted & (labels == 0)))
    fn = int(np.sum(~predicted & (labels == 1)))
    tpr = tp / (tp + fn) if tp + fn else None
    tnr = tn / (tn + fp) if tn + fp else None
    return {
        "threshold": threshold,
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "recall": tpr,
        "fpr": fp / (fp + tn) if fp + tn else None,
        "precision": tp / (tp + fp) if tp + fp else None,
        "accuracy": (tp + tn) / len(labels) if len(labels) else None,
        "balanced_accuracy": (tpr + tnr) / 2 if tpr is not None and tnr is not None else None,
    }


def fpr_at_recall(labels: np.ndarray, scores: np.ndarray, target_recall: float) -> dict[str, float] | None:
    """Самый высокий порог, при котором recall >= target, и FPR на нём."""
    labels = np.asarray(labels, dtype=int)
    scores = np.asarray(scores, dtype=float)
    if labels.sum() == 0 or labels.sum() == len(labels):
        return None
    for threshold in np.unique(scores)[::-1]:
        stats = confusion(labels, scores, float(threshold))
        if stats["recall"] >= target_recall:
            return {"threshold": float(threshold), "recall": stats["recall"], "fpr": stats["fpr"]}
    return None


def expected_calibration_error(labels: np.ndarray, scores: np.ndarray, bins: int = 10) -> float | None:
    labels = np.asarray(labels, dtype=float)
    scores = np.clip(np.asarray(scores, dtype=float), 0, 1)
    if not len(labels):
        return None
    edges = np.linspace(0, 1, bins + 1)
    indices = np.clip(np.digitize(scores, edges[1:-1]), 0, bins - 1)
    error = 0.0
    for index in range(bins):
        mask = indices == index
        if mask.any():
            error += mask.mean() * abs(scores[mask].mean() - labels[mask].mean())
    return float(error)


def bootstrap_auc_ci(labels: np.ndarray, scores: np.ndarray, rounds: int = 1000, seed: int = 0) -> tuple[float, float] | None:
    labels = np.asarray(labels, dtype=int)
    scores = np.asarray(scores, dtype=float)
    if labels.sum() == 0 or labels.sum() == len(labels):
        return None
    rng = np.random.default_rng(seed)
    values = []
    for _ in range(rounds):
        sample = rng.integers(0, len(labels), len(labels))
        value = roc_auc(labels[sample], scores[sample])
        if value is not None:
            values.append(value)
    return float(np.percentile(values, 2.5)), float(np.percentile(values, 97.5))


def summarize(labels: list[int], scores: list[float], threshold: float) -> dict[str, Any]:
    y = np.asarray(labels, dtype=int)
    s = np.asarray(scores, dtype=float)
    return {
        "n": int(len(y)),
        "n_ai": int(y.sum()),
        "n_real": int(len(y) - y.sum()),
        "roc_auc": roc_auc(y, s),
        "roc_auc_ci95": bootstrap_auc_ci(y, s),
        "pr_auc": average_precision(y, s),
        "ece": expected_calibration_error(y, s),
        "at_threshold": confusion(y, s, threshold),
        "at_recall_95": fpr_at_recall(y, s, 0.95),
    }
