from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from typing import List, Optional

from rich import print
from rich.table import Table

from models import TestCase, TestResult
from excel_loader import load_test_cases
from sql_runner import load_db_config, execute_query
from report_builder import build_html_report, build_excel_results


def read_sql_from_file(base_dir: str, path: str) -> str:
    if os.path.isabs(path):
        full_path = path
    else:
        full_path = os.path.join(base_dir, path)
    if not os.path.exists(full_path):
        raise FileNotFoundError(f"SqlFile not found: {full_path}")
    with open(full_path, "r", encoding="utf-8") as f:
        return f.read()


def preview_sql(sql: str, max_lines: int = 100) -> str:
    lines = sql.splitlines()
    if len(lines) <= max_lines:
        return sql
    return "\n".join(lines[:max_lines] + ["... (truncated)"])


def result_output_summary(row_count: int, error: Optional[str]) -> str:
    if error:
        return "Error"
    return "No violations" if row_count == 0 else f"{row_count} violation(s)"


def html_table_from_rows(rows: List[dict]) -> str:
    if not rows:
        return ""
    headers = list(rows[0].keys())
    thead = "<thead><tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr></thead>"
    trs = []
    for r in rows:
        tds = "".join(f"<td>{'' if r.get(h) is None else r.get(h)}</td>" for h in headers)
        trs.append(f"<tr>{tds}</tr>")
    tbody = "<tbody>" + "".join(trs) + "</tbody>"
    return f"<table>{thead}{tbody}</table>"


def run_test_cases(test_cases: List[TestCase], db_config_path: str, env: str, tests_dir: str, dry_run: bool = False) -> List[TestResult]:
    cfg = load_db_config(db_config_path, env)
    results: List[TestResult] = []

    for tc in test_cases:
        if tc.enabled != 1:
            continue

        sql_text: Optional[str] = tc.sql_text
        if (sql_text is None or len(sql_text.strip()) == 0) and tc.sql_file:
            sql_text = read_sql_from_file(tests_dir, tc.sql_file)

        if sql_text is None or len(sql_text.strip()) == 0:
            results.append(TestResult(
                test_case_id=tc.test_case_id,
                test_case_name=tc.test_case_name,
                status="Failed",
                row_count=0,
                duration_seconds=0.0,
                output_summary="Missing SQL",
                sql_preview="",
                error_message="Neither SqlText nor SqlFile provided",
                row_limit_for_report=tc.row_limit_for_report,
                description=tc.description,
                suite=tc.suite,
                owner=tc.owner,
                database=tc.database,
            ))
            continue

        sql_preview = preview_sql(sql_text)
        error_message: Optional[str] = None
        rows: List[dict]
        duration: float
        if dry_run:
            rows = []
            duration = 0.0
        else:
            try:
                # Override database per test if provided
                cfg_exec = dict(cfg)
                if tc.database:
                    cfg_exec["database"] = tc.database
                rows, duration = execute_query(cfg_exec, sql_text, timeout_seconds=tc.timeout_seconds)
            except Exception as ex:
                rows = []
                duration = 0.0
                error_message = str(ex)

        row_count = len(rows)
        passed = (row_count == 0) if tc.pass_if_zero_rows == 1 else (row_count > 0)
        status = "Passed" if (error_message is None and passed) else "Failed"
        sample_rows = None
        sample_rows_html = None
        if status == "Failed":
            if error_message is None and row_count > 0:
                sample_rows = rows[: tc.row_limit_for_report]
                sample_rows_html = html_table_from_rows(sample_rows)

        results.append(TestResult(
            test_case_id=tc.test_case_id,
            test_case_name=tc.test_case_name,
            status=status,
            row_count=row_count,
            duration_seconds=duration,
            output_summary=result_output_summary(row_count, error_message),
            sql_preview=sql_preview,
            error_message=error_message,
            sample_rows=sample_rows,
            sample_rows_html=sample_rows_html,
            row_limit_for_report=tc.row_limit_for_report,
            description=tc.description,
            suite=tc.suite,
            owner=tc.owner,
            database=tc.database,
        ))

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Run SQL tests from Excel catalog")
    parser.add_argument("--excel", required=True, help="Path to Excel catalog")
    parser.add_argument("--env", default="dev", help="Environment name from config/db.yml")
    parser.add_argument("--db-config", default="config/db.yml", help="Path to DB config YAML")
    parser.add_argument("--report-dir", default="reports", help="Directory to write reports")
    parser.add_argument("--dry-run", action="store_true", help="Skip DB execution")
    parser.add_argument("--limit", type=int, default=None, help="Run only first N enabled tests")

    args = parser.parse_args()

    test_cases, errors = load_test_cases(args.excel)
    if errors:
        for e in errors:
            print(f"[red]- {e}")
        return 2

    enabled_cases = [tc for tc in test_cases if tc.enabled == 1]
    if args.limit is not None:
        enabled_cases = enabled_cases[: args.limit]

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(args.report_dir, timestamp)
    os.makedirs(out_dir, exist_ok=True)

    results = run_test_cases(enabled_cases, args.db_config, args.env, tests_dir=os.path.dirname(args.excel), dry_run=args.dry_run)

    html_path = build_html_report(template_dir="templates", results=results, environment=args.env, out_dir=out_dir)
    xlsx_path = build_excel_results(results=results, out_dir=out_dir)

    # Console summary
    total = len(results)
    passed = sum(1 for r in results if r.status == "Passed")
    failed = total - passed

    table = Table(title="SQL Test Results")
    table.add_column("TestCaseId")
    table.add_column("TestCase")
    table.add_column("Result")
    table.add_column("Rows")
    table.add_column("Duration (s)")

    for r in results:
        table.add_row(r.test_case_id, r.test_case_name, r.status, str(r.row_count), f"{r.duration_seconds:.3f}")

    print(table)
    print(f"HTML: {html_path}")
    print(f"Excel: {xlsx_path}")
    print(f"Totals -> Total: {total}, Passed: {passed}, Failed: {failed}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())