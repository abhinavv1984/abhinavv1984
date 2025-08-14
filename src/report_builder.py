from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import List, Dict, Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from models import TestResult
import xlsxwriter


def _format_duration(seconds: float) -> str:
    total_seconds = int(seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def build_html_report(template_dir: str, results: List[TestResult], environment: str, out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(["html", "xml"])  # safe for HTML
    )
    template = env.get_template("report.html.j2")

    total = len(results)
    passed = sum(1 for r in results if r.status == "Passed")
    failed = total - passed
    duration = sum(r.duration_seconds for r in results)

    rendered = template.render(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        environment=environment,
        totals={
            "total": total,
            "passed": passed,
            "failed": failed,
            "duration_hms": _format_duration(duration),
        },
        results=results,
    )
    out_path = os.path.join(out_dir, "report.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(rendered)
    return out_path


def _write_sheet_from_rows(workbook: xlsxwriter.Workbook, sheet_name: str, rows: List[Dict[str, Any]]):
    worksheet = workbook.add_worksheet(sheet_name)
    if not rows:
        return
    headers = list(rows[0].keys())
    for col_idx, col_name in enumerate(headers):
        worksheet.write(0, col_idx, col_name)
    for row_idx, row in enumerate(rows, start=1):
        for col_idx, col_name in enumerate(headers):
            worksheet.write(row_idx, col_idx, row.get(col_name))


def build_excel_results(results: List[TestResult], out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)

    # Summary sheet rows
    summary_rows: List[Dict[str, Any]] = []
    for r in results:
        summary_rows.append({
            "TestCaseId": r.test_case_id,
            "TestCase": r.test_case_name,
            "Result": r.status,
            "RowCount": r.row_count,
            "DurationSeconds": round(r.duration_seconds, 3),
            "Output": r.output_summary,
            "Suite": r.suite,
            "Owner": r.owner,
            "Database": r.database,
            "Description": r.description,
        })

    out_path = os.path.join(out_dir, "results.xlsx")
    workbook = xlsxwriter.Workbook(out_path)
    try:
        _write_sheet_from_rows(workbook, "Summary", summary_rows)

        for r in results:
            if r.status == "Failed" and r.sample_rows is not None and len(r.sample_rows) > 0:
                safe_sheet = (r.test_case_id or "Case").replace("/", "_")[:31]
                _write_sheet_from_rows(workbook, safe_sheet, r.sample_rows)
    finally:
        workbook.close()

    return out_path