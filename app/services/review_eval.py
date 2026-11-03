"""Metrics from independent review annotations, not model self-assessment."""

def score(rows):
    if not rows:
        raise ValueError("Нет оценённых примеров.")
    if any(row["review"][key] is None for row in rows for key in ("true_positives", "false_positives", "false_negatives", "fix_usable")):
        raise ValueError("Сначала заполните независимую ручную оценку каждого ответа.")
    for row in rows:
        review = row["review"]
        if any(type(review[k]) is not int or review[k] < 0 for k in ("true_positives", "false_positives", "false_negatives")) or type(review["fix_usable"]) is not bool:
            raise ValueError("Оценки TP/FP/FN должны быть неотрицательными целыми, fix_usable — bool.")
    tp = sum(row["review"]["true_positives"] for row in rows)
    fp = sum(row["review"]["false_positives"] for row in rows)
    fn = sum(row["review"]["false_negatives"] for row in rows)
    clean = [row for row in rows if not row["expected_defects"]]
    defects = [row for row in rows if row["expected_defects"]]
    return {"precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "clean_false_positive_rate": sum(row["review"]["false_positives"] > 0 for row in clean) / len(clean) if clean else None,
            "usable_fix_rate": sum(bool(row["review"]["fix_usable"]) for row in defects) / len(defects) if defects else None,
            "cases": len(rows)}
