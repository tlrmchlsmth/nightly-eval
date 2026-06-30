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

STATUS_COLORS = {"pass": "#26a269", "warn": "#e5a50a", "fail": "#c01c28"}
CHART_COLORS = ["#1c71d8", "#c01c28", "#e5a50a", "#813d9c", "#2ec27e", "#e66100"]


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

def _tpot(p: dict) -> float:
    """TPOT/user in ms, falling back to older latency columns if needed."""
    return p.get("tpot_ms") or p.get("tpot_p50_ms") or p.get("itl_mean_ms") or p.get("itl_p50_ms", 0)


def _interactivity(p: dict) -> float:
    """tok/sec/user derived from TPOT/user."""
    tpot = _tpot(p)
    if tpot > 0:
        return 1000.0 / tpot
    return p.get("interactivity", 0)


def render_pareto_plotly(points: list[dict], frontier: list[dict]) -> str:
    if not points:
        return '<p style="color:#888;">No pareto data available.</p>'

    configs = sorted(set(p["config"] for p in points))
    color_map = {c: CHART_COLORS[i % len(CHART_COLORS)] for i, c in enumerate(configs)}

    traces = []
    for config in configs:
        cfg_pts = sorted(
            [p for p in points if p["config"] == config],
            key=lambda p: p["concurrency"],
        )
        traces.append({
            "x": [_tpot(p) for p in cfg_pts],
            "y": [p["tok_per_sec_per_gpu"] for p in cfg_pts],
            "text": [f"c={int(p['concurrency'])}" for p in cfg_pts],
            "customdata": [
                [_interactivity(p),
                 p.get("stream_itl_mean_ms") or p.get("itl_mean_ms", 0),
                 p.get("ttft_p50_ms", 0),
                 p.get("tok_per_sec", 0),
                 int(p.get("concurrency", 0))]
                for p in cfg_pts
            ],
            "name": config,
            "mode": "lines+markers+text",
            "textposition": "top right",
            "textfont": {"size": 9, "color": "#77767b"},
            "marker": {"size": 9, "color": color_map[config]},
            "line": {"color": color_map[config], "width": 1.5, "dash": "dot"},
            "hovertemplate": (
                "<b>%{fullData.name}</b><br>"
                "TPOT/user: %{x:.2f} ms<br>"
                "Throughput: %{y:.2f} tok/s/GPU<br>"
                "Concurrency: %{customdata[4]}<br>"
                "Interactivity: %{customdata[0]:.2f} tok/s/user<br>"
                "Raw stream ITL mean: %{customdata[1]:.2f} ms<br>"
                "TTFT p50: %{customdata[2]:.2f} ms<br>"
                "Total tok/sec: %{customdata[3]:.0f}"
                "<extra></extra>"
            ),
        })

    if frontier:
        sorted_f = sorted(frontier, key=lambda p: _tpot(p))
        traces.append({
            "x": [_tpot(p) for p in sorted_f],
            "y": [p["tok_per_sec_per_gpu"] for p in sorted_f],
            "name": "Pareto frontier",
            "mode": "lines",
            "line": {"color": "#241f31", "width": 2, "dash": "dash"},
            "hoverinfo": "skip",
            "showlegend": True,
        })

    layout = {
        "title": {"text": "Efficiency Pareto Frontier", "font": {"size": 15, "color": "#241f31"}},
        "xaxis": {"title": "TPOT/user (ms)", "gridcolor": "#deddda"},
        "yaxis": {"title": "Throughput (tok/sec/GPU)", "gridcolor": "#deddda"},
        "plot_bgcolor": "#fafafa",
        "paper_bgcolor": "rgba(0,0,0,0)",
        "font": {"family": "Cantarell, 'Segoe UI', Roboto, sans-serif", "color": "#241f31"},
        "legend": {"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "center", "x": 0.5},
        "margin": {"l": 60, "r": 30, "t": 60, "b": 50},
        "hovermode": "closest",
    }

    div_id = "pareto-chart"
    return (
        f'<div id="{div_id}" style="width:100%;height:480px;"></div>\n'
        f"<script>Plotly.newPlot('{div_id}',{json.dumps(traces)},{json.dumps(layout)},"
        f"{{responsive:true,displayModeBar:false}});</script>"
    )


def render_trend_svg(history: list[dict], metric: str, label: str) -> str:
    """Render a small sparkline-style SVG for a metric over the history window."""
    if not history:
        return f'<p style="color:#888;">No trend data for {escape(label)}.</p>'

    # Extract (date, value) pairs — average across configs for each day
    data = []
    for entry in history:
        vals = []
        for cfg in entry.get("configs", {}).values():
            if not isinstance(cfg, dict):
                continue
            if metric in cfg:
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
        parts.append(f'<line x1="{pad_l}" y1="{y:.2f}" x2="{pad_l + plot_w}" y2="{y:.2f}" stroke="#e5e7eb"/>')
        parts.append(
            f'<text x="{pad_l - 5}" y="{y + 3:.2f}" text-anchor="end" fill="#9ca3af" font-size="9">'
            f"{v:.2f}</text>"
        )

    # Line + points
    line_pts = " ".join(f"{sx(i):.2f},{sy(d[1]):.2f}" for i, d in enumerate(data))
    parts.append(f'<polyline points="{line_pts}" fill="none" stroke="#3b82f6" stroke-width="2"/>')

    for i, (date_str, val) in enumerate(data):
        x = sx(i)
        y = sy(val)
        parts.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3.5" fill="#3b82f6"/>')
        # Date label on x-axis (show month-day only)
        short_date = date_str[-5:] if len(date_str) >= 5 else date_str
        parts.append(
            f'<text x="{x:.2f}" y="{pad_t + plot_h + 14}" text-anchor="middle" '
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
        f'<span style="display:inline-block;padding:4px 16px;border-radius:0;'
        f'background:{color};color:#fff;font-weight:700;font-size:0.85rem;'
        f'text-transform:uppercase;letter-spacing:0.04em;">{escape(status)}</span>'
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
        <div class="card-value">{peak['tok_per_sec_per_gpu']:.2f}</div>
        <div class="card-sub">tok/sec/GPU @ c={int(peak.get('concurrency', 0))}</div>
      </div>""")

    if frontier:
        best_inter = max(frontier, key=lambda p: _interactivity(p))
        inter_val = _interactivity(best_inter)
        cards.append(f"""
      <div class="card">
        <div class="card-label">Best Interactivity</div>
        <div class="card-value">{inter_val:.2f}</div>
        <div class="card-sub">tok/s/user @ c={int(best_inter.get('concurrency', 0))}</div>
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
    svg = render_pareto_plotly(points, frontier)

    # Collapsible data table
    if points:
        cols = ["config", "concurrency", "tpot_ms", "interactivity",
                "stream_itl_mean_ms", "ttft_p50_ms", "tok_per_sec",
                "tok_per_sec_per_gpu"]
        header = "".join(f"<th>{escape(c)}</th>" for c in cols)
        rows = []
        for p in sorted(points, key=lambda p: (p["config"], p.get("concurrency", 0))):
            cells = []
            for c in cols:
                if c == "interactivity":
                    v = _interactivity(p)
                else:
                    v = p.get(c, "")
                if isinstance(v, float):
                    cells.append(f"<td>{v:.2f}</td>")
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

        tpot = f"{r['tpot_ms']:.2f}" if r.get("tpot_ms") is not None else "N/A"
        base_tpot = f"{r['baseline_tpot_ms']:.2f}" if r.get("baseline_tpot_ms") is not None else "N/A"
        if r.get("tpot_ms") is not None and r.get("baseline_tpot_ms"):
            tpot_delta = (r["tpot_ms"] / r["baseline_tpot_ms"] - 1) * 100
            tpot_str = f"{tpot} <span class='delta'>({tpot_delta:+.1f}%)</span>"
        else:
            tpot_str = tpot

        tput = f"{r['tok_per_sec_per_gpu']:.2f}" if r.get("tok_per_sec_per_gpu") is not None else "N/A"
        base_tput = f"{r['baseline_tok_per_sec_per_gpu']:.2f}" if r.get("baseline_tok_per_sec_per_gpu") is not None else "N/A"
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
          <th>TPOT/user (ms)</th><th>Baseline</th>
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
    tpot_svg = render_trend_svg(history, "tpot_ms", "TPOT/user ms (7-day trend)")

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
  font-family: Cantarell, "Segoe UI", Roboto, Ubuntu, sans-serif;
  background: #fafafa; color: #241f31; line-height: 1.5;
}
.container { max-width: 1100px; margin: 0 auto; padding: 0; }
header {
  background: #241f31; color: #fff; padding: 14px 24px; border-radius: 0;
}
.header-top {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; flex-wrap: wrap;
}
h1 { font-size: 1.15rem; font-weight: 700; }
.header-meta {
  margin-top: 8px; display: flex; gap: 20px; flex-wrap: wrap;
  font-size: 0.82rem; color: #c0bfbc;
}
.header-meta code {
  background: rgba(255,255,255,0.12); padding: 1px 6px; border-radius: 0;
  font-size: 0.8rem; font-family: "Source Code Pro", monospace;
}
.content { background: #fafafa; padding: 20px 24px; border-radius: 0; }
.cards { display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }
.card {
  flex: 1; min-width: 140px; background: #fff; border-radius: 0;
  padding: 16px 18px; border: 1px solid #deddda;
  box-shadow: 0 1px 2px rgba(0,0,0,0.04);
}
.card-label {
  font-size: 0.72rem; color: #77767b; text-transform: uppercase;
  letter-spacing: 0.06em; font-weight: 600;
}
.card-value { font-size: 1.5rem; font-weight: 700; margin: 2px 0; }
.card-sub { font-size: 0.78rem; color: #9a9996; }
section {
  background: #fff; border-radius: 0; padding: 20px 24px; margin-bottom: 16px;
  border: 1px solid #deddda;
  box-shadow: 0 1px 2px rgba(0,0,0,0.04);
}
h2 { font-size: 1.05rem; font-weight: 700; margin-bottom: 14px; color: #241f31; }
table { width: 100%; border-collapse: collapse; font-size: 0.84rem; }
th {
  text-align: left; padding: 7px 10px; border-bottom: 1px solid #deddda;
  color: #77767b; font-weight: 600; font-size: 0.75rem; text-transform: uppercase;
}
td { padding: 7px 10px; border-bottom: 1px solid #f6f5f4; }
tbody tr:hover { background: #f6f5f4; }
.status-sm {
  display: inline-block; padding: 2px 10px; border-radius: 0;
  color: #fff; font-weight: 700; font-size: 0.72rem; text-transform: uppercase;
}
.delta { color: #77767b; font-size: 0.8rem; }
.msg { color: #9a9996; font-size: 0.8rem; max-width: 250px; }
.trends { display: flex; gap: 16px; flex-wrap: wrap; }
.trends > div { flex: 1; min-width: 300px; }
details { margin-top: 12px; }
summary {
  cursor: pointer; color: #1c71d8; font-size: 0.84rem; font-weight: 500;
  padding: 6px 0;
}
footer {
  text-align: center; padding: 16px; color: #9a9996; font-size: 0.75rem;
}
@media print {
  body { background: #fff; }
  header { background: #241f31; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  .card, section { box-shadow: none; }
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
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>{CSS}</style>
</head>
<body>
<div class="container">
  {header}
  <div class="content">
    {cards}
    {pareto_sec}
    {regression_sec}
    {gsm8k_sec}
    {trend_sec}
  </div>
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
