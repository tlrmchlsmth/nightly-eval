#!/usr/bin/env python3
"""Generate a self-contained HTML dashboard from nightly eval results.

Usage:
    python3 dashboard.py /mnt/lustre/nightly/results/2026-06-15

Reads:
    - metadata.json
    - regression-report.json
    - gsm8k_accuracy.json
    - pareto.csv / pareto_frontier.csv
    - ../baseline.json (for 7-day trend)

Outputs:
    - dashboard.html
"""

import csv
import json
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path

STATUS_COLORS = {"pass": "#22c55e", "warn": "#f59e0b", "fail": "#ef4444"}
CHART_COLORS = ["#3b82f6", "#ef4444", "#f59e0b", "#8b5cf6", "#06b6d4", "#ec4899"]


# ---------------------------------------------------------------------------
# Data loaders — each returns a sensible default when the file is missing
# ---------------------------------------------------------------------------

def load_json(path: Path, default=None):
    if not path.exists():
        return default
    with open(path) as f:
        return json.load(f)


def load_csv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path) as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            parsed = {}
            for k, v in row.items():
                if k == "config":
                    parsed[k] = v
                else:
                    try:
                        parsed[k] = float(v)
                    except (ValueError, TypeError):
                        parsed[k] = v
            rows.append(parsed)
        return rows


# ---------------------------------------------------------------------------
# SVG chart renderers
# ---------------------------------------------------------------------------

def render_pareto_svg(points: list[dict], frontier: list[dict]) -> str:
    if not points:
        return '<p style="color:#888;">No pareto data available.</p>'

    w, h = 720, 420
    pad_l, pad_r, pad_t, pad_b = 70, 30, 40, 60

    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b

    xs = [p["tpot_p50_ms"] for p in points]
    ys = [p["tok_per_sec_per_gpu"] for p in points]

    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)

    x_pad = (x_max - x_min) * 0.08 or 1
    y_pad = (y_max - y_min) * 0.08 or 1
    x_min -= x_pad
    x_max += x_pad
    y_min -= y_pad
    y_max += y_pad

    def sx(v):
        return pad_l + (v - x_min) / (x_max - x_min) * plot_w

    def sy(v):
        return pad_t + plot_h - (v - y_min) / (y_max - y_min) * plot_h

    configs = sorted(set(p["config"] for p in points))
    color_map = {c: CHART_COLORS[i % len(CHART_COLORS)] for i, c in enumerate(configs)}

    parts = [
        f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;max-width:{w}px;height:auto;font-family:system-ui,sans-serif;">',
        f'<rect width="{w}" height="{h}" fill="#fafafa" rx="8"/>',
    ]

    # Grid lines and axis ticks
    n_xticks = 5
    n_yticks = 5
    parts.append('<g stroke="#e5e7eb" stroke-width="1">')
    for i in range(n_xticks + 1):
        v = x_min + (x_max - x_min) * i / n_xticks
        x = sx(v)
        parts.append(f'<line x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" y2="{pad_t + plot_h}"/>')
        parts.append(
            f'<text x="{x:.1f}" y="{pad_t + plot_h + 20}" text-anchor="middle" '
            f'fill="#6b7280" font-size="11">{v:.0f}</text>'
        )
    for i in range(n_yticks + 1):
        v = y_min + (y_max - y_min) * i / n_yticks
        y = sy(v)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w}" y2="{y:.1f}"/>')
        parts.append(
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" '
            f'fill="#6b7280" font-size="11">{v:.1f}</text>'
        )
    parts.append("</g>")

    # Axis labels
    parts.append(
        f'<text x="{pad_l + plot_w / 2}" y="{h - 8}" text-anchor="middle" '
        f'fill="#374151" font-size="13">TPOT p50 (ms)</text>'
    )
    parts.append(
        f'<text x="16" y="{pad_t + plot_h / 2}" text-anchor="middle" '
        f'fill="#374151" font-size="13" transform="rotate(-90 16 {pad_t + plot_h / 2})">'
        f"tok/sec/GPU</text>"
    )

    # Title
    parts.append(
        f'<text x="{w / 2}" y="24" text-anchor="middle" fill="#111827" '
        f'font-size="15" font-weight="600">Efficiency Pareto Frontier</text>'
    )

    # Pareto frontier line
    if frontier:
        sorted_f = sorted(frontier, key=lambda p: p["tpot_p50_ms"])
        fpts = " ".join(f"{sx(p['tpot_p50_ms']):.1f},{sy(p['tok_per_sec_per_gpu']):.1f}" for p in sorted_f)
        parts.append(
            f'<polyline points="{fpts}" fill="none" stroke="#111827" '
            f'stroke-width="2" stroke-dasharray="6 4" opacity="0.5"/>'
        )

    # Data points with concurrency labels
    for config in configs:
        color = color_map[config]
        cfg_pts = sorted(
            [p for p in points if p["config"] == config],
            key=lambda p: p["concurrency"],
        )

        # Connecting line
        if len(cfg_pts) > 1:
            line_pts = " ".join(
                f"{sx(p['tpot_p50_ms']):.1f},{sy(p['tok_per_sec_per_gpu']):.1f}"
                for p in cfg_pts
            )
            parts.append(
                f'<polyline points="{line_pts}" fill="none" stroke="{color}" '
                f'stroke-width="1.5" opacity="0.3"/>'
            )

        for p in cfg_pts:
            cx = sx(p["tpot_p50_ms"])
            cy = sy(p["tok_per_sec_per_gpu"])
            parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" fill="{color}"/>')
            parts.append(
                f'<text x="{cx + 7:.1f}" y="{cy - 5:.1f}" fill="#6b7280" '
                f'font-size="9">{int(p["concurrency"])}</text>'
            )

    # Legend
    lx = pad_l + 10
    ly = pad_t + 12
    for i, config in enumerate(configs):
        parts.append(f'<rect x="{lx}" y="{ly + i * 18 - 8}" width="10" height="10" rx="2" fill="{color_map[config]}"/>')
        parts.append(
            f'<text x="{lx + 14}" y="{ly + i * 18}" fill="#374151" font-size="11">'
            f"{escape(config)}</text>"
        )

    parts.append("</svg>")
    return "\n".join(parts)


def render_trend_svg(history: list[dict], metric: str, label: str) -> str:
    """Render a small sparkline-style SVG for a metric over the history window."""
    if not history:
        return f'<p style="color:#888;">No trend data for {escape(label)}.</p>'

    # Extract (date, value) pairs — average across configs for each day
    data = []
    for entry in history:
        vals = []
        for cfg in entry.get("configs", {}).values():
            if isinstance(cfg, dict) and metric in cfg:
                vals.append(cfg[metric])
        if vals:
            data.append((entry.get("date", "?"), sum(vals) / len(vals)))

    if len(data) < 2:
        return f'<p style="color:#888;">Insufficient history for {escape(label)} trend.</p>'

    w, h = 360, 120
    pad_l, pad_r, pad_t, pad_b = 50, 20, 24, 30

    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b

    vals = [d[1] for d in data]
    v_min = min(vals)
    v_max = max(vals)
    v_pad = (v_max - v_min) * 0.15 or 1
    v_min -= v_pad
    v_max += v_pad

    def sx(i):
        return pad_l + i / (len(data) - 1) * plot_w

    def sy(v):
        return pad_t + plot_h - (v - v_min) / (v_max - v_min) * plot_h

    parts = [
        f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;max-width:{w}px;height:auto;font-family:system-ui,sans-serif;">',
        f'<rect width="{w}" height="{h}" fill="#fafafa" rx="6"/>',
        f'<text x="{w / 2}" y="16" text-anchor="middle" fill="#374151" '
        f'font-size="12" font-weight="600">{escape(label)}</text>',
    ]

    # Y-axis ticks
    for i in range(4):
        v = v_min + (v_max - v_min) * i / 3
        y = sy(v)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w}" y2="{y:.1f}" stroke="#e5e7eb"/>')
        parts.append(
            f'<text x="{pad_l - 5}" y="{y + 3:.1f}" text-anchor="end" fill="#9ca3af" font-size="9">'
            f"{v:.1f}</text>"
        )

    # Line + points
    line_pts = " ".join(f"{sx(i):.1f},{sy(d[1]):.1f}" for i, d in enumerate(data))
    parts.append(f'<polyline points="{line_pts}" fill="none" stroke="#3b82f6" stroke-width="2"/>')

    for i, (date_str, val) in enumerate(data):
        x = sx(i)
        y = sy(val)
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="#3b82f6"/>')
        # Date label on x-axis (show month-day only)
        short_date = date_str[-5:] if len(date_str) >= 5 else date_str
        parts.append(
            f'<text x="{x:.1f}" y="{pad_t + plot_h + 14}" text-anchor="middle" '
            f'fill="#9ca3af" font-size="9">{escape(short_date)}</text>'
        )

    parts.append("</svg>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# HTML section renderers
# ---------------------------------------------------------------------------

def status_badge(status: str) -> str:
    color = STATUS_COLORS.get(status, "#6b7280")
    return (
        f'<span style="display:inline-block;padding:6px 20px;border-radius:6px;'
        f'background:{color};color:#fff;font-weight:700;font-size:1.1rem;'
        f'text-transform:uppercase;letter-spacing:0.05em;">{escape(status)}</span>'
    )


def render_header(metadata: dict, report: dict) -> str:
    date = report.get("date", metadata.get("start", "")[:10] if metadata else "unknown")
    image = escape(metadata.get("vllm_image", "unknown")) if metadata else "unknown"
    status = report.get("status", "unknown") if report else "unknown"

    start = metadata.get("start", "") if metadata else ""
    end = metadata.get("end", "") if metadata else ""
    duration = ""
    if start and end:
        try:
            t0 = datetime.fromisoformat(start)
            t1 = datetime.fromisoformat(end)
            mins = int((t1 - t0).total_seconds() / 60)
            duration = f"{mins}m"
        except (ValueError, TypeError):
            pass

    return f"""
    <header>
      <div class="header-top">
        <h1>Nightly Eval Dashboard</h1>
        {status_badge(status)}
      </div>
      <div class="header-meta">
        <span><strong>Date:</strong> {escape(date)}</span>
        <span><strong>Image:</strong> <code>{image}</code></span>
        {"<span><strong>Duration:</strong> " + escape(duration) + "</span>" if duration else ""}
      </div>
    </header>"""


def render_summary_cards(metadata: dict, report: dict, gsm8k: list, frontier: list) -> str:
    cards = []

    # Overall status
    status = report.get("status", "N/A") if report else "N/A"
    color = STATUS_COLORS.get(status, "#6b7280")
    cards.append(f"""
      <div class="card" style="border-top:4px solid {color};">
        <div class="card-label">Status</div>
        <div class="card-value" style="color:{color};">{escape(status).upper()}</div>
      </div>""")

    # GSM8K accuracy
    if gsm8k:
        best = gsm8k[0]
        pre = f"{best['pre_accuracy']:.1%}" if best.get("pre_accuracy") is not None else "N/A"
        post = f"{best['post_accuracy']:.1%}" if best.get("post_accuracy") is not None else "N/A"
        cards.append(f"""
      <div class="card">
        <div class="card-label">GSM8K Accuracy</div>
        <div class="card-value">{pre}</div>
        <div class="card-sub">pre {pre} / post {post}</div>
      </div>""")

    # Peak throughput from frontier
    if frontier:
        peak = max(frontier, key=lambda p: p.get("tok_per_sec_per_gpu", 0))
        cards.append(f"""
      <div class="card">
        <div class="card-label">Peak Throughput</div>
        <div class="card-value">{peak['tok_per_sec_per_gpu']:.1f}</div>
        <div class="card-sub">tok/sec/GPU @ c={int(peak.get('concurrency', 0))}</div>
      </div>""")

    # TPOT p50 at peak throughput
    if frontier:
        best_tpot = min(frontier, key=lambda p: p.get("tpot_p50_ms", float("inf")))
        cards.append(f"""
      <div class="card">
        <div class="card-label">Best TPOT p50</div>
        <div class="card-value">{best_tpot['tpot_p50_ms']:.1f} ms</div>
        <div class="card-sub">@ c={int(best_tpot.get('concurrency', 0))}</div>
      </div>""")

    failed = metadata.get("failed", 0) if metadata else 0
    configs_count = len(metadata.get("configs", [])) if metadata else 0
    cards.append(f"""
      <div class="card">
        <div class="card-label">Configs</div>
        <div class="card-value">{configs_count}</div>
        <div class="card-sub">{failed} failed</div>
      </div>""")

    return '<div class="cards">' + "".join(cards) + "</div>"


def render_pareto_section(points: list[dict], frontier: list[dict]) -> str:
    svg = render_pareto_svg(points, frontier)

    # Collapsible data table
    if points:
        cols = ["config", "concurrency", "tpot_p50_ms", "tpot_p95_ms",
                "ttft_p50_ms", "tok_per_sec", "tok_per_sec_per_gpu"]
        header = "".join(f"<th>{escape(c)}</th>" for c in cols)
        rows = []
        for p in sorted(points, key=lambda p: (p["config"], p.get("concurrency", 0))):
            cells = []
            for c in cols:
                v = p.get(c, "")
                if isinstance(v, float):
                    cells.append(f"<td>{v:.1f}</td>")
                else:
                    cells.append(f"<td>{escape(str(v))}</td>")
            rows.append("<tr>" + "".join(cells) + "</tr>")
        table = f"""
        <details>
          <summary>Raw data ({len(points)} points)</summary>
          <table><thead><tr>{header}</tr></thead><tbody>{"".join(rows)}</tbody></table>
        </details>"""
    else:
        table = ""

    return f"""
    <section>
      <h2>Pareto Frontier</h2>
      {svg}
      {table}
    </section>"""


def render_regression_section(report: dict) -> str:
    perf = report.get("performance", []) if report else []
    if not perf:
        return """
    <section>
      <h2>Performance Regression</h2>
      <p style="color:#888;">No regression data available.</p>
    </section>"""

    rows = []
    for r in perf:
        status = r.get("status", "?")
        color = STATUS_COLORS.get(status, "#6b7280")
        badge = f'<span class="status-sm" style="background:{color};">{escape(status).upper()}</span>'

        tpot = f"{r['tpot_p50_ms']:.1f}" if r.get("tpot_p50_ms") is not None else "N/A"
        base_tpot = f"{r['baseline_tpot_p50_ms']:.1f}" if r.get("baseline_tpot_p50_ms") is not None else "N/A"
        if r.get("tpot_p50_ms") is not None and r.get("baseline_tpot_p50_ms"):
            tpot_delta = (r["tpot_p50_ms"] / r["baseline_tpot_p50_ms"] - 1) * 100
            tpot_str = f"{tpot} <span class='delta'>({tpot_delta:+.1f}%)</span>"
        else:
            tpot_str = tpot

        tput = f"{r['tok_per_sec_per_gpu']:.1f}" if r.get("tok_per_sec_per_gpu") is not None else "N/A"
        base_tput = f"{r['baseline_tok_per_sec_per_gpu']:.1f}" if r.get("baseline_tok_per_sec_per_gpu") is not None else "N/A"
        if r.get("tok_per_sec_per_gpu") is not None and r.get("baseline_tok_per_sec_per_gpu"):
            tput_delta = (r["tok_per_sec_per_gpu"] / r["baseline_tok_per_sec_per_gpu"] - 1) * 100
            tput_str = f"{tput} <span class='delta'>({tput_delta:+.1f}%)</span>"
        else:
            tput_str = tput

        msgs = "; ".join(r.get("messages", [])) or "-"

        rows.append(f"""<tr>
          <td>{escape(r.get('config', '?'))}</td>
          <td>{badge}</td>
          <td>{tpot_str}</td><td>{base_tpot}</td>
          <td>{tput_str}</td><td>{base_tput}</td>
          <td class="msg">{escape(msgs)}</td>
        </tr>""")

    return f"""
    <section>
      <h2>Performance Regression</h2>
      <table>
        <thead><tr>
          <th>Config</th><th>Status</th>
          <th>TPOT p50 (ms)</th><th>Baseline</th>
          <th>tok/sec/GPU</th><th>Baseline</th>
          <th>Messages</th>
        </tr></thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
    </section>"""


def render_gsm8k_section(gsm8k: list) -> str:
    if not gsm8k:
        return """
    <section>
      <h2>GSM8K Accuracy</h2>
      <p style="color:#888;">No accuracy data available.</p>
    </section>"""

    rows = []
    for r in gsm8k:
        status = r.get("status", "?")
        color = STATUS_COLORS.get(status, "#6b7280")
        badge = f'<span class="status-sm" style="background:{color};">{escape(status).upper()}</span>'
        pre = f"{r['pre_accuracy']:.3f}" if r.get("pre_accuracy") is not None else "N/A"
        post = f"{r['post_accuracy']:.3f}" if r.get("post_accuracy") is not None else "N/A"
        delta = f"{r['delta']:.4f}" if r.get("delta") is not None else "N/A"
        msgs = "; ".join(r.get("messages", [])) or "-"

        rows.append(f"""<tr>
          <td>{escape(r.get('config', '?'))}</td>
          <td>{badge}</td>
          <td>{pre}</td><td>{post}</td><td>{delta}</td>
          <td class="msg">{escape(msgs)}</td>
        </tr>""")

    return f"""
    <section>
      <h2>GSM8K Accuracy</h2>
      <table>
        <thead><tr>
          <th>Config</th><th>Status</th>
          <th>Pre</th><th>Post</th><th>Delta</th>
          <th>Messages</th>
        </tr></thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
    </section>"""


def render_trend_section(history: list[dict]) -> str:
    tput_svg = render_trend_svg(history, "tok_per_sec_per_gpu", "tok/sec/GPU (7-day trend)")
    tpot_svg = render_trend_svg(history, "tpot_p50_ms", "TPOT p50 ms (7-day trend)")

    return f"""
    <section>
      <h2>7-Day Trend</h2>
      <div class="trends">
        <div>{tput_svg}</div>
        <div>{tpot_svg}</div>
      </div>
    </section>"""


# ---------------------------------------------------------------------------
# Full page assembly
# ---------------------------------------------------------------------------

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #f9fafb; color: #111827; line-height: 1.5;
}
.container { max-width: 1100px; margin: 0 auto; padding: 24px 16px; }
header {
  background: #111827; color: #f9fafb; padding: 24px 28px; border-radius: 12px;
  margin-bottom: 24px;
}
.header-top { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
h1 { font-size: 1.5rem; font-weight: 700; }
.header-meta { margin-top: 12px; display: flex; gap: 24px; flex-wrap: wrap; font-size: 0.9rem; color: #d1d5db; }
.header-meta code { background: #1f2937; padding: 2px 6px; border-radius: 4px; font-size: 0.85rem; }
.cards { display: flex; gap: 16px; margin-bottom: 24px; flex-wrap: wrap; }
.card {
  flex: 1; min-width: 150px; background: #fff; border-radius: 10px;
  padding: 18px 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); border-top: 4px solid #e5e7eb;
}
.card-label { font-size: 0.8rem; color: #6b7280; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
.card-value { font-size: 1.6rem; font-weight: 700; margin: 4px 0; }
.card-sub { font-size: 0.8rem; color: #9ca3af; }
section {
  background: #fff; border-radius: 10px; padding: 24px; margin-bottom: 20px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}
h2 { font-size: 1.15rem; font-weight: 700; margin-bottom: 16px; color: #111827; }
table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
th { text-align: left; padding: 8px 10px; border-bottom: 2px solid #e5e7eb; color: #6b7280; font-weight: 600; font-size: 0.8rem; text-transform: uppercase; }
td { padding: 8px 10px; border-bottom: 1px solid #f3f4f6; }
tbody tr:nth-child(even) { background: #f9fafb; }
.status-sm {
  display: inline-block; padding: 2px 10px; border-radius: 4px;
  color: #fff; font-weight: 700; font-size: 0.75rem; text-transform: uppercase;
}
.delta { color: #6b7280; font-size: 0.82rem; }
.msg { color: #9ca3af; font-size: 0.82rem; max-width: 250px; }
.trends { display: flex; gap: 20px; flex-wrap: wrap; }
.trends > div { flex: 1; min-width: 300px; }
details { margin-top: 12px; }
summary { cursor: pointer; color: #3b82f6; font-size: 0.88rem; font-weight: 500; }
footer { text-align: center; padding: 20px; color: #9ca3af; font-size: 0.8rem; }
@media print {
  body { background: #fff; }
  header { background: #111; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  .card, section { box-shadow: none; border: 1px solid #e5e7eb; }
}
"""


def render_dashboard(run_dir: Path) -> str:
    metadata = load_json(run_dir / "metadata.json", {})
    report = load_json(run_dir / "regression-report.json", {})
    gsm8k = load_json(run_dir / "gsm8k_accuracy.json", [])
    pareto_points = load_csv_rows(run_dir / "pareto.csv")
    pareto_frontier = load_csv_rows(run_dir / "pareto_frontier.csv")
    baseline = load_json(run_dir.parent / "baseline.json", {})
    history = baseline.get("history", []) if baseline else []

    header = render_header(metadata, report)
    cards = render_summary_cards(metadata, report, gsm8k, pareto_frontier)
    pareto_sec = render_pareto_section(pareto_points, pareto_frontier)
    regression_sec = render_regression_section(report)
    gsm8k_sec = render_gsm8k_section(gsm8k)
    trend_sec = render_trend_section(history)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nightly Eval &mdash; {escape(report.get('date', ''))}</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">
  {header}
  {cards}
  {pareto_sec}
  {regression_sec}
  {gsm8k_sec}
  {trend_sec}
</div>
<footer>Generated {now} from {escape(str(run_dir))}</footer>
</body>
</html>"""


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <run_dir>", file=sys.stderr)
        sys.exit(1)

    run_dir = Path(sys.argv[1])
    if not run_dir.is_dir():
        print(f"ERROR: {run_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    html = render_dashboard(run_dir)
    out_path = run_dir / "dashboard.html"
    out_path.write_text(html)
    print(f"Dashboard written to {out_path}")


if __name__ == "__main__":
    main()
