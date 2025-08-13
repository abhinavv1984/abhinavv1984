#!/usr/bin/env python3
import argparse
import os
import sys
import textwrap
from datetime import datetime, date
from typing import Optional, Tuple

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# Optional imports for SQL connectivity
try:
    from sqlalchemy import create_engine, text as sql_text
except Exception:
    create_engine = None  # type: ignore
    sql_text = None  # type: ignore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate volume comparison report from SQL Server data and output an interactive HTML.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--start-date", required=False, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", required=False, help="End date (YYYY-MM-DD)")
    parser.add_argument("--threshold", type=int, default=10000, help="Absolute daily diff threshold for inclusion")

    # SQL connection options
    parser.add_argument("--server", help="SQL Server hostname or IP")
    parser.add_argument("--database", help="Database name")
    parser.add_argument("--username", help="Username for SQL Server")
    parser.add_argument("--password", help="Password for SQL Server")
    parser.add_argument("--driver", default="ODBC Driver 17 for SQL Server", help="ODBC driver for pyodbc connection string")
    parser.add_argument("--trust-cert", action="store_true", help="Trust server certificate (add TrustServerCertificate=yes)")

    parser.add_argument("--output", default="/workspace/reports/volume_report.html", help="Output HTML path")
    parser.add_argument("--table", default="RG_AgodaDaily_WebsiteWiseStats", help="Source table name")
    parser.add_argument("--demo", action="store_true", help="Run with generated demo data instead of DB")

    return parser.parse_args()


def parse_date(d: Optional[str], default: Optional[date]) -> Optional[date]:
    if d is None:
        return default
    return datetime.strptime(d, "%Y-%m-%d").date()


def validate_dates(start: Optional[date], end: Optional[date]) -> Tuple[Optional[date], Optional[date]]:
    if start and end and start > end:
        raise ValueError("start-date must be <= end-date")
    return start, end


def build_query(table: str, start: Optional[date], end: Optional[date], threshold: int) -> str:
    # Convert to SQL literals
    where_parts = ["AssignmentDate IS NOT NULL"]
    if start is not None:
        where_parts.append(f"AssignmentDate >= '{start.strftime('%Y-%m-%d')}'")
    if end is not None:
        where_parts.append(f"AssignmentDate <= '{end.strftime('%Y-%m-%d')}'")
    where_clause = " AND ".join(where_parts)

    # Compose query using a CTE as provided
    query = f"""
    WITH volume_comparison AS (
        SELECT
            AssignmentDate,
            website,
            TotalRequest,
            LAG(TotalRequest) OVER (PARTITION BY website ORDER BY AssignmentDate) AS prev_day_volume,
            TotalRequest - LAG(TotalRequest) OVER (PARTITION BY website ORDER BY AssignmentDate) AS volume_diff,
            CASE
                WHEN TotalRequest - LAG(TotalRequest) OVER (PARTITION BY website ORDER BY AssignmentDate) > 0 THEN 'Higher'
                WHEN TotalRequest - LAG(TotalRequest) OVER (PARTITION BY website ORDER BY AssignmentDate) < 0 THEN 'Lower'
                ELSE 'Same'
            END AS volume_status
        FROM {table} WITH (NOLOCK)
        WHERE {where_clause}
    )
    SELECT AssignmentDate, website, TotalRequest, prev_day_volume, volume_diff, volume_status
    FROM volume_comparison
    WHERE prev_day_volume IS NOT NULL AND ABS(volume_diff) >= {threshold}
    ORDER BY AssignmentDate, website;
    """
    return query


def build_engine(server: str, database: str, username: Optional[str], password: Optional[str], driver: str, trust_cert: bool):
    if create_engine is None:
        raise RuntimeError("SQLAlchemy is not available. Please install requirements.")

    # Prefer pyodbc connection string through SQLAlchemy
    params = {
        "DRIVER": driver,
        "SERVER": server,
        "DATABASE": database,
    }
    if username and password:
        params.update({"UID": username, "PWD": password})
    else:
        # Trusted connection (Linux + Kerberos not typical). Keeping UID/PWD optional.
        pass
    if trust_cert:
        params["TrustServerCertificate"] = "yes"

    # Encode into URL form
    # mssql+pyodbc:///?odbc_connect=...
    import urllib.parse

    odbc_parts = ";".join([f"{k}={v}" for k, v in params.items()])
    odbc_connect = urllib.parse.quote_plus(odbc_parts)
    url = f"mssql+pyodbc:///?odbc_connect={odbc_connect}"
    engine = create_engine(url, fast_executemany=True)
    return engine


def fetch_data(engine, query: str) -> pd.DataFrame:
    with engine.connect() as conn:
        df = pd.read_sql_query(sql_text(query), conn)  # type: ignore
    # Enforce dtypes
    if not pd.api.types.is_datetime64_any_dtype(df["AssignmentDate"]):
        df["AssignmentDate"] = pd.to_datetime(df["AssignmentDate"]).dt.date
    return df


def demo_data() -> pd.DataFrame:
    # Create simulated sample across dates and websites
    rng = pd.date_range("2025-08-01", "2025-08-12", freq="D").date
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
    rows = []
    seed = 42
    for ws in websites:
        base = 100000 + (hash(ws) % 50000)
        last = None
        for d in rng:
            # deterministic pseudo random
            seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
            delta = (seed % 60000) - 30000  # -30k .. +30k
            vol = max(10000, base + delta)
            if last is None:
                prev = None
                diff = None
            else:
                prev = last
                diff = vol - last
            rows.append({
                "AssignmentDate": d,
                "website": ws,
                "TotalRequest": vol,
                "prev_day_volume": prev,
                "volume_diff": diff,
                "volume_status": (
                    "Higher" if diff is not None and diff > 0 else ("Lower" if diff is not None and diff < 0 else "Same")
                ),
            })
            last = vol
    df = pd.DataFrame(rows)
    df = df[df["prev_day_volume"].notna()].copy()
    df["volume_diff_abs"] = df["volume_diff"].abs()
    df = df[df["volume_diff_abs"] >= 10000]
    return df.drop(columns=["volume_diff_abs"])  # will recompute based on threshold later


def filter_threshold(df: pd.DataFrame, threshold: int) -> pd.DataFrame:
    mask = df["volume_diff"].abs() >= threshold
    return df.loc[mask].copy()


def format_number(n: float) -> str:
    return f"{n:,.0f}"


def chart_higher_vs_lower(df: pd.DataFrame):
    counts = df.groupby(["AssignmentDate", "volume_status"], as_index=False)["website"].count()
    counts.rename(columns={"website": "count"}, inplace=True)
    # Keep only Higher/Lower for the stacked chart
    counts = counts[counts["volume_status"].isin(["Higher", "Lower"])]
    fig = px.bar(
        counts,
        x="AssignmentDate",
        y="count",
        color="volume_status",
        barmode="group",
        color_discrete_map={"Higher": "#2ca02c", "Lower": "#d62728"},
        title="Higher vs Lower counts per day",
    )
    fig.update_layout(legend_title_text="Status", bargap=0.2)
    return fig


def chart_top_websites(df: pd.DataFrame, status: str, top_n: int = 10):
    sub = df[df["volume_status"] == status].copy()
    if sub.empty:
        return go.Figure()
    # Sum absolute diffs by website across the period
    agg = (
        sub.assign(abs_diff=sub["volume_diff"].abs())
        .groupby("website", as_index=False)["abs_diff"].sum()
        .sort_values("abs_diff", ascending=False)
        .head(top_n)
    )
    fig = px.bar(
        agg,
        x="website",
        y="abs_diff",
        title=f"Top {top_n} websites by total absolute diff ({status})",
        labels={"abs_diff": "Total Abs Diff"},
        color_discrete_sequence=["#1f77b4"],
    )
    fig.update_yaxes(tickformat=",")
    fig.update_xaxes(tickangle=30)
    return fig


def chart_best_worst_websites(df: pd.DataFrame, top_n: int = 10):
    # Best: most positive cumulative diff; Worst: most negative cumulative diff
    agg = df.groupby("website", as_index=False)["volume_diff"].sum()
    best = agg.sort_values("volume_diff", ascending=False).head(top_n)
    worst = agg.sort_values("volume_diff").head(top_n)

    fig = make_subplots(rows=1, cols=2, subplot_titles=("Best (Cumulative Increase)", "Worst (Cumulative Decrease)"))

    fig.add_trace(
        go.Bar(x=best["website"], y=best["volume_diff"], marker_color="#2ca02c", name="Best"),
        row=1, col=1
    )
    fig.add_trace(
        go.Bar(x=worst["website"], y=worst["volume_diff"], marker_color="#d62728", name="Worst"),
        row=1, col=2
    )
    fig.update_layout(title_text="Best and Worst Performance Websites (Cumulative Diff)")
    fig.update_yaxes(tickformat=",")
    fig.update_xaxes(tickangle=30)
    return fig


def build_report(df: pd.DataFrame, output_path: str, start: Optional[date], end: Optional[date], threshold: int, sql_info: Optional[str] = None):
    if df.empty:
        html = f"""
        <html><head><meta charset='utf-8'><title>Volume Report</title></head>
        <body>
            <h2>Volume Report</h2>
            <p>No data after applying filters.</p>
        </body></html>
        """
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)
        return output_path

    df = df.copy()
    # Ensure date order
    df["AssignmentDate"] = pd.to_datetime(df["AssignmentDate"]).dt.date

    # Charts
    higher_lower_fig = chart_higher_vs_lower(df)
    top_higher_fig = chart_top_websites(df, status="Higher")
    top_lower_fig = chart_top_websites(df, status="Lower")
    best_worst_fig = chart_best_worst_websites(df)

    # Build HTML
    start_str = start.isoformat() if start else "(min)"
    end_str = end.isoformat() if end else "(max)"
    title = f"Volume Report {start_str} to {end_str} | Threshold: {threshold:,}"

    html_parts = [
        "<html>",
        "<head>",
        "<meta charset='utf-8'>",
        f"<title>{title}</title>",
        "<style>body{font-family:Arial,Helvetica,sans-serif;margin:24px;} h2{margin-top:0} .meta{color:#666} .grid{display:grid;grid-template-columns:1fr;gap:32px;} .sec{border:1px solid #eee;border-radius:8px;padding:16px;} .small{font-size:12px;color:#555}</style>",
        "</head>",
        "<body>",
        f"<h2>{title}</h2>",
    ]
    if sql_info:
        html_parts.append(f"<p class='meta'>{sql_info}</p>")

    # Embed figures
    def fig_div(fig):
        return fig.to_html(include_plotlyjs='cdn', full_html=False)

    html_parts.extend([
        "<div class='grid'>",
        "<div class='sec'>",
        "<h3>Higher vs Lower</h3>",
        fig_div(higher_lower_fig),
        "</div>",
        "<div class='sec'>",
        "<h3>Top Higher Websites</h3>",
        fig_div(top_higher_fig),
        "</div>",
        "<div class='sec'>",
        "<h3>Top Lower Websites</h3>",
        fig_div(top_lower_fig),
        "</div>",
        "<div class='sec'>",
        "<h3>Best & Worst Performance</h3>",
        fig_div(best_worst_fig),
        "</div>",
        "</div>",
    ])

    # Tabular preview
    preview = df.copy()
    preview["TotalRequest"] = preview["TotalRequest"].map(format_number)
    preview["prev_day_volume"] = preview["prev_day_volume"].map(lambda v: "" if pd.isna(v) else format_number(float(v)))
    preview["volume_diff"] = preview["volume_diff"].map(lambda v: format_number(v) if pd.notna(v) else "")
    html_parts.append("<div class='sec'>")
    html_parts.append("<h3>Filtered Rows</h3>")
    html_parts.append(preview.to_html(index=False))
    html_parts.append("</div>")

    html_parts.append("<p class='small'>Generated at " + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "</p>")
    html_parts.append("</body></html>")

    html = "\n".join(html_parts)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    return output_path


def main():
    args = parse_args()
    # Dates
    default_end = date.today()
    default_start = default_end.replace(day=1)

    start = parse_date(args.start_date, default_start)
    end = parse_date(args.end_date, default_end)
    start, end = validate_dates(start, end)

    threshold = int(args.threshold)

    if args.demo:
        df = demo_data()
        df = filter_threshold(df, threshold)
        output = build_report(df, args.output, start, end, threshold, sql_info="Demo mode")
        print(output)
        return 0

    # Ensure required SQL params
    missing = [name for name, val in {
        "server": args.server,
        "database": args.database,
    }.items() if not val]
    if missing:
        print("Missing required arguments: " + ", ".join(missing), file=sys.stderr)
        return 2

    engine = build_engine(
        server=args.server,
        database=args.database,
        username=args.username,
        password=args.password,
        driver=args.driver,
        trust_cert=args.trust_cert,
    )

    query = build_query(args.table, start, end, threshold)
    df = fetch_data(engine, query)
    df = filter_threshold(df, threshold)

    sql_info = f"Server={args.server}; Database={args.database}; Table={args.table}"
    output = build_report(df, args.output, start, end, threshold, sql_info=sql_info)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())