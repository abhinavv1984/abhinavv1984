# Volume Comparison Report

Generates an interactive HTML report from the daily website volume table. It dynamically filters by date range and threshold, and creates:

- Higher vs Lower daily counts
- Top Higher websites
- Top Lower websites
- Best & Worst performance websites (cumulative diff)

## Requirements

Install Python packages:

```bash
pip install -r /workspace/volume_report/requirements.txt
```

## Run (Demo Mode)

Produces a sample report without DB access:

```bash
python /workspace/volume_report/report.py --demo --start-date 2025-08-01 --end-date 2025-08-12 --threshold 10000 --output /workspace/reports/volume_report_demo.html
```

Open the generated HTML in your browser.

## Run (SQL Server)

Supply connection parameters. Dates and threshold are dynamic.

```bash
python /workspace/volume_report/report.py \
  --server YOUR_SQL_HOST \
  --database YOUR_DB \
  --username USERNAME \
  --password PASSWORD \
  --start-date 2025-08-01 \
  --end-date 2025-08-12 \
  --threshold 10000 \
  --table RG_AgodaDaily_WebsiteWiseStats \
  --output /workspace/reports/volume_report.html
```

Notes:
- Uses a CTE equivalent to your provided query and applies `ABS(volume_diff) >= threshold`.
- Set `--driver "ODBC Driver 17 for SQL Server"` if needed on your system, and add `--trust-cert` if your server requires it.
- The output HTML embeds Plotly charts with CDN resources, so viewing requires internet access.