# SQL Test Automation Framework

A lightweight framework to run SQL-based data quality tests from an Excel catalog and produce HTML + Excel reports.

## Features
- Use both `SqlText` and `SqlFile` per test case
- Enable/disable tests via `Enabled` flag (0/1) in Excel
- Default rule: test passes when query returns zero rows (violations)
- Generates HTML summary and Excel results (per-failure sheets with top rows)

## Prerequisites
- Python 3.10+
- For DB connectivity: SQL Server ODBC Driver 18 and `pyodbc` installed (optional for dry run)

## Install
If you cannot install system packages, you can still run `--dry-run`.

Otherwise, create a venv and install deps:
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Configure
Edit `config/db.yml` and set the connection details for your environment (e.g., `dev`). You can reference environment variables using `${VAR_NAME}` notation.

## Excel Test Catalog
Required columns:
- `TestCaseId` (string)
- `TestCaseName` (string)
- `Enabled` (0 or 1)
- one of `SqlText` or `SqlFile`

Optional columns:
- `Description`, `Suite`, `Owner`, `PassIfZeroRows` (default 1), `RowLimitForReport` (default 10), `Database`, `TimeoutSeconds`

## Run
```bash
python src/main.py --excel tests/catalog.xlsx --env dev --report-dir reports/
```

Optional flags:
- `--dry-run`: skip DB execution and synthesize empty results (useful to validate the pipeline)
- `--limit N`: run only the first N enabled tests

## Sample Catalog
Generate a sample Excel catalog and SQL script:
```bash
python tools/make_sample_catalog.py
```
Then run (no DB required):
```bash
python src/main.py --excel tests/catalog.xlsx --env dev --report-dir reports/ --dry-run
```

## Output
Reports are written to a timestamped folder under `reports/`, including:
- `report.html`
- `results.xlsx`
