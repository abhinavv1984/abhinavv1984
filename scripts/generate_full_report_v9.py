import os
import csv
import html as html_lib
from datetime import datetime, timedelta
from PIL import Image
import json
import pyodbc
import shutil


def _status_rank(status_value):
    s = (status_value or '').lower()
    # Higher is better (to keep as the representative row)
    # Fail > Error > Pass >= Stats > Ignore > unknown
    return {
        'fail': 4,
        'error': 3,
        'pass': 2,
        'stats': 2,
        'ignore': 1,
    }.get(s, 0)


def _dedupe_results_at_test_level(rows):
    """
    Keep a single row per (UserId, CustomerName, ReportId, TestID) using priority:
    - Higher status rank wins (Fail > Error > Pass >= Stats > Ignore)
    - Newer LogDate wins
    - Larger MaxReportdetailID wins
    """
    best_by_key = {}

    for r in rows:
        key = (
            r.get('UserId'),
            r.get('CustomerName'),
            r.get('ReportId'),
            r.get('TestID'),
        )

        # Skip malformed keys
        if any(v is None for v in key):
            continue

        current = best_by_key.get(key)
        if current is None:
            best_by_key[key] = r
            continue

        a_score = (
            _status_rank(r.get('Status')),
            r.get('LogDate') or datetime.min,
            r.get('MaxReportdetailID') or 0,
        )
        b_score = (
            _status_rank(current.get('Status')),
            current.get('LogDate') or datetime.min,
            current.get('MaxReportdetailID') or 0,
        )

        if a_score > b_score:
            best_by_key[key] = r

    return list(best_by_key.values())


def fetch_report_data_from_db(config, report_ids):
    conn = pyodbc.connect(
        f"DRIVER={config['database']['driver']};"
        f"SERVER={config['database']['server']};"
        f"DATABASE={config['database']['database']};"
        f"UID={config['database']['username']};"
        f"PWD={config['database']['password']};"
        "TrustServerCertificate=yes;"
    )
    cursor = conn.cursor()

    if report_ids:
        report_ids_str = ','.join(str(rid) for rid in report_ids)
        query = f"""
            SELECT ReportID, AccountName, CustomerId, UserId, TestCaseID, TestCaseInfo, TCPriority, TCStatus,
                   TotalCount, TCFailureCount, WebsiteID, Requestsegmentid, MaxReportdetailID, LogDate, errordetails,
                   ExecutionTimeSeconds, ExecutionTimeFormatted
            FROM [rg_oprationalbackup].[dbo].[RG_QC_TestRunAutomate_QA]
            WHERE ReportID IN ({report_ids_str})
              AND LogDate >= CAST(GETDATE() AS DATE)
              AND LogDate < DATEADD(DAY, 1, CAST(GETDATE() AS DATE))
        """
    else:
        query = """
            SELECT ReportID, AccountName, CustomerId, UserId, TestCaseID, TestCaseInfo, TCPriority, TCStatus,
                   TotalCount, TCFailureCount, WebsiteID, Requestsegmentid, MaxReportdetailID, LogDate, errordetails,
                   ExecutionTimeSeconds, ExecutionTimeFormatted
            FROM [rg_oprationalbackup].[dbo].[RG_QC_TestRunAutomate_QA]
            WHERE LogDate >= CAST(GETDATE() AS DATE)
              AND LogDate < DATEADD(DAY, 1, CAST(GETDATE() AS DATE))
        """

    cursor.execute(query)

    results = []
    for row in cursor.fetchall():
        details = []
        columns = []
        if row.errordetails:
            try:
                errordetails = json.loads(row.errordetails)
                columns = errordetails.get("columns", [])
                details = errordetails.get("rows", [])
            except Exception:
                columns = []
                details = []

        results.append({
            "CustomerName": row.AccountName,
            "UserId": row.UserId,
            "ReportId": row.ReportID,
            "TestID": row.TestCaseID,
            "TestName": row.TestCaseInfo,
            "Priority": row.TCPriority,
            "Status": row.TCStatus,
            "FailureCount": row.TCFailureCount,
            "Details": details,
            "Columns": columns,
            "Error": "" if (row.TCStatus or '').lower() != "error" else "See details",
            "ExecutionTimeSeconds": row.ExecutionTimeSeconds,
            "ExecutionTimeFormatted": row.ExecutionTimeFormatted,
            "WebsiteID": row.WebsiteID,
            # extra fields for dedupe decisions
            "LogDate": row.LogDate,
            "MaxReportdetailID": row.MaxReportdetailID,
        })

    conn.close()

    # Dedupe at test granularity (UserId, CustomerName, ReportId, TestID)
    results = _dedupe_results_at_test_level(results)

    return results


def generate_reports(results, config, total_time_str):
    """
    Generate QATestOutput.html and QATestOutput.csv in ./reports/
    Version 10: Dedupe by (UserId, CustomerName, ReportId, TestID) + improved assets
    Extended with Stats support
    """
    location = config.get("system", {}).get("location", "Unknown Location")
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    report_dir = os.path.join(base_dir, "reports")
    archive_dir = os.path.join(report_dir, "archive")
    os.makedirs(report_dir, exist_ok=True)
    os.makedirs(archive_dir, exist_ok=True)

    # Dedupe again defensively in case callers pass non-deduped results
    results = _dedupe_results_at_test_level(results or [])

    # Archive files modified yesterday
    yesterday = (datetime.now() - timedelta(days=1)).date()
    for filename in ['QATestOutput.html', 'QATestOutput.csv']:
        file_path = os.path.join(report_dir, filename)
        if os.path.exists(file_path):
            try:
                mod_time = datetime.fromtimestamp(os.path.getmtime(file_path)).date()
                if mod_time == yesterday:
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M')
                    archive_file_path = os.path.join(archive_dir, f'QATestOutput_{yesterday.strftime("%Y%m%d")}_{timestamp}.{filename.rsplit(".", 1)[1]}')
                    shutil.copy(file_path, archive_file_path)
                    print(f'Archived {filename} to: {archive_file_path}')
                else:
                    print(f'Skipped archiving {filename}: not modified yesterday (modified on {mod_time})')
            except Exception as e:
                print(f'Failed to archive {filename}: {e}')
        else:
            print(f'Skipped archiving {filename}: file does not exist')

    logo_path = os.path.join(base_dir, "gallery", "logo.png")
    favicon = os.path.join(base_dir, "gallery", "favicon.ico")

    if os.path.exists(logo_path):
        try:
            print(f"Generating favicon from {logo_path} to {favicon}")
            img = Image.open(logo_path)
            size = min(img.size)
            img = img.crop((0, 0, size, size))
            img = img.resize((32, 32), Image.Resampling.LANCZOS)
            img.save(favicon, format="ICO")
            print(f"Favicon generated successfully at {favicon}")
        except Exception as e:
            print(f"Failed to generate favicon: {e}")

    # Filter out ignore status for display
    display_results = [r for r in results if (r.get('Status') or '').lower() != 'ignore']

    # Total Summary: Treat 'stats' as 'pass'
    total_tests = len(display_results)
    pass_count = sum(1 for r in display_results if (r.get('Status') or '').lower() in ['pass', 'stats'])
    fail_count = sum(1 for r in display_results if (r.get('Status') or '').lower() == 'fail')
    error_count = sum(1 for r in display_results if (r.get('Status') or '').lower() == 'error')
    pass_pct = (pass_count / total_tests * 100) if total_tests else 0.0

    # Build HTML
    html = []
    html.append('<!DOCTYPE html>')
    html.append('<html><head><meta charset="UTF-8">')
    html.append('<meta name="viewport" content="width=device-width, initial-scale=1.0">')
    if os.path.exists(favicon):
        html.append(f'<link rel="icon" href="../gallery/favicon.ico" type="image/x-icon">')
        print(f"Favicon link added: ../gallery/favicon.ico")
    else:
        print("Favicon not found, link not added")
    html.append('<title>QA Automation Report</title>')

    # CSS and JS libraries
    html.append('<link rel="stylesheet" href="https://cdn.datatables.net/1.13.4/css/jquery.dataTables.min.css">')
    html.append('<link rel="stylesheet" href="https://cdn.datatables.net/responsive/2.5.0/css/responsive.dataTables.min.css">')
    html.append('<link rel="stylesheet" href="https://cdn.datatables.net/buttons/2.3.6/css/buttons.dataTables.min.css">')
    html.append('<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css">')
    html.append('<script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>')
    html.append('<script src="https://cdn.datatables.net/1.13.4/js/jquery.dataTables.min.js"></script>')
    html.append('<script src="https://cdn.datatables.net/responsive/2.5.0/js/dataTables.responsive.min.js"></script>')
    html.append('<script src="https://cdn.datatables.net/buttons/2.3.6/js/dataTables.buttons.min.js"></script>')

    # Enhanced CSS with mobile responsiveness
    html.append('<style>')
    html.append("body{font-family:'Inter',sans-serif;background-color:#f8fafc;padding:1rem;}")
    html.append("table.dataTable th, table.dataTable td{white-space:nowrap;}")
    html.append(".show-hide{color:#2563eb;background:#eff6ff;padding:2px 6px;border-radius:4px;font-size:0.75rem;cursor:pointer;}")
    html.append(".show-hide:hover{background:#dbeafe;text-decoration:underline;}")
    html.append(".footer{padding:8px;text-align:center;font-size:0.75rem;color:#666;border-top:1px solid #ddd;margin-top:1rem;}")
    html.append(".cust-head th{background:#f0f9ff;color:#1e40af;font-weight:600;padding:0.5rem;}")
    html.append(".cust-val td{font-weight:500;padding:0.5rem;}")
    html.append(".professional-button{background-color:#007BFF;color:#FFFFFF;padding:0.5rem 1rem;border-radius:5px;font-weight:600;transition:background-color 0.3s;border:none;cursor:pointer;margin-right:0.5rem;}")
    html.append(".professional-button:hover{background-color:#0056b3;}")
    html.append(".filter-controls-line{background-color:#ffffff;padding:1rem;border-radius:8px;margin:1rem 0;border:1px solid #e2e8f0;box-shadow:0 1px 3px rgba(0,0,0,0.1);}")
    html.append(".filter-group{display:flex;flex-direction:column;gap:0.5rem;}")
    html.append("@media (min-width:640px){.filter-group{flex-direction:row;justify-content:space-between;}}");
    html.append(".filter-item{display:flex;align-items:center;gap:0.5rem;}")
    html.append(".filter-item label{font-weight:500;color:#374151;font-size:0.875rem;}")
    html.append(".filter-item select,.filter-item input{padding:0.5rem;border:1px solid #d1d5db;border-radius:6px;background-color:white;font-size:0.875rem;}")
    html.append(".filter-item select:focus,.filter-item input:focus{outline:none;border-color:#3b82f6;box-shadow:0 0 0 1px #3b82f6;}")
    html.append("#searchBox{width:100%;max-width:220px;}")
    html.append(".modal-backdrop{position:fixed;top:0;left:0;width:100%;height:100%;background-color:rgba(0,0,0,0.5);display:none;align-items:center;justify-content:center;z-index:1000;}")
    html.append(".modal{background-color:white;border-radius:8px;box-shadow:0 10px 25px rgba(0,0,0,0.2);width:90%;max-width:600px;min-width:600px;max-height:90vh;overflow:hidden;}")
    html.append("@media (max-width:640px){.modal{min-width:0;width:90%;}}")
    html.append(".modal-header{padding:1rem;border-bottom:1px solid #e5e7eb;display:flex;justify-content:space-between;align-items:center;}")
    html.append(".modal-body{padding:1rem;overflow-y:auto;overflow-x:auto;}")
    html.append(".modal-footer{padding:1rem;border-top:1px solid #e5e7eb;display:flex;justify-content:flex-end;gap:0.5rem;}")
    html.append(".modal-close{background:none;border:none;font-size:1.5rem;cursor:pointer;color:#6b7280;}")
    html.append(".modal-close:hover{color:#374151;}")
    html.append(".download-btn{background-color:#10b981;color:white;padding:0.5rem 1rem;border-radius:4px;border:none;cursor:pointer;font-size:0.875rem;}")
    html.append(".download-btn:hover{background-color:#059669;}")
    html.append(".dataTable tbody tr td{max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}")
    html.append("@media (max-width:640px){.dataTable tbody tr td{font-size:0.75rem;padding:0.5rem;}}");
    html.append(".dataTable thead th{max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}")
    html.append("@media (max-width:640px){.dataTable thead th{font-size:0.75rem;padding:0.5rem;}}");
    html.append(".dataTables_wrapper .dataTables_info{font-size:0.75rem;color:#6b7280;}")
    html.append(".dataTables_wrapper .dataTables_paginate{font-size:0.75rem;}")
    html.append("#qaTable_length{display:none;}")
    html.append("#qaTable_filter{display:none;}")
    html.append('</style></head><body>')

    # Header section
    html.append('<div class="container mx-auto px-2 sm:px-4 md:px-6">')
    html.append('<div class="flex flex-col sm:flex-row justify-between items-center mb-4 sm:mb-6">')
    logo_html = ""
    logo_path = os.path.join(base_dir, "gallery", "logo.png")
    if os.path.exists(logo_path):
        logo_html = f"<img src='../gallery/logo.png' class='h-10 sm:h-12 mr-3' alt='Logo'>"
    html.append(f"<div class='flex items-center mb-2 sm:mb-0'>{logo_html}<h1 class='text-xl sm:text-2xl md:text-3xl font-bold text-gray-800'>QA Automation Report</h1></div>")
    html.append(f"<div class='text-gray-600 text-xs sm:text-sm font-medium'>Total Execution Time: {html_lib.escape(str(total_time_str))}</div>")
    html.append('</div>')

    # Summary section
    html.append('<div class="bg-white p-4 sm:p-6 rounded-lg shadow-md mb-4 sm:mb-6">')
    html.append('<div class="grid grid-cols-1 sm:grid-cols-2 gap-4 sm:gap-6">')
    html.append('<div>')
    html.append('<h2 class="text-base sm:text-lg font-semibold text-gray-800 mb-3">Test Summary</h2>')
    html.append('<div class="grid grid-cols-2 gap-3 sm:gap-4">')
    html.append(f"<div class='bg-green-50 border border-green-200 rounded-lg p-3 text-center'><div class='text-lg sm:text-xl font-bold text-green-700'>{pass_count}</div><div class='text-xs sm:text-sm text-green-600'>Passed ({pass_pct:.1f}%)</div></div>")
    html.append(f"<div class='bg-red-50 border border-red-200 rounded-lg p-3 text-center'><div class='text-lg sm:text-xl font-bold text-red-700'>{fail_count}</div><div class='text-xs sm:text-sm text-red-600'>Failed</div></div>")
    html.append(f"<div class='bg-yellow-50 border border-yellow-200 rounded-lg p-3 text-center'><div class='text-lg sm:text-xl font-bold text-yellow-700'>{error_count}</div><div class='text-xs sm:text-sm text-yellow-600'>Errors</div></div>")
    html.append(f"<div class='bg-gray-50 border border-gray-200 rounded-lg p-3 text-center'><div class='text-lg sm:text-xl font-bold text-gray-700'>{total_tests}</div><div class='text-xs sm:text-sm text-gray-600'>Total</div></div>")
    html.append('</div>')
    html.append(f"<div class='mt-4 w-full bg-gray-200 rounded-full h-3'><div class='bg-green-600 h-3 rounded-full transition-all duration-500' style='width:{pass_pct}%'></div></div>")
    html.append('</div>')

    # Customer summary
    cust_summary = {}
    for r in display_results:
        c = r.get("CustomerName", "Unknown")
        s = cust_summary.setdefault(c, {"total": 0, "pass": 0, "fail": 0, "error": 0})
        s["total"] += 1
        st = (r.get("Status") or "").lower()
        if st in ["pass", "stats"]:
            s["pass"] += 1
        elif st == "fail":
            s["fail"] += 1
        elif st == "error":
            s["error"] += 1

    html.append('<div>')
    html.append('<h2 class="text-base sm:text-lg font-semibold text-gray-800 mb-3">Customer Summary</h2>')
    html.append('<div class="overflow-x-auto">')
    html.append('<table class="min-w-full text-xs bg-white border border-gray-300 rounded-lg">')
    html.append('<thead class="cust-head">')
    html.append('<tr>')
    html.append('<th class="border-b border-gray-300 px-3 py-2 text-left">Customer</th>')
    html.append('<th class="border-b border-gray-300 px-3 py-2 text-center">Total Run</th>')
    html.append('<th class="border-b border-gray-300 px-3 py-2 text-center">Pass</th>')
    html.append('<th class="border-b border-gray-300 px-3 py-2 text-center">Fail</th>')
    html.append('<th class="border-b border-gray-300 px-3 py-2 text-center">Error</th>')
    html.append('<th class="border-b border-gray-300 px-3 py-2 text-center">Pass %</th>')
    html.append('</tr>')
    html.append('</thead>')
    html.append('<tbody>')

    for cust, s in cust_summary.items():
        pct = (s["pass"] / s["total"] * 100) if s["total"] else 0.0
        html.append('<tr class="cust-val hover:bg-gray-50">')
        html.append(f"<td class='border-b border-gray-200 px-3 py-2 font-medium max-w-[150px] truncate' title='{html_lib.escape(cust)}'>{html_lib.escape(cust)}</td>")
        html.append(f"<td class='border-b border-gray-200 px-3 py-2 text-center font-semibold text-gray-700'>{s['total']}</td>")
        html.append(f"<td class='border-b border-gray-200 px-3 py-2 text-center font-semibold text-green-700'>{s['pass']}</td>")
        html.append(f"<td class='border-b border-gray-200 px-3 py-2 text-center font-semibold text-red-700'>{s['fail']}</td>")
        html.append(f"<td class='border-b border-gray-200 px-3 py-2 text-center font-semibold text-yellow-700'>{s['error']}</td>")
        html.append(f"<td class='border-b border-gray-200 px-3 py-2 text-center font-semibold text-blue-700'>{pct:.1f}%</td>")
        html.append('</tr>')

    html.append('</tbody>')
    html.append('</table>')
    html.append('</div>')
    html.append('</div>')
    html.append('</div>')
    html.append('</div>')

    # Filter Controls
    customers = sorted({r.get("CustomerName", "Unknown") for r in display_results})
    html.append('<div class="filter-controls-line">')
    html.append('<div class="filter-group">')
    html.append('<div class="filter-item">')
    html.append('<button id="exportFailedBtn" class="professional-button">Export Failed/Stats Cases</button>')
    html.append('</div>')
    html.append('<div class="filter-item">')
    html.append('<label for="rowsPerPage" class="whitespace-nowrap">Show:</label>')
    html.append('<select id="rowsPerPage" class="w-full sm:w-auto">')
    html.append('<option value="10" selected>10</option>')
    html.append('<option value="20">20</option>')
    html.append('<option value="40">40</option>')
    html.append('<option value="50">50</option>')
    html.append('<option value="70">70</option>')
    html.append('<option value="100">100</option>')
    html.append('</select>')
    html.append('<span class="text-xs sm:text-sm text-gray-600 ml-2 hidden sm:inline">entries</span>')
    html.append('</div>')
    html.append('<div class="filter-item">')
    html.append('<label for="customerFilter" class="whitespace-nowrap">Customer:</label>')
    html.append('<select id="customerFilter" class="w-full sm:w-auto">')
    html.append('<option value="">All</option>')
    for c in customers:
        html.append(f"<option value='{html_lib.escape(c)}'>{html_lib.escape(c)}</option>")
    html.append('</select>')
    html.append('</div>')
    html.append('<div class="filter-item">')
    html.append('<label for="searchBox" class="whitespace-nowrap">Search:</label>')
    html.append('<input type="text" id="searchBox" placeholder="Search in table..." class="w-full sm:w-auto">')
    html.append('</div>')
    html.append('</div>')
    html.append('</div>')

    # Main table section
    html.append('<div class="bg-white rounded-lg shadow-md overflow-x-auto">')
    html.append('<table id="qaTable" class="display w-full">')
    html.append('<thead class="bg-gray-100">')
    html.append('<tr>')
    html.append('<th class="px-2 sm:px-4 py-2 text-left text-xs sm:text-sm font-semibold text-gray-700">User ID</th>')
    html.append('<th class="px-2 sm:px-4 py-2 text-left text-xs sm:text-sm font-semibold text-gray-700">Report ID</th>')
    html.append('<th class="px-2 sm:px-4 py-2 text-left text-xs sm:text-sm font-semibold text-gray-700">Customer Name</th>')
    html.append('<th class="px-2 sm:px-4 py-2 text-left text-xs sm:text-sm font-semibold text-gray-700">Test ID</th>')
    html.append('<th class="px-2 sm:px-4 py-2 text-left text-xs sm:text-sm font-semibold text-gray-700">Test Name</th>')
    html.append('<th class="px-2 sm:px-4 py-2 text-left text-xs sm:text-sm font-semibold text-gray-700">Priority</th>')
    html.append('<th class="px-2 sm:px-4 py-2 text-left text-xs sm:text-sm font-semibold text-gray-700">Status</th>')
    html.append('<th class="px-2 sm:px-4 py-2 text-left text-xs sm:text-sm font-semibold text-gray-700">Failure Count</th>')
    html.append('<th class="px-2 sm:px-4 py-2 text-left text-xs sm:text-sm font-semibold text-gray-700">Execution Time</th>')
    html.append('<th class="px-2 sm:px-4 py-2 text-left text-xs sm:text-sm font-semibold text-gray-700">Details</th>')
    html.append('</tr>')
    html.append('</thead>')
    html.append('<tbody>')

    # Generate table rows
    for idx, r in enumerate(display_results):
        status = (r.get("Status") or "").lower()
        status_class = {
            "pass": "bg-green-100 text-green-800",
            "fail": "bg-red-100 text-red-800",
            "error": "bg-yellow-100 text-yellow-800",
            "stats": "bg-blue-100 text-blue-800"
        }.get(status, "bg-gray-100 text-gray-800")

        user_id = html_lib.escape(str(r.get("UserId", "")))
        report_id = html_lib.escape(str(r.get("ReportId", "")))
        customer = html_lib.escape(str(r.get("CustomerName", "")))
        test_id = html_lib.escape(str(r.get("TestID", "")))
        test_name = html_lib.escape(str(r.get("TestName", "")))
        priority = html_lib.escape(str(r.get("Priority", "")))
        status_disp = html_lib.escape(str(r.get("Status", "")))
        failure_count = html_lib.escape(str(r.get("FailureCount", "")))
        exec_time = html_lib.escape(str(r.get("ExecutionTimeFormatted", "")))

        details_html = 'N/A'
        if status in ("fail", "error", "stats") and r.get("Details") and r.get("Columns"):
            json_obj = {"columns": r.get("Columns", []), "rows": r.get("Details", [])}
            json_str = json.dumps(json_obj, default=str).replace('</', r'\</')
            details_html = f"<a href='#' class='show-hide' onclick='showModal({idx});return false;'>Show/Hide</a>"
            details_html += f"<script type='application/json' class='errordetails' data-idx='{idx}'>{json_str}</script>"

        html.append('<tr class="hover:bg-gray-50">')
        html.append(f"<td class='px-2 sm:px-4 py-2 text-xs max-w-[100px] truncate' title='{user_id}'>{user_id}</td>")
        html.append(f"<td class='px-2 sm:px-4 py-2 text-xs max-w-[100px] truncate' title='{report_id}'>{report_id}</td>")
        html.append(f"<td class='px-2 sm:px-4 py-2 text-xs max-w-[150px] truncate' title='{customer}'>{customer}</td>")
        html.append(f"<td class='px-2 sm:px-4 py-2 text-xs max-w-[100px] truncate' title='{test_id}'>{test_id}</td>")
        html.append(f"<td class='px-2 sm:px-4 py-2 text-xs max-w-[150px] truncate' title='{test_name}'>{test_name}</td>")
        html.append(f"<td class='px-2 sm:px-4 py-2 text-xs max-w-[100px] truncate' title='{priority}'>{priority}</td>")
        html.append(f"<td class='px-2 sm:px-4 py-2 text-xs'><span class='inline-block px-2 py-1 rounded text-xs font-medium {status_class}'>{status_disp}</span></td>")
        html.append(f"<td class='px-2 sm:px-4 py-2 text-xs text-center'>{failure_count}</td>")
        html.append(f"<td class='px-2 sm:px-4 py-2 text-xs max-w-[100px] truncate' title='{exec_time}'>{exec_time}</td>")
        html.append(f"<td class='px-2 sm:px-4 py-2 text-xs'>{details_html}</td>")
        html.append('</tr>')

    html.append('</tbody>')
    html.append('</table>')
    html.append('</div>')

    # Modal
    html.append('<div id="detailsModal" class="modal-backdrop">')
    html.append('<div class="modal">')
    html.append('<button class="modal-close" onclick="closeModal()">&times;</button>')
    html.append('<div class="modal-header">')
    html.append('<h3 class="text-base sm:text-lg font-semibold text-gray-800">Error Details</h3>')
    html.append('</div>')
    html.append('<div class="modal-body" id="modalContent">')
    html.append('<!-- Content populated by JavaScript -->')
    html.append('</div>')
    html.append('<div class="modal-footer">')
    html.append('<button class="download-btn" id="downloadBtn" onclick="downloadModalData()">Download CSV</button>')
    html.append('<button class="bg-gray-500 text-white px-3 py-1.5 rounded hover:bg-gray-600 text-sm" onclick="closeModal()">Close</button>')
    html.append('</div>')
    html.append('</div>')
    html.append('</div>')

    # Footer
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    html.append(f"<div class='footer mt-8 pt-4 border-t border-gray-200'>Generated on: {now} | QA Automation Report | © {datetime.now().year} OTA Group<br>Disclaimer: This report is for internal use only.</div>")

    # JavaScript
    html.append('<script>')
    html.append('var currentModalData = null;')
    html.append('var qaTable;')

    html.append("""
function escapeHtml(s){
    if(s === null || s === undefined) return '';
    return s.toString()
        .replace(/&/g,'&amp;')
        .replace(/</g,'&lt;')
        .replace(/>/g,'&gt;')
        .replace(/"/g,'&quot;')
        .replace(/'/g,'&#39;');
}

function showModal(idx){
    try{
        var script = document.querySelector('script.errordetails[data-idx="' + idx + '"]');
        if(!script){
            alert('No details available.');
            return;
        }

        var parsed = JSON.parse(script.textContent);
        var cols = parsed.columns || [];
        var rows = parsed.rows || [];

        currentModalData = {columns: cols, rows: rows};

        var table = '<div class="overflow-x-auto">';
        table += '<table class="min-w-full border-collapse border border-gray-300 text-xs">';
        table += '<thead><tr class="bg-gray-100">';
        cols.forEach(function(c){ 
            table += '<th class="border border-gray-300 px-2 py-1 text-left font-semibold text-gray-700 max-w-[150px] truncate" title="' + escapeHtml(c) + '">' + escapeHtml(c) + '</th>'; 
        });
        table += '</tr></thead><tbody>';

        rows.forEach(function(r,i){
            var rowClass = i % 2 ? 'bg-gray-50' : 'bg-white';
            table += '<tr class="' + rowClass + '">';
            for(var j=0; j<cols.length; j++){
                var v = (r[j] === null || r[j] === undefined) ? '' : r[j];
                table += '<td class="border border-gray-300 px-2 py-1 max-w-[150px] truncate" title="' + escapeHtml(v) + '">' + escapeHtml(v) + '</td>';
            }
            table += '</tr>';
        });
        table += '</tbody></table></div>';

        document.getElementById('modalContent').innerHTML = table;
        document.getElementById('detailsModal').style.display = 'flex';
    } catch(e){
        console.error(e);
        alert('Failed to render details: ' + e);
    }
}

function closeModal(){
    document.getElementById('detailsModal').style.display = 'none';
    currentModalData = null;
}

function downloadModalData(){
    if(!currentModalData || !currentModalData.columns || !currentModalData.rows){
        alert('No data available for download.');
        return;
    }

    var csvRows = [];
    csvRows.push(currentModalData.columns);
    currentModalData.rows.forEach(function(row){
        csvRows.push(row);
    });

    var csvContent = csvRows.map(function(r){
        return r.map(function(cell){
            var s = (cell === null || cell === undefined) ? '' : cell.toString();
            s = s.replace(/"/g, '""');
            return '"' + s + '"';
        }).join(',');
    }).join('\\n');

    var timestamp = new Date().toISOString().replace(/[:.]/g,'-').replace('T','_').split('Z')[0];
    var filename = 'Details_' + timestamp + '.csv';

    var blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

document.getElementById('detailsModal').addEventListener('click', function(e){
    if(e.target === this){
        closeModal();
    }
});

document.getElementById('exportFailedBtn').addEventListener('click', function () {
    var metaHeaders = ['UserId','ReportId','CustomerName','TestID','TestName','Priority','Status','FailureCount','ExecutionTime'];
    var unionCols = [];
    var collected = [];

    qaTable.rows({search:'applied'}).every(function(rowIdx, tableLoop, rowLoop) {
        var rowNode = this.node();
        var statusCell = $(rowNode).find('td').eq(6);
        var status = statusCell.text().trim().toLowerCase();

        if(status === 'fail' || status === 'stats'){
            var metaVals = [];
            for(var i = 0; i < 9; i++){
                metaVals.push($(rowNode).find('td').eq(i).text().trim());
            }

            var script = $(rowNode).find('script.errordetails');
            if(script.length > 0){
                try {
                    var parsed = JSON.parse(script.text());
                    var cols = parsed.columns || [];
                    var rows = parsed.rows || [];

                    cols.forEach(function(c){ 
                        if(unionCols.indexOf(c) === -1) unionCols.push(c); 
                    });

                    if(rows.length > 0) {
                        rows.forEach(function(r){
                            var m = {};
                            for(var i=0;i<cols.length;i++){
                                m[cols[i]] = (r[i] === null || r[i] === undefined) ? '' : r[i];
                            }
                            collected.push({meta: metaVals, map: m});
                        });
                    } else {
                        collected.push({meta: metaVals, map: {}});
                    }
                } catch (e){
                    console.error('parse errordetails JSON error', e);
                    collected.push({meta: metaVals, map: {}});
                }
            } else {
                collected.push({meta: metaVals, map: {}});
            }
        }
    });

    if(collected.length === 0){
        alert('No failed/stats cases found for export in current view.');
        return;
    }

    var headers = metaHeaders.concat(unionCols);
    var csvRows = [];
    csvRows.push(headers);
    collected.forEach(function(item){
        var row = [].concat(item.meta);
        unionCols.forEach(function(c){
            var v = item.map.hasOwnProperty(c) ? item.map[c] : '';
            row.push(v);
        });
        csvRows.push(row);
    });

    var csvContent = csvRows.map(function(r){
        return r.map(function(cell){
            var s = (cell === null || cell === undefined) ? '' : cell.toString();
            s = s.replace(/"/g, '""');
            return '"' + s + '"';
        }).join(',');
    }).join('\\n');

    var timestamp = new Date().toISOString().replace(/[:.]/g,'-').replace('T','_').split('Z')[0];
    var customerFilter = $('#customerFilter').val().trim();
    var customerPart = customerFilter ? ('_' + customerFilter.replace(/[^a-zA-Z0-9-_]/g, '_')) : '_All';
    var filename = 'Failed_Stats' + customerPart + '_' + timestamp + '.csv';

    var blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
});

$(document).ready(function(){
    console.log('Initializing DataTable...');

    qaTable = $('#qaTable').DataTable({
        responsive: true,
        pageLength: 10,
        lengthChange: false,
        searching: true,
        info: true,
        dom: 'rtip',
        language: {
            info: "Showing _START_ to _END_ of _TOTAL_ entries",
            infoEmpty: "Showing 0 to 0 of 0 entries",
            infoFiltered: "(filtered from _MAX_ total entries)"
        },
        columnDefs: [
            { targets: '_all', className: 'dt-body-left' },
            { targets: 4, className: 'max-w-[150px] truncate' }
        ]
    });

    console.log('DataTable initialized');

    $('#rowsPerPage').on('change', function(){
        var val = parseInt($(this).val());
        console.log('Changing page length to:', val);
        qaTable.page.len(val).draw();
    });

    $('#customerFilter').on('change', function(){
        var val = $(this).val();
        console.log('Customer filter changed to:', val);
        if(val === '') {
            qaTable.column(2).search('').draw();
        } else {
            var escapedVal = $.fn.dataTable.util.escapeRegex(val);
            qaTable.column(2).search('^' + escapedVal + '$', true, false).draw();
        }
    });

    let searchTimeout = null;
    $('#searchBox').on('keyup', function(){
        var val = this.value;
        console.log('Global search:', val);
        clearTimeout(searchTimeout);
        searchTimeout = setTimeout(function() {
            qaTable.search(val).draw();
        }, 300);
    });

    console.log('All event handlers attached');
});
    """)

    html.append('</script>')
    html.append('</div>')
    html.append('</body>')
    html.append('</html>')

    # Write HTML file
    html_path = os.path.join(report_dir, 'QATestOutput.html')
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(html))

    # Write CSV for failed cases and stats (based on deduped results)
    csv_path = os.path.join(report_dir, 'QATestOutput.csv')
    export_rows = [r for r in results if (r.get('Status') or '').lower() in ('fail','stats')]
    union_cols = []
    rows_out = []

    for r in export_rows:
        cols = r.get('Columns') or []
        details = r.get('Details') or []
        for c in cols:
            if c not in union_cols:
                union_cols.append(c)
        if details:
            for d in details:
                rows_out.append((r, dict(zip(cols, d))))
        else:
            rows_out.append((r, {}))

    meta_headers = ['UserId','ReportId','CustomerName','TestID','TestName','Priority','Status','FailureCount','ExecutionTimeFormatted']
    headers = meta_headers + union_cols

    try:
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for r, m in rows_out:
                row = [r.get(h, '') for h in meta_headers]
                for c in union_cols:
                    row.append(m.get(c, ''))
                writer.writerow(row)
        print('CSV written:', csv_path)
    except Exception as e:
        print('Failed to write CSV:', e)

    print('HTML written:', html_path)
    return html_path, csv_path

