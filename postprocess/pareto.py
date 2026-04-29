#!/usr/bin/env python3
"""Merge per-config benchmark results into a TPOT vs tok/sec/GPU Pareto chart.

Usage:
    python3 pareto.py /mnt/lustre/nightly/results/2026-04-27

Reads:
    - metadata.json (config list with num_gpus)
    - <config>/staircase/summary.json (nyann-bench JSON summary per config)

Outputs:
    - pareto.csv   (one row per config × stage)
    - pareto.png   (scatter + Pareto frontier)
"""

import csv
import json
import os
import sys
from pathlib import Path


def load_summary(summary_path: Path) -> list[dict]:
    """Parse a nyann-bench summary JSON and extract per-stage stats."""
    with open(summary_path) as f:
        content = f.read().strip()

    # nyann-bench collect outputs one JSON block per pod, separated by
    # "--- pod-name ---" headers. Find JSON blocks.
    stages = []
    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("---"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Each summary has "stages" with per-stage metrics
        if "stages" in data:
            for s in data["stages"]:
                stages.append(s)
        elif "summary" in data and "stages" in data["summary"]:
            for s in data["summary"]["stages"]:
                stages.append(s)

    return stages


def compute_pareto_points(
    run_dir: Path,
) -> list[dict]:
    """Compute (tpot, tok/sec/gpu) for each config × stage."""
    meta_path = run_dir / "metadata.json"
    if not meta_path.exists():
        print(f"ERROR: {meta_path} not found", file=sys.stderr)
        sys.exit(1)

    with open(meta_path) as f:
        metadata = json.load(f)

    points = []

    for cfg in metadata.get("configs", []):
        config_name = cfg["name"]
        num_gpus = cfg["num_gpus"]
        summary_path = run_dir / config_name / "staircase" / "summary.json"

        if not summary_path.exists():
            print(f"WARN: {summary_path} not found, skipping", file=sys.stderr)
            continue

        stages = load_summary(summary_path)
        if not stages:
            print(f"WARN: no stages found in {summary_path}", file=sys.stderr)
            continue

        for stage in stages:
            concurrency = stage.get("concurrency", 0)
            duration_s = stage.get("duration_seconds", stage.get("duration", 0))
            total_tokens = stage.get("total_output_tokens", 0)
            num_requests = stage.get("num_requests", stage.get("completed", 0))

            # TPOT: median inter-token latency across requests
            tpot_p50 = stage.get("itl_p50", stage.get("itl_ms_p50", 0))
            tpot_p95 = stage.get("itl_p95", stage.get("itl_ms_p95", 0))
            tpot_p99 = stage.get("itl_p99", stage.get("itl_ms_p99", 0))

            ttft_p50 = stage.get("ttft_p50", stage.get("ttft_ms_p50", 0))
            ttft_p95 = stage.get("ttft_p95", stage.get("ttft_ms_p95", 0))

            # tok/sec/GPU: aggregate throughput normalized by GPU count
            tok_per_sec = total_tokens / duration_s if duration_s > 0 else 0
            tok_per_sec_per_gpu = tok_per_sec / num_gpus if num_gpus > 0 else 0

            points.append({
                "config": config_name,
                "num_gpus": num_gpus,
                "concurrency": concurrency,
                "tpot_p50_ms": tpot_p50,
                "tpot_p95_ms": tpot_p95,
                "tpot_p99_ms": tpot_p99,
                "ttft_p50_ms": ttft_p50,
                "ttft_p95_ms": ttft_p95,
                "tok_per_sec": round(tok_per_sec, 1),
                "tok_per_sec_per_gpu": round(tok_per_sec_per_gpu, 1),
                "total_output_tokens": total_tokens,
                "num_requests": num_requests,
                "duration_s": duration_s,
            })

    return points


def pareto_frontier(points: list[dict]) -> list[dict]:
    """Extract the Pareto-optimal points (minimize TPOT, maximize tok/sec/GPU)."""
    sorted_pts = sorted(points, key=lambda p: p["tpot_p50_ms"])
    frontier = []
    max_efficiency = -1
    for pt in sorted_pts:
        if pt["tok_per_sec_per_gpu"] > max_efficiency:
            max_efficiency = pt["tok_per_sec_per_gpu"]
            frontier.append(pt)
    return frontier


def write_csv(points: list[dict], path: Path):
    if not points:
        return
    fieldnames = list(points[0].keys())
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(points)


def plot_pareto(points: list[dict], frontier: list[dict], path: Path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("WARN: matplotlib not available, skipping plot", file=sys.stderr)
        return

    configs = sorted(set(p["config"] for p in points))
    colors = plt.cm.tab10(np.linspace(0, 1, len(configs)))
    config_colors = dict(zip(configs, colors))

    fig, ax = plt.subplots(figsize=(12, 8))

    for config in configs:
        cfg_points = [p for p in points if p["config"] == config]
        x = [p["tpot_p50_ms"] for p in cfg_points]
        y = [p["tok_per_sec_per_gpu"] for p in cfg_points]
        ax.scatter(x, y, color=config_colors[config], label=config, s=80, zorder=3)

        # Connect stages within same config
        sorted_cfg = sorted(cfg_points, key=lambda p: p["concurrency"])
        x_line = [p["tpot_p50_ms"] for p in sorted_cfg]
        y_line = [p["tok_per_sec_per_gpu"] for p in sorted_cfg]
        ax.plot(x_line, y_line, color=config_colors[config], alpha=0.3, linewidth=1)

        # Annotate concurrency
        for p in cfg_points:
            ax.annotate(
                str(p["concurrency"]),
                (p["tpot_p50_ms"], p["tok_per_sec_per_gpu"]),
                textcoords="offset points",
                xytext=(5, 5),
                fontsize=7,
                alpha=0.7,
            )

    # Pareto frontier line
    if frontier:
        fx = [p["tpot_p50_ms"] for p in frontier]
        fy = [p["tok_per_sec_per_gpu"] for p in frontier]
        ax.plot(fx, fy, "k--", linewidth=2, alpha=0.5, label="Pareto frontier")

    ax.set_xlabel("TPOT p50 (ms)", fontsize=12)
    ax.set_ylabel("tok/sec/GPU", fontsize=12)
    ax.set_title("Nightly Efficiency Pareto Frontier", fontsize=14)
    ax.legend(loc="best")
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(path, dpi=150)
    plt.close()
    print(f"Pareto chart saved to {path}")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <run_dir>", file=sys.stderr)
        sys.exit(1)

    run_dir = Path(sys.argv[1])
    points = compute_pareto_points(run_dir)

    if not points:
        print("No data points found.", file=sys.stderr)
        sys.exit(1)

    frontier = pareto_frontier(points)

    csv_path = run_dir / "pareto.csv"
    write_csv(points, csv_path)
    print(f"Wrote {len(points)} data points to {csv_path}")

    png_path = run_dir / "pareto.png"
    plot_pareto(points, frontier, png_path)

    # Also write frontier as separate CSV
    frontier_path = run_dir / "pareto_frontier.csv"
    write_csv(frontier, frontier_path)
    print(f"Wrote {len(frontier)} frontier points to {frontier_path}")


if __name__ == "__main__":
    main()
