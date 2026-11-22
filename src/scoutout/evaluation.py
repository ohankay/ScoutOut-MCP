"""Column-level quality against the synthetic ground truth (spec §9): zone accuracy, confusion matrix, false CLEAN."""

from __future__ import annotations

from collections import Counter

ZONES = ["SENSITIVE", "SPARSE", "CLEAN", "EMPTY", "UNDECIDED"]


def evaluate(manifest: dict, decisions: dict[tuple[str, str], str], include_baseline_only: bool = True) -> dict:
    """``decisions`` maps (schema.table, column) -> zone."""
    confusion: Counter = Counter()
    wrong, false_clean = [], []
    total = 0
    for t in manifest["tables"]:
        table = f"{t['schema']}.{t['name']}"
        for c in t["columns"]:
            if c.get("requires_baseline") and not include_baseline_only:
                continue
            got = decisions.get((table, c["name"]))
            if got is None:
                continue
            total += 1
            exp = c["expected_zone"]
            confusion[(exp, got)] += 1
            if got != exp:
                wrong.append(f"{table}.{c['name']}: {got} (beklenen {exp})")
            if got == "CLEAN" and exp in ("SENSITIVE", "SPARSE"):
                false_clean.append(f"{table}.{c['name']}")
    correct = sum(v for (e, g), v in confusion.items() if e == g)
    return {
        "columns": total, "correct": correct, "accuracy": round(correct / total, 4) if total else 0.0,
        "false_clean": false_clean, "wrong": wrong,
        "confusion": {e: {g: confusion.get((e, g), 0) for g in ZONES} for e in ZONES},
    }


def markdown(title: str, results: dict[str, dict]) -> str:
    lines = [f"# {title}", ""]
    lines.append("| Kurulum | Kolon | Doğru | Doğruluk | Yanlış CLEAN |")
    lines.append("|---|---:|---:|---:|---:|")
    for name, r in results.items():
        lines.append(f"| {name} | {r['columns']} | {r['correct']} | {r['accuracy']:.1%} | {len(r['false_clean'])} |")
    for name, r in results.items():
        lines += ["", f"## {name}", "", "Karışıklık matrisi (satır: beklenen, sütun: sistem):", "",
                  "| | " + " | ".join(ZONES) + " |", "|---|" + "---:|" * len(ZONES)]
        for e in ZONES:
            lines.append(f"| {e} | " + " | ".join(str(r["confusion"][e][g]) for g in ZONES) + " |")
        if r["wrong"]:
            lines += ["", "Yanlış bölgeye düşenler:", ""] + [f"- {w}" for w in r["wrong"]]
    return "\n".join(lines) + "\n"
