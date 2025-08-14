from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any


@dataclass
class TestCase:
    test_case_id: str
    test_case_name: str
    enabled: int
    sql_text: Optional[str] = None
    sql_file: Optional[str] = None
    description: Optional[str] = None
    suite: Optional[str] = None
    owner: Optional[str] = None
    pass_if_zero_rows: int = 1
    row_limit_for_report: int = 10
    database: Optional[str] = None
    timeout_seconds: Optional[int] = None


@dataclass
class TestResult:
    test_case_id: str
    test_case_name: str
    status: str  # "Passed" | "Failed"
    row_count: int
    duration_seconds: float
    output_summary: str
    sql_preview: str
    error_message: Optional[str] = None
    sample_rows: Optional[List[Dict[str, Any]]] = None
    sample_rows_html: Optional[str] = None
    row_limit_for_report: int = 10
    description: Optional[str] = None
    suite: Optional[str] = None
    owner: Optional[str] = None
    database: Optional[str] = None