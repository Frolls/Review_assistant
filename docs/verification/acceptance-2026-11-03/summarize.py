"""Recompute the frozen control run, keeping missed defects in the denominator."""
from collections import Counter
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[2]
sys.path.insert(0, str(WORKSPACE / "Review_bot"))
from app.services.review_eval import score  # noqa: E402


def read(name):
    return json.loads((HERE / name).read_text())


def stats(values):
    values = sorted(values)
    return {"count": len(values), "median": statistics.median(values),
            "mean": statistics.mean(values), "p95_nearest_rank": values[math.ceil(.95 * len(values)) - 1]}


def usage(name):
    calls = [event for event in read(name) if event.get("event") == "model.call"]
    return {"model_calls": len(calls), "usage_available_calls": sum(bool(c.get("usage_available")) for c in calls),
            "tokens": {key: sum(c.get("usage." + key, 0) for c in calls)
                       for key in ("prompt_tokens", "completion_tokens", "total_tokens")}}


def resources(name):
    data = read(name)
    samples = data["samples"]
    gpus = [gpu for sample in samples for gpu in sample["amd_gpus"] if gpu["card"] == "card1"]
    models = {}
    for sample in samples:
        for model in sample.get("loaded_models") or []:
            models[(model["name"], model.get("context_length"))] = {
                k: model.get(k) for k in ("name", "digest", "context_length", "size_vram")}
    return {"duration_seconds_including_management_waits": data["duration_seconds"],
            "exit_code": data["exit_code"],
            "host_cpu_mean_percent": statistics.mean(s["host_cpu_percent"] for s in samples if s.get("host_cpu_percent") is not None),
            "peak_host_ram_gib": data["peak_host_ram_kib"] / 2**20,
            "peak_device_vram_gib": max(g["mem_info_vram_used"] for g in gpus) / 2**30,
            "gpu_busy_mean_percent": statistics.mean(g["gpu_busy_percent"] for g in gpus),
            "loaded_models": list(models.values())}


protocol = read("protocol.json")
assert hashlib.sha256((HERE / "cases.json").read_bytes()).hexdigest() == protocol["case_sha256"]
rows = read("review-annotated.json")
assert [r["id"] for r in rows] == [r["id"] for r in read("cases.json")]
assert len(rows) == 18
agent = read("agent-live.json")
audit = [json.loads(line) for line in (HERE / "bot-access.jsonl").read_text().splitlines()]
summary = {
    "review": {"metrics": score(rows),
               "counts": {key: sum(r["review"][key] for r in rows) for key in ("true_positives", "false_positives", "false_negatives")},
               "statuses": dict(Counter(r["saved_answer_metadata"]["review_answer"]["status"] for r in rows)),
               "latency_seconds": stats([r["latency_seconds"] for r in rows]),
               "latency_defects_seconds": stats([r["latency_seconds"] for r in rows if r["expected_defects"]]),
               "usage": usage("review-telemetry.json"), "resources": resources("review-resources.json"),
               "strict_source_support": "finally_override: general propagation/cleanup excerpt, not explicit return-in-finally rule"},
    "agent": {"operational_status": agent["status"], "steps": len(agent["steps"]),
              "turn_seconds": stats([s["seconds"] for s in agent["steps"]]),
              "notify_requests": sum(row["path"] == "/notify" for row in audit),
              "stale_request_rejected": agent["clear_invalidates_old_request"],
              "semantic_quality": "one incorrect unsolicited Optional[T] addition in knowledge answer; see agent-evaluation.json",
              "usage": usage("agent-telemetry.json"), "resources": resources("agent-resources.json")},
    "scope": "Frozen new cases, no output-driven production tuning; same author, not independent human assessment. Operational success is not general answer-quality success."}
(HERE / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
with (HERE / "cases.csv").open("w") as output:
    writer = csv.writer(output)
    writer.writerow(["id", "status", "seconds", "tp", "fp", "fn", "fix_usable"])
    for row in rows:
        review = row["review"]
        writer.writerow([row["id"], row["saved_answer_metadata"]["review_answer"]["status"], row["latency_seconds"],
                         review["true_positives"], review["false_positives"], review["false_negatives"], review["fix_usable"]])
print(json.dumps({"review": summary["review"]["metrics"], "agent_operations": summary["agent"]["operational_status"]}, indent=2))
