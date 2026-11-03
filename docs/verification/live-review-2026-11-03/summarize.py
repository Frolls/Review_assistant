"""Rebuild numerical summaries from retained raw measurements and annotations."""
from collections import Counter
import csv
import json
import math
from pathlib import Path
import statistics
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "Review_bot"))
from app.services.review_eval import score  # noqa: E402


def read(name):
    return json.loads((HERE / name).read_text())


def distribution(values):
    values = sorted(values)
    return {"n": len(values), "mean": statistics.mean(values),
            "p50": statistics.median(values),
            "p95_nearest_rank": values[math.ceil(.95 * len(values)) - 1], "max": max(values)}


def resources(name):
    report = read(f"resources-{name}.json")
    samples, baseline = report["samples"], report["baseline"]
    intervals = [(a, b, b["time"] - a["time"]) for a, b in zip(samples, samples[1:])]
    duration = sum(dt for _, _, dt in intervals)
    result = {"duration_seconds": report["duration_seconds"], "samples": len(samples),
              "host_cpu_percent": distribution([s["host_cpu_percent"] for s in samples if s.get("host_cpu_percent") is not None]),
              "peak_host_ram_gib": report["peak_host_ram_kib"] / 1048576,
              "idle_host_ram_gib": statistics.mean(s["ram_used_kib"] for s in baseline) / 1048576,
              "peak_sum_ollama_rss_gib": max(sum(p["rss_bytes"] for p in s["ollama_processes"]) for s in samples) / 2**30,
              "gpus": {}, "loaded_models": []}
    for card in sorted({g["card"] for s in samples for g in s["amd_gpus"]}):
        def gpu(sample):
            return next(g for g in sample["amd_gpus"] if g["card"] == card)
        gs = [gpu(s) for s in samples]
        entry = {"peak_vram_gib": max(g["mem_info_vram_used"] for g in gs) / 2**30,
                 "idle_vram_gib": statistics.mean(gpu(s)["mem_info_vram_used"] for s in baseline) / 2**30,
                 "busy_percent": distribution([g["gpu_busy_percent"] for g in gs])}
        if all("power_watts" in g for g in gs):
            joules = sum((gpu(a)["power_watts"] + gpu(b)["power_watts"]) / 2 * dt for a, b, dt in intervals)
            idle = statistics.mean(gpu(s)["power_watts"] for s in baseline)
            entry.update({"mean_power_watts_time_weighted": joules / duration,
                          "peak_power_watts": max(g["power_watts"] for g in gs),
                          "idle_power_watts": idle, "energy_wh": joules / 3600,
                          "energy_above_idle_wh_estimate": (joules - idle * duration) / 3600})
        result["gpus"][card] = entry
    models = {}
    for sample in samples:
        for model in sample.get("loaded_models") or []:
            key = (model["name"], model["digest"], model["context_length"])
            models[key] = {k: model[k] for k in ("name", "digest", "context_length", "size_vram")}
    result["loaded_models"] = list(models.values())
    return result


summary = {"quality_annotation": "Automated rubric-based evaluation, not blind human review",
           "latency_percentile_method": "median; p95 nearest rank, n=9 implies maximum",
           "resources_scope": "host-wide; desktop and other processes included; GPU sensor energy is not wall-plug energy",
           "runs": {}, "inference": {}}
case_rows = []
for name in ("baseline", "repeat"):
    rows = read(f"{name}-annotated.json")
    states = [r["saved_answer_metadata"].get("review_answer", {}).get("status", "error") for r in rows]
    calls = [c for c in read(f"{name}-telemetry.json") if c.get("event") == "model.call"]
    result = {"metrics": score(rows), "statuses": dict(Counter(states)),
              "latency_seconds_all": distribution([r["latency_seconds"] for r in rows]),
              "answered_fraction": states.count("answered") / len(rows),
              "clean_accepted_without_findings": 0,
              "completed_model_calls": len(calls),
              "model_usage_excludes_embeddings_and_incomplete_calls": {
                  key: sum(c.get("usage." + key, 0) for c in calls)
                  for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
              "resources": resources(name)}
    summary["runs"][name] = result
    for row, state in zip(rows, states):
        events = row["event_timings"]
        case_rows.append({"run": name, "case": row["id"], "status": state,
                          "latency_seconds": row["latency_seconds"],
                          "first_event_seconds": events[0]["seconds"] if events else None,
                          "first_text_seconds": next((e["seconds"] for e in events if e["type"] == "token"), None),
                          **{k: v for k, v in row["review"].items() if k != "notes"}})
for think in (False, True):
    rows = [r for r in read("inference-raw.json")["runs"] if r["think"] == think and not r["warmup"]]
    summary["inference"][str(think).lower()] = {
        "repetitions": len(rows), "wall_seconds": distribution([r["wall_seconds"] for r in rows]),
        "decode_tokens_per_second": distribution([r["eval_tokens_per_second"] for r in rows]),
        "output_tokens": [r["response"]["eval_count"] for r in rows],
        "prompt_tokens": [r["response"]["prompt_eval_count"] for r in rows],
        "load_seconds": [r["response"]["load_duration"] / 1e9 for r in rows],
        "finish_reasons": [r["response"].get("done_reason") for r in rows]}
summary["inference"]["resources"] = resources("inference")
(HERE / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
with (HERE / "cases.csv").open("w") as output:
    writer = csv.DictWriter(output, fieldnames=list(case_rows[0]))
    writer.writeheader()
    writer.writerows(case_rows)
print(json.dumps({"runs": {k: {field: v[field] for field in ("metrics", "statuses", "latency_seconds_all")} for k, v in summary["runs"].items()},
                  "inference": {k: v for k, v in summary["inference"].items() if k != "resources"}}, indent=2))
