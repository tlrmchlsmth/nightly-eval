#!/usr/bin/env python3
"""Compare nightly results against rolling baseline and detect regressions.

Usage:
    python3 regression.py /mnt/lustre/nightly/results/2026-04-27

Reads:
    - pareto.csv (from pareto.py)
    - <config>/gsm8k-pre/summary.json, <config>/gsm8k-post/summary.json
    - ../baseline.json (rolling 7-day baseline, if exists)

Outputs:
    - regression-report.json
    - gsm8k_accuracy.json
    - Updates ../baseline.json with current run data
"""

import json
import sys
from pathlib import Path

TPOT_REGRESSION_THRESHOLD = 0.10  # 10% TPOT regression
THROUGHPUT_REGRESSION_THRESHOLD = 0.05  # 5% throughput drop
GSM8K_ABSOLUTE_THRESHOLD = 0.95  # 95% minimum accuracy (DeepSeek-R1)
GSM8K_DELTA_THRESHOLD = 0.02  # 2% pre→post accuracy drop


def load_gsm8k_accuracy(summary_path: Path) -> float | None:
    """Extract accuracy from a GSM8K eval summary."""
    if not summary_path.exists():
        return None

    with open(summary_path) as f:
        content = f.read().strip()

    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("---"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Look for accuracy in various formats
        if "accuracy" in data:
            return float(data["accuracy"])
        if "eval" in data and "accuracy" in data["eval"]:
            return float(data["eval"]["accuracy"])
        if "summary" in data:
            summary = data["summary"]
            if "accuracy" in summary:
                return float(summary["accuracy"])
            if "eval_correct" in summary and "eval_total" in summary:
                total = summary["eval_total"]
                if total > 0:
                    return summary["eval_correct"] / total

    return None


def load_pareto_csv(csv_path: Path) -> list[dict]:
    """Load pareto.csv as list of dicts."""
    import csv

    if not csv_path.exists():
        return []

    with open(csv_path) as f:
        reader = csv.DictReader(f)
        return [
            {k: float(v) if k != "config" else v for k, v in row.items()}
            for row in reader
        ]


def check_gsm8k(run_dir: Path, metadata: dict) -> list[dict]:
    """Check GSM8K accuracy before/after each config."""
    results = []

    for cfg in metadata.get("configs", []):
        config_name = cfg["name"]
        pre_path = run_dir / f"{config_name}-gsm8k-pre" / "summary.json"
        post_path = run_dir / f"{config_name}-gsm8k-post" / "summary.json"

        pre_acc = load_gsm8k_accuracy(pre_path)
        post_acc = load_gsm8k_accuracy(post_path)

        status = "pass"
        messages = []

        if pre_acc is not None and pre_acc < GSM8K_ABSOLUTE_THRESHOLD:
            status = "fail"
            messages.append(
                f"Pre-load accuracy {pre_acc:.3f} below threshold {GSM8K_ABSOLUTE_THRESHOLD}"
            )

        if post_acc is not None and post_acc < GSM8K_ABSOLUTE_THRESHOLD:
            status = "fail"
            messages.append(
                f"Post-load accuracy {post_acc:.3f} below threshold {GSM8K_ABSOLUTE_THRESHOLD}"
            )

        if pre_acc is not None and post_acc is not None:
            delta = pre_acc - post_acc
            if delta > GSM8K_DELTA_THRESHOLD:
                status = "fail"
                messages.append(
                    f"Accuracy degradation {delta:.3f} exceeds threshold {GSM8K_DELTA_THRESHOLD}"
                )

        results.append({
            "config": config_name,
            "pre_accuracy": pre_acc,
            "post_accuracy": post_acc,
            "delta": round(pre_acc - post_acc, 4) if pre_acc and post_acc else None,
            "status": status,
            "messages": messages,
        })

    return results


def check_perf_regression(
    points: list[dict], baseline: dict | None
) -> list[dict]:
    """Compare current perf against rolling baseline."""
    if not baseline or not baseline.get("configs"):
        return []

    results = []
    baseline_by_config = {c["config"]: c for c in baseline["configs"]}

    configs = sorted(set(p["config"] for p in points))
    for config in configs:
        cfg_points = [p for p in points if p["config"] == config]
        if not cfg_points:
            continue

        # Use peak concurrency point for comparison
        peak = max(cfg_points, key=lambda p: p["concurrency"])
        base = baseline_by_config.get(config)

        if not base:
            results.append({
                "config": config,
                "status": "new",
                "messages": ["No baseline for comparison"],
            })
            continue

        messages = []
        status = "pass"

        # TPOT regression
        if base.get("tpot_p50_ms", 0) > 0:
            tpot_ratio = peak["tpot_p50_ms"] / base["tpot_p50_ms"]
            if tpot_ratio > 1 + TPOT_REGRESSION_THRESHOLD:
                status = "warn"
                messages.append(
                    f"TPOT p50 regressed {(tpot_ratio - 1) * 100:.1f}% "
                    f"({base['tpot_p50_ms']:.1f}ms → {peak['tpot_p50_ms']:.1f}ms)"
                )

        # Throughput regression
        if base.get("tok_per_sec_per_gpu", 0) > 0:
            tput_ratio = peak["tok_per_sec_per_gpu"] / base["tok_per_sec_per_gpu"]
            if tput_ratio < 1 - THROUGHPUT_REGRESSION_THRESHOLD:
                status = "warn"
                messages.append(
                    f"tok/sec/GPU dropped {(1 - tput_ratio) * 100:.1f}% "
                    f"({base['tok_per_sec_per_gpu']:.1f} → {peak['tok_per_sec_per_gpu']:.1f})"
                )

        results.append({
            "config": config,
            "status": status,
            "tpot_p50_ms": peak["tpot_p50_ms"],
            "tok_per_sec_per_gpu": peak["tok_per_sec_per_gpu"],
            "concurrency": peak["concurrency"],
            "baseline_tpot_p50_ms": base.get("tpot_p50_ms"),
            "baseline_tok_per_sec_per_gpu": base.get("tok_per_sec_per_gpu"),
            "messages": messages,
        })

    return results


def update_baseline(run_dir: Path, points: list[dict]):
    """Update rolling baseline with current run's peak-concurrency data."""
    baseline_path = run_dir.parent / "baseline.json"

    baseline = {"configs": [], "history": []}
    if baseline_path.exists():
        with open(baseline_path) as f:
            baseline = json.load(f)

    # Add current run to history
    configs = sorted(set(p["config"] for p in points))
    current = {}
    for config in configs:
        cfg_points = [p for p in points if p["config"] == config]
        if cfg_points:
            peak = max(cfg_points, key=lambda p: p["concurrency"])
            current[config] = {
                "config": config,
                "tpot_p50_ms": peak["tpot_p50_ms"],
                "tok_per_sec_per_gpu": peak["tok_per_sec_per_gpu"],
                "concurrency": peak["concurrency"],
                "date": run_dir.name,
            }

    baseline["history"].append({
        "date": run_dir.name,
        "configs": current,
    })

    # Keep last 7 days
    baseline["history"] = baseline["history"][-7:]

    # Compute rolling median as new baseline
    from statistics import median

    all_configs = set()
    for entry in baseline["history"]:
        all_configs.update(entry.get("configs", {}).keys())

    baseline["configs"] = []
    for config in sorted(all_configs):
        tpot_values = []
        tput_values = []
        for entry in baseline["history"]:
            c = entry.get("configs", {}).get(config)
            if c:
                tpot_values.append(c["tpot_p50_ms"])
                tput_values.append(c["tok_per_sec_per_gpu"])

        if tpot_values:
            baseline["configs"].append({
                "config": config,
                "tpot_p50_ms": round(median(tpot_values), 2),
                "tok_per_sec_per_gpu": round(median(tput_values), 2),
                "n_samples": len(tpot_values),
            })

    with open(baseline_path, "w") as f:
        json.dump(baseline, f, indent=2)
    print(f"Updated baseline at {baseline_path} ({len(baseline['history'])} days)")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <run_dir>", file=sys.stderr)
        sys.exit(1)

    run_dir = Path(sys.argv[1])
    meta_path = run_dir / "metadata.json"

    if not meta_path.exists():
        print(f"ERROR: {meta_path} not found", file=sys.stderr)
        sys.exit(1)

    with open(meta_path) as f:
        metadata = json.load(f)

    # GSM8K accuracy check
    gsm8k_results = check_gsm8k(run_dir, metadata)
    gsm8k_path = run_dir / "gsm8k_accuracy.json"
    with open(gsm8k_path, "w") as f:
        json.dump(gsm8k_results, f, indent=2)
    print(f"GSM8K accuracy results: {gsm8k_path}")

    for r in gsm8k_results:
        status = r["status"].upper()
        pre = f"{r['pre_accuracy']:.3f}" if r["pre_accuracy"] else "N/A"
        post = f"{r['post_accuracy']:.3f}" if r["post_accuracy"] else "N/A"
        print(f"  {r['config']}: {status} (pre={pre}, post={post})")

    # Performance regression check
    pareto_csv = run_dir / "pareto.csv"
    points = load_pareto_csv(pareto_csv)

    baseline_path = run_dir.parent / "baseline.json"
    baseline = None
    if baseline_path.exists():
        with open(baseline_path) as f:
            baseline = json.load(f)

    perf_results = check_perf_regression(points, baseline)

    # Combined report
    overall_status = "pass"
    for r in gsm8k_results + perf_results:
        if r["status"] == "fail":
            overall_status = "fail"
        elif r["status"] == "warn" and overall_status == "pass":
            overall_status = "warn"

    report = {
        "status": overall_status,
        "date": run_dir.name,
        "gsm8k": gsm8k_results,
        "performance": perf_results,
    }

    report_path = run_dir / "regression-report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nRegression report: {report_path}")
    print(f"Overall status: {overall_status.upper()}")

    # Update baseline
    if points:
        update_baseline(run_dir, points)

    # Exit with non-zero if any failures
    if overall_status == "fail":
        sys.exit(1)


if __name__ == "__main__":
    main()
