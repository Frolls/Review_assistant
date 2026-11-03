"""Measure the host serving a local model during a specified benchmark command.

Example: python scripts/measure_local.py --output resources.json -- python scripts/smoke_pair.py
Host-wide CPU/RAM/GPU samples include other processes; run on an idle model host.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time
from datetime import UTC, datetime
from urllib.request import urlopen


def sample(ollama_url=None):
    cpu = [int(n) for n in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
    mem = {line.split(":")[0]: int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines()}
    value = {"time": time.monotonic(), "cpu_total": sum(cpu), "cpu_idle": cpu[3] + cpu[4],
             "ram_used_kib": mem["MemTotal"] - mem["MemAvailable"], "load1": os.getloadavg()[0]}
    value["utc"] = datetime.now(UTC).isoformat()
    value["amd_gpus"] = []
    for card in Path("/sys/class/drm").glob("card[0-9]"):
        device = card / "device"
        gpu = {"card": card.name}
        for key in ("gpu_busy_percent", "mem_info_vram_used", "mem_info_vram_total", "mem_info_gtt_used"):
            try:
                gpu[key] = int((device / key).read_text())
            except (OSError, ValueError):
                pass
        for field in ("average", "input"):
            files = list(device.glob(f"hwmon/hwmon*/power1_{field}"))
            if files:
                try:
                    gpu["power_watts"] = int(files[0].read_text()) / 1_000_000
                    break
                except (OSError, ValueError):
                    pass
        if len(gpu) > 1:
            value["amd_gpus"].append(gpu)
    value["ollama_processes"] = []
    for path in Path("/proc").glob("[0-9]*"):
        try:
            name = (path / "comm").read_text().strip()
            if "ollama" not in name and "llama" not in name:
                continue
            stat = (path / "stat").read_text().rpartition(") ")[2].split()
            value["ollama_processes"].append({"pid": int(path.name), "name": name,
                "cpu_ticks": int(stat[11]) + int(stat[12]),
                "rss_bytes": int(stat[21]) * os.sysconf("SC_PAGE_SIZE")})
        except (OSError, ValueError, IndexError):
            pass
    if ollama_url:
        try:
            with urlopen(ollama_url.rstrip("/") + "/api/ps", timeout=2) as response:
                value["loaded_models"] = json.load(response)["models"]
        except (OSError, ValueError):
            value["loaded_models"] = None
    if shutil.which("nvidia-smi"):
        try:
            gpu = subprocess.run(["nvidia-smi", "--query-gpu=index,utilization.gpu,memory.used,power.draw", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=2)
            value["gpu_csv"] = gpu.stdout.strip() if gpu.returncode == 0 else None
        except subprocess.TimeoutExpired:
            value["gpu_csv"] = None
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--ollama-url")
    parser.add_argument("--baseline-seconds", type=float, default=0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("Provide a benchmark command after --")
    baseline = []
    baseline_end = time.monotonic() + args.baseline_seconds
    while time.monotonic() < baseline_end:
        baseline.append(sample(args.ollama_url))
        time.sleep(.5)
    started = time.monotonic()
    samples = [sample(args.ollama_url)]
    process = subprocess.Popen(command)
    try:
        while process.poll() is None:
            time.sleep(.5)
            samples.append(sample(args.ollama_url))
    except BaseException:
        process.terminate()
        process.wait(timeout=5)
        raise
    for previous, current in zip(samples, samples[1:]):
        total = current["cpu_total"] - previous["cpu_total"]
        current["host_cpu_percent"] = 100 * (1 - (current["cpu_idle"] - previous["cpu_idle"]) / total) if total else None
    report = {"platform": platform.platform(), "cpu_count": os.cpu_count(), "duration_seconds": time.monotonic() - started,
              "exit_code": process.returncode, "peak_host_ram_kib": max(s["ram_used_kib"] for s in samples),
              "clock_ticks_per_second": os.sysconf("SC_CLK_TCK"),
              "scope": "host-wide; includes other processes; API price does not measure compute cost",
              "baseline": baseline, "samples": samples}
    Path(args.output).write_text(json.dumps(report, indent=2))
    raise SystemExit(process.returncode)


if __name__ == "__main__":
    main()
