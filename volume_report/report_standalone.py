#!/usr/bin/env python3
import argparse
import os
from datetime import date, datetime
from typing import List, Dict, Optional


def parse_args():
    p = argparse.ArgumentParser(
        description="Generate volume comparison HTML report (no external Python deps).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--start-date", help="Start date (YYYY-MM-DD)")
    p.add_argument("--end-date", help="End date (YYYY-MM-DD)")
    p.add_argument("--threshold", type=int, default=10000, help="Absolute diff threshold")
    p.add_argument("--output", default="/workspace/reports/volume_report_demo.html", help="Output HTML path")
    p.add_argument("--demo", action="store_true", help="Use generated data (default)")
    return p.parse_args()


def to_date(s: Optional[str], default: Optional[date]) -> Optional[date]:
    if s is None:
        return default
    return datetime.strptime(s, "%Y-%m-%d").date()


def demo_rows() -> List[Dict]:
    # Generate deterministic demo data across dates and websites
    rng = [date(2025, 8, d) for d in range(1, 13)]
    websites = [
        "BOOKINGDOTCOM",
        "BOOKINGMOBAPPCUG",
        "GOOGLEHPA",
        "TRAVELOKAMOBAPP",
        "EXPEDIA",
        "EXPEDIAMOCUG",
        "MAKEMYTRIPANDROIDMOBAPP",
        "MAKEMYTRIPANDROIDMOBAPPCUGAG",
        "MAKEMYTRIPCTH",
    ]
    rows: List[Dict] = []
    seed = 42
    for ws in websites:
        base = 100000 + (hash(ws) % 50000)
        last: Optional[int] = None
        for d in rng:
            seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
            delta = (seed % 60000) - 30000  # -30k..+30k
            vol = max(10000, base + delta)
            if last is None:
                prev = None
                diff = None
                status = "Same"
            else:
                prev = last
                diff = vol - last
                status = "Higher" if diff > 0 else ("Lower" if diff < 0 else "Same")
            rows.append(
                {
                    "AssignmentDate": d.isoformat(),
                    "website": ws,
                    "TotalRequest": vol,
                    "prev_day_volume": prev,
                    "volume_diff": diff,
                    "volume_status": status,
                }
            )
            last = vol
    # Drop first day (no prev)
    rows = [r for r in rows if r["prev_day_volume"] is not None]
    return rows


def filter_rows(rows: List[Dict], threshold: int) -> List[Dict]:
    return [r for r in rows if abs(int(r["volume_diff"])) >= threshold]


def group_counts_by_date_and_status(rows: List[Dict]):
    counts = {}
    for r in rows:
        key = (r["AssignmentDate"], r["volume_status"])
        counts[key] = counts.get(key, 0) + 1
    # Return sorted arrays
    dates = sorted({d for d, _ in counts.keys()})
    higher = [counts.get((d, "Higher"), 0) for d in dates]
    lower = [counts.get((d, "Lower"), 0) for d in dates]
    return dates, higher, lower


def top_websites_by_abs_diff(rows: List[Dict], status: str, top_n: int = 10):
    totals = {}
    for r in rows:
        if r["volume_status"] != status:
            continue
        totals[r["website"]] = totals.get(r["website"], 0) + abs(int(r["volume_diff"]))
    items = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    names = [k for k, _ in items]
    values = [v for _, v in items]
    return names, values


def best_worst(rows: List[Dict], top_n: int = 10):
    totals = {}
    for r in rows:
        totals[r["website"]] = totals.get(r["website"], 0) + int(r["volume_diff"])
    items = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    best = items[:top_n]
    worst = items[-top_n:][::-1]
    return ([k for k, _ in best], [v for _, v in best], [k for k, _ in worst], [v for _, v in worst])


def number(x: int) -> str:
    return f"{x:,}"


def build_html(rows: List[Dict], output_path: str, start: Optional[date], end: Optional[date], threshold: int):
    dates, higher_counts, lower_counts = group_counts_by_date_and_status(rows)
    top_h_names, top_h_vals = top_websites_by_abs_diff(rows, "Higher")
    top_l_names, top_l_vals = top_websites_by_abs_diff(rows, "Lower")
    b_names, b_vals, w_names, w_vals = best_worst(rows)

    start_str = start.isoformat() if start else "(min)"
    end_str = end.isoformat() if end else "(max)"
    title = f"Volume Report {start_str} to {end_str} | Threshold: {threshold:,}"

    # Tabular preview HTML
    header = ["AssignmentDate", "website", "TotalRequest", "prev_day_volume", "volume_diff", "volume_status"]
    table_rows = []
    for r in rows:
        table_rows.append(
            "<tr>"
            + f"<td>{r['AssignmentDate']}</td>"
            + f"<td>{r['website']}</td>"
            + f"<td>{number(int(r['TotalRequest']))}</td>"
            + f"<td>{number(int(r['prev_day_volume']))}</td>"
            + f"<td>{number(int(r['volume_diff']))}</td>"
            + f"<td>{r['volume_status']}</td>"
            + "</tr>"
        )

    html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<title>{title}</title>
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
<style>
body{{font-family:Arial,Helvetica,sans-serif;margin:24px;}}
.grid{{display:grid;grid-template-columns:1fr;gap:32px;}}
.sec{{border:1px solid #eee;border-radius:8px;padding:16px;}}
.small{{font-size:12px;color:#555}}
.table-wrapper{{overflow:auto; max-height:420px;}}
 table{{border-collapse:collapse;}}
 th, td{{border:1px solid #ddd;padding:6px 8px;}}
 th{{background:#fafafa;}}
</style>
</head>
<body>
<h2>{title}</h2>
<div class="grid">
  <div class="sec">
    <h3>Higher vs Lower</h3>
    <div id="chart_hilo"></div>
  </div>
  <div class="sec">
    <h3>Top Higher Websites</h3>
    <div id="chart_top_h"></div>
  </div>
  <div class="sec">
    <h3>Top Lower Websites</h3>
    <div id="chart_top_l"></div>
  </div>
  <div class="sec">
    <h3>Best & Worst Performance</h3>
    <div id="chart_best"></div>
    <div id="chart_worst"></div>
  </div>
  <div class="sec">
    <h3>Filtered Rows</h3>
    <div class="table-wrapper">
      <table>
        <thead><tr>{''.join(f'<th>{{c}}</th>' for c in header)}</tr></thead>
        <tbody>
          {''.join(table_rows)}
        </tbody>
      </table>
    </div>
  </div>
</div>
<p class="small">Generated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
<script>
  const dates = {dates};
  const higher = {higher_counts};
  const lower = {lower_counts};
  Plotly.newPlot('chart_hilo', [
    {{x: dates, y: higher, type: 'bar', name: 'Higher', marker: {{color: '#2ca02c'}}}},
    {{x: dates, y: lower, type: 'bar', name: 'Lower', marker: {{color: '#d62728'}}}},
  ], {{barmode:'group', title:'Higher vs Lower counts per day'}});

  Plotly.newPlot('chart_top_h', [
    {{x: {top_h_names}, y: {top_h_vals}, type:'bar', marker: {{color:'#1f77b4'}}}}
  ], {{title: 'Top Higher Websites by total abs diff'}});

  Plotly.newPlot('chart_top_l', [
    {{x: {top_l_names}, y: {top_l_vals}, type:'bar', marker: {{color:'#9467bd'}}}}
  ], {{title: 'Top Lower Websites by total abs diff'}});

  Plotly.newPlot('chart_best', [
    {{x: {b_names}, y: {b_vals}, type:'bar', marker: {{color:'#2ca02c'}}}}
  ], {{title: 'Best (Cumulative Increase)'}});

  Plotly.newPlot('chart_worst', [
    {{x: {w_names}, y: {w_vals}, type:'bar', marker: {{color:'#d62728'}}}}
  ], {{title: 'Worst (Cumulative Decrease)'}});
</script>
</body>
</html>
"""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    return output_path


def main():
    args = parse_args()
    default_end = date(2025, 8, 12)
    default_start = date(2025, 8, 1)
    start = to_date(args.start_date, default_start)
    end = to_date(args.end_date, default_end)

    rows = demo_rows()
    # Note: For simplicity, demo data set already fixed to Aug 01-12; you can still filter by threshold.
    rows = filter_rows(rows, args.threshold)

    output = build_html(rows, args.output, start, end, args.threshold)
    print(output)


if __name__ == "__main__":
    main()