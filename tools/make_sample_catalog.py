from __future__ import annotations

import os
from openpyxl import Workbook

BASE = "/workspace"
TESTS_DIR = os.path.join(BASE, "tests")
SCRIPTS_DIR = os.path.join(TESTS_DIR, "scripts")


def write_catalog_excel(path: str) -> None:
    wb = Workbook()
    ws = wb.active
    headers = [
        "TestCaseId",
        "TestCaseName",
        "Enabled",
        "SqlText",
        "SqlFile",
        "PassIfZeroRows",
        "RowLimitForReport",
        "Description",
        "Suite",
        "Owner",
        "Database",
        "TimeoutSeconds",
    ]
    ws.append(headers)

    ws.append([
        "TC_001",
        "No rows (inline SqlText)",
        1,
        "SELECT TOP 0 * FROM sys.objects;",
        None,
        1,
        10,
        "Should pass because it returns zero rows",
        "Sanity",
        "QA",
        None,
        30,
    ])

    ws.append([
        "TC_002",
        "No rows (from SqlFile)",
        1,
        None,
        "scripts/tc002_sample.sql",
        1,
        10,
        "Should pass because it returns zero rows",
        "Sanity",
        "QA",
        None,
        30,
    ])

    ws.append([
        "TC_003",
        "Failure example (returns 1 row)",
        1,
        "SELECT 1 AS Violation;",
        None,
        1,
        10,
        "Should fail because it returns a row",
        "Sanity",
        "QA",
        None,
        30,
    ])

    wb.save(path)


def main() -> None:
    os.makedirs(SCRIPTS_DIR, exist_ok=True)

    # Create a sample SQL file that returns zero rows
    sql_path = os.path.join(SCRIPTS_DIR, "tc002_sample.sql")
    with open(sql_path, "w", encoding="utf-8") as f:
        f.write("""
-- Sample SQL returning zero rows (assuming sys.objects has names)
SELECT TOP 0 * FROM sys.objects WHERE name LIKE 'no_such_object_%';
""".strip() + "\n")

    excel_path = os.path.join(TESTS_DIR, "catalog.xlsx")
    write_catalog_excel(excel_path)
    print(f"Wrote sample catalog: {excel_path}")
    print(f"Wrote sample SQL: {sql_path}")


if __name__ == "__main__":
    main()