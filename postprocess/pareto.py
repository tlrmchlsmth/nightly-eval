#!/usr/bin/env python3
"""Merge per-config benchmark results into a TPOT vs tok/sec/GPU Pareto chart.

Usage:
    python3 pareto.py /mnt/lustre/nightly/results/2026-04-27

Reads:
    - metadata.json (config list with num_gpus)
    - <config>/staircase/summary.json (nyann-bench JSON summaries, one per line per worker)

Outputs:
    - pareto.csv   (one row per config x stage)
    - pareto.png   (scatter + Pareto frontier)
"""

import csv
import json
import sys
from pathlib import Path


def load_stages(summary_path: Path) -> list[dict]:
    """Parse nyann-bench summary JSON and extract per-stage stats.

    Each line in the file is either a log line or a JSON summary from one worker.
    We look for JSON objects with a "stages" array.
    """
    if not summary_path.exists():
        return []

    stages_by_idx: dict[int, list[dict]] = {}

    with open(summary_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            for s in data.get("stages", []):
                idx = s.get("stage", 0)
                stages_by_idx.setdefault(idx, []).append(s)

    # Merge per-worker stage stats. nyann-bench divides configured concurrency
    # across workers, so sum it back to the global concurrency for labels and
    # peak-stage selection.
    merged = []
    for idx in sorted(stages_by_idx):
        workers = stages_by_idx[idx]
        concurrency = sum(w.get("concurrency", 0) for w in workers)
        duration = max(w.get("duration_seconds", 0) for w in workers)
        total_tok_s = sum(w.get("output_tokens_per_second", 0) for w in workers)
        requests = sum(w.get("requests", 0) for w in workers)

        # Average latency percentiles across workers (weighted by request count would
        # be better, but simple average is fine for the Pareto chart)
        def avg_latency(key: str, sub: str) -> float:
            vals = [w[key][sub] for w in workers if key in w and sub in w[key]]
            return sum(vals) / len(vals) if vals else 0

        merged.append({
            "stage": idx,
            "concurrency": concurrency,
            "duration_seconds": duration,
            "output_tokens_per_second": total_tok_s,
            "requests": requests,
            "stream_itl_mean": avg_latency("itl_ms", "mean"),
            "stream_itl_p50": avg_latency("itl_ms", "p50"),
            "stream_itl_p90": avg_latency("itl_ms", "p90"),
            "stream_itl_p99": avg_latency("itl_ms", "p99"),
            "ttft_p50": avg_latency("ttft_ms", "p50"),
            "ttft_p90": avg_latency("ttft_ms", "p90"),
        })

    return merged


def compute_pareto_points(run_dir: Path) -> list[dict]:
    """Compute (TPOT/user, tok/sec/gpu) for each config x stage."""
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

        stages = load_stages(summary_path)
        if not stages:
            print(f"WARN: no stages found in {summary_path}", file=sys.stderr)
            continue

        for stage in stages:
            tok_per_sec = stage["output_tokens_per_second"]
            tok_per_sec_per_gpu = tok_per_sec / num_gpus if num_gpus > 0 else 0

            tpot_ms = stage["concurrency"] * 1000.0 / tok_per_sec if tok_per_sec > 0 else 0
            interactivity = 1000.0 / tpot_ms if tpot_ms > 0 else 0

            points.append({
                "config": config_name,
                "num_gpus": num_gpus,
                "concurrency": stage["concurrency"],
                "interactivity": interactivity,
                "tpot_ms": tpot_ms,
                "stream_itl_mean_ms": stage["stream_itl_mean"],
                "stream_itl_p50_ms": stage["stream_itl_p50"],
                "stream_itl_p90_ms": stage["stream_itl_p90"],
                "stream_itl_p99_ms": stage["stream_itl_p99"],
                "ttft_p50_ms": stage["ttft_p50"],
                "ttft_p90_ms": stage["ttft_p90"],
                "tok_per_sec": tok_per_sec,
                "tok_per_sec_per_gpu": tok_per_sec_per_gpu,
                "num_requests": stage["requests"],
                "duration_s": stage["duration_seconds"],
            })

    return points


def pareto_frontier(points: list[dict]) -> list[dict]:
    """Extract the Pareto-optimal points (minimize TPOT/user, maximize tok/sec/GPU)."""
    sorted_pts = sorted(points, key=lambda p: p["tpot_ms"])
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
        x = [p["tpot_ms"] for p in cfg_points]
        y = [p["tok_per_sec_per_gpu"] for p in cfg_points]
        ax.scatter(x, y, color=config_colors[config], label=config, s=80, zorder=3)

        sorted_cfg = sorted(cfg_points, key=lambda p: p["concurrency"])
        x_line = [p["tpot_ms"] for p in sorted_cfg]
        y_line = [p["tok_per_sec_per_gpu"] for p in sorted_cfg]
        ax.plot(x_line, y_line, color=config_colors[config], alpha=0.3, linewidth=1)

        for p in cfg_points:
            ax.annotate(
                str(p["concurrency"]),
                (p["tpot_ms"], p["tok_per_sec_per_gpu"]),
                textcoords="offset points",
                xytext=(5, 5),
                fontsize=7,
                alpha=0.7,
            )

    if frontier:
        fx = [p["tpot_ms"] for p in frontier]
        fy = [p["tok_per_sec_per_gpu"] for p in frontier]
        ax.plot(fx, fy, "k--", linewidth=2, alpha=0.5, label="Pareto frontier")

    ax.set_xlabel("TPOT/user (ms)", fontsize=12)
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

    frontier_path = run_dir / "pareto_frontier.csv"
    write_csv(frontier, frontier_path)
    print(f"Wrote {len(frontier)} frontier points to {frontier_path}")


if __name__ == "__main__":
    main()
