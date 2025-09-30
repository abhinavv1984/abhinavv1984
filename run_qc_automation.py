import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import csv
import time
from datetime import datetime
import yaml
import pyodbc
import pandas as pd
import smtplib
from email.mime.text import MIMEText
import json
import logging

# Configure logging to only capture ERROR level and above
logging.basicConfig(
    filename='error_log.txt',
    level=logging.ERROR,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# ----------------------- CONFIG & DB -----------------------
def load_config(config_file="config.yaml"):
    try:
        with open(config_file) as f:
            return yaml.safe_load(f)
    except Exception as e:
        logging.error(f"Failed to load config: {e}")
        return None

def get_db_connection(config):
    try:
        db_cfg = config['database']
        conn_str = (
            f"DRIVER={db_cfg['driver']};"
            f"SERVER={db_cfg['server']};"
            f"DATABASE={db_cfg['database']};"
            f"UID={db_cfg['username']};"
            f"PWD={db_cfg['password']};"
            "TrustServerCertificate=yes;"
        )
        conn = pyodbc.connect(conn_str, autocommit=False)
        return conn
    except Exception as e:
        logging.error(f"Failed to connect DB: {e}")
        return None

# ----------------------- EMAIL FUNCTION -----------------------
def send_email(config, html_filename, total_time_str, pass_count, fail_count, error_count, ignore_count):
    try:
        email_config = config.get('email', {})
        smtp_server = email_config.get('smtp_server', 'smtp.gmail.com')
        smtp_port = email_config.get('smtp_port', 587)
        sender = email_config.get('sender', 'your_email@example.com')
        password = email_config.get('password', 'your_password')
        recipients = email_config.get('recipients', [])
        base_url = email_config.get('report_base_url', 'http://yourserver/reports/')

        if not recipients:
            return

        subject = f"QA Automation Report - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        report_url = f"{base_url}{html_filename}"
        body = (
            f"The QA automation test run completed.\n\n"
            f"Summary:\n"
            f"- Pass: {pass_count}\n"
            f"- Fail: {fail_count}\n"
            f"- Error: {error_count}\n"
            f"- Ignore: {ignore_count}\n"
            f"- Total Execution Time: {total_time_str}\n\n"
            f"View the report here: {report_url}"
        )

        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = sender
        msg['To'] = ", ".join(recipients)

        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, recipients, msg.as_string())
    except Exception as e:
        logging.error(f"Failed to send email: {e}")

# ----------------------- HELPERS -----------------------
def get_fully_qualified_name(tbl_name):
    if not tbl_name:
        return None
    tbl_name = tbl_name.strip()
    if tbl_name.startswith("[") and tbl_name.endswith("]"):
        tbl_name = tbl_name[1:-1]
    parts = tbl_name.split(".")
    if len(parts) == 1:
        return f"[dbo].[{parts[0]}]"
    elif len(parts) == 2:
        schema, table = parts
        schema = schema if schema else "dbo"
        return f"[{schema}].[{table}]"
    elif len(parts) == 3:
        database, schema, table = parts
        schema = schema if schema else "dbo"
        return f"[{database}].[{schema}].[{table}]"
    else:
        return f"[{tbl_name}]"

def safe_in_clause_params(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return "(NULL)", tuple()
    placeholders = ", ".join(["?"] * len(vals))
    return f"({placeholders})", tuple(vals)

def execute_sql(cursor, sql_text, params=()):
    """
    Execute SQL or EXEC as given. If params are provided, pass them positionally.
    Returns cursor after execute.
    """
    try:
        if params:
            cursor.execute(sql_text, params)
        else:
            cursor.execute(sql_text)
    except Exception:
        # re-raise to caller for context logging
        raise
    return cursor

def parse_stats_resultset(columns, rows):
    if not rows or not columns:
        return columns, rows
    try:
        cols_lower = [c.lower() for c in columns]
        if any(name in cols_lower for name in ['totalrequest', 'available', 'failed', 'totalrecords']):
            return columns, rows
        if len(columns) == 3 and len(rows) >= 1:
            first_row = rows[0]
            numeric_count = sum(1 for val in first_row if isinstance(val, (int, float)))
            if numeric_count >= 2:
                mapped_cols = ['TotalRequest', 'Available', 'Failed']
                mapped_rows = []
                for r in rows:
                    rlist = list(r)
                    mapped_rows.append([int(x) if isinstance(x, (int, float)) else x for x in rlist])
                return mapped_cols, mapped_rows
    except Exception as e:
        logging.error(f"parse_stats_resultset error: {e}")
    return columns, rows

# ----------------------- DB helper functions -----------------------
def fetch_customer_details(conn, userid):
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT TOP 1 DBO.FNEMAILENCODEDECODE(username,'D') as customername, customerid "
            "FROM rg_users WITH (NOLOCK) WHERE userid = ?", userid
        )
        row = cursor.fetchone()
        return (row.customername if row and getattr(row, "customername", None) else "N/A",
                row.customerid if row and getattr(row, "customerid", None) else None)
    except Exception as e:
        logging.error(f"Error fetching customer details for {userid}: {e}")
        return "N/A", None

def get_dynamic_table_name(conn, userid, reportid):
    try:
        cursor = conn.cursor()
        cursor.execute("""
            DECLARE @UserDetailTable VARCHAR(256);
            EXEC USP_GetResponseTable @UserID = ?, @ReportID = ?, @ResponseTable = @UserDetailTable OUTPUT;
            SELECT @UserDetailTable AS TableName;
        """, (userid, reportid))
        row = cursor.fetchone()
        if row and getattr(row, "TableName", None):
            return get_fully_qualified_name(row.TableName)

        sql_fallback = """
        SELECT TableName FROM [RG_Centralizeddb].dbo.RG_Users_ReportDetail WITH (NOLOCK) WHERE IsActive = 1 AND UserId = ?
        UNION
        SELECT CASE WHEN U.ReportType = 'Offline' THEN 'RG_ReportDetail_Ext' ELSE 'RG_ReportDetail' END AS TableName
        FROM RG_Reports R WITH (NOLOCK)
        INNER JOIN RG_Users U WITH (NOLOCK) ON R.CustomerId = U.UserId
        WHERE R.ReportId = ?
          AND NOT EXISTS (
              SELECT 1 FROM [RG_Centralizeddb].dbo.RG_Users_ReportDetail WITH (NOLOCK) WHERE IsActive = 1 AND UserId = ?
          )
        """
        cursor.execute(sql_fallback, (userid, reportid, userid))
        row = cursor.fetchone()
        if row and getattr(row, "TableName", None):
            return get_fully_qualified_name(row.TableName)
        return None
    except Exception as e:
        logging.error(f"Error fetching dynamic table name for User {userid}, Report {reportid}: {e}")
        return None

def fetch_max_reportdetailid_from_qc_table(conn, userid, reportid):
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT MAX(MaxReportdetailID)
            FROM [rg_oprationalbackup].[dbo].[RG_QC_TestRunAutomate_QA]
            WHERE UserId = ? AND ReportID = ?
        """, (userid, reportid))
        row = cursor.fetchone()
        return row[0] if row and row[0] is not None else 0
    except Exception as e:
        logging.error(f"Error fetching MaxReportdetailID from RG_QC_TestRunAutomate_QA for User {userid} Report {reportid}: {e}")
        return 0

def qc_entry_exists(conn, userid, reportid):
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 1 FROM [rg_oprationalbackup].[dbo].[RG_QC_TestRunAutomate_QA]
            WHERE UserId = ? AND ReportID = ?
        """, (userid, reportid))
        return cursor.fetchone() is not None
    except Exception as e:
        logging.error(f"Error checking RG_QC_TestRunAutomate_QA existence for User {userid} Report {reportid}: {e}")
        return True

def fetch_max_reportdetailid_from_dynamic_table(conn, table_name, reportid):
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT MAX(ReportDetailID) FROM {table_name} WHERE ReportID = ?", (reportid,))
        row = cursor.fetchone()
        return row[0] if row and row[0] is not None else 0
    except Exception as e:
        logging.error(f"Error fetching max ReportDetailID from {table_name} for Report {reportid}: {e}")
        return 0

def has_new_data(conn, table_name, reportid, max_reportdetailid_prev):
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT MAX(ReportDetailID) FROM {table_name} WHERE ReportID = ?", (reportid,))
        row = cursor.fetchone()
        max_dynamic_reportdetailid = row[0] if row and row[0] is not None else 0
        return max_dynamic_reportdetailid > max_reportdetailid_prev
    except Exception as e:
        logging.error(f"Error checking new data for Report {reportid}: {e}")
        return False

# ----------------------- SQL content helpers -----------------------
def build_replacements(table_name, reportid, userid, max_reportdetailid_param):
    # Support common placeholder variants used in .sql files
    return {
        "{table}": table_name or "",
        "{table_name}": table_name or "",
        "{reportid}": str(int(reportid) if reportid is not None else 0),
        "{userid}": str(int(userid) if userid is not None else 0),
        "{user}": str(int(userid) if userid is not None else 0),
        "{max_reportdetailid}": str(int(max_reportdetailid_param) if max_reportdetailid_param is not None else 0),
        "{details}": str(int(max_reportdetailid_param) if max_reportdetailid_param is not None else 0),
    }

def apply_replacements(sql_text, replacements):
    out = sql_text
    for key, value in replacements.items():
        out = out.replace(key, value)
    return out

# ----------------------- run_test_case -----------------------
def run_test_case(conn, sql_path, table_name, reportid, sample_size, max_reportdetailid_param, priority, userid):
    """
    Runs a single test case by reading its .sql file and executing the queries exactly as defined.
    - Replaces placeholders found in file: {reportid}, {userid}/{user}, {max_reportdetailid}/{details}, {table}/{table_name}.
    - When 'stats', execute the file content after replacement and parse the resultset for stats.
    - When not 'stats', tries to split by markers: -- COUNT_QUERY and -- DETAILS_QUERY.
      If markers are missing, treats full file as DETAILS query and falls back accordingly.
    """
    try:
        start_time = time.time()
        with open(sql_path, "r", encoding="utf-8") as f:
            raw_sql = f.read().strip()

        replacements = build_replacements(table_name, reportid, userid, max_reportdetailid_param)
        sql_text = apply_replacements(raw_sql, replacements)

        details_marker = "-- DETAILS_QUERY"
        count_marker = "-- COUNT_QUERY"

        # Stats flow executes the replaced SQL as-is
        if priority and str(priority).strip().lower() == "stats":
            try:
                cursor = conn.cursor()
                execute_sql(cursor, sql_text)
                full_rows = cursor.fetchall()
                columns = [col[0] for col in cursor.description] if cursor.description else []
                rows = [tuple(r) for r in full_rows]
                parsed_columns, parsed_rows = parse_stats_resultset(columns, rows)
                end_time = time.time()
                exec_seconds = int(end_time - start_time)
                formatted = f"{exec_seconds} sec"
                return "Stats", len(parsed_rows), parsed_rows, parsed_columns, "", sql_text, exec_seconds, formatted
            except Exception as e:
                error_msg = str(e)
                logging.error(f"Stats test case failed for {sql_path}, Report {reportid}, User {userid}: {error_msg}")
                end_time = time.time()
                exec_seconds = int(end_time - start_time)
                formatted = f"{exec_seconds} sec"
                return "Error", 0, [], [], error_msg, sql_text, exec_seconds, formatted

        # Non-stats: try to locate markers
        details_idx = sql_text.find(details_marker)
        count_sql = None
        details_sql = sql_text
        if details_idx != -1:
            details_sql = sql_text[details_idx + len(details_marker):].strip()
            count_part_raw = sql_text[:details_idx].strip()
            count_start_idx = count_part_raw.find(count_marker)
            if count_start_idx != -1:
                count_sql = count_part_raw[count_start_idx + len(count_marker):].strip()
            else:
                count_sql = count_part_raw if count_part_raw else None

        cursor = conn.cursor()

        # Execute COUNT if available; otherwise attempt fallback for SELECT statements
        count_value = 0
        if count_sql:
            try:
                execute_sql(cursor, count_sql)
                row = cursor.fetchone()
                count_value = int(row[0]) if row and row[0] is not None else 0
            except Exception as e:
                error_msg = str(e)
                logging.error(f"Count query failed for {sql_path}, Report {reportid}, User {userid}: {error_msg}")
                end_time = time.time()
                exec_seconds = int(end_time - start_time)
                formatted = f"{exec_seconds} sec"
                return "Error", 0, [], [], error_msg, details_sql, exec_seconds, formatted
        else:
            # If the details looks like a SELECT, try wrapping with COUNT(*)
            try:
                stripped = details_sql.lstrip().lower()
                if stripped.startswith("select"):
                    count_wrapper = f"SELECT COUNT(*) FROM ({details_sql}) AS _ct"
                    execute_sql(cursor, count_wrapper)
                    row = cursor.fetchone()
                    count_value = int(row[0]) if row and row[0] is not None else 0
                else:
                    # For non-SELECT (e.g., EXEC), we cannot reliably count without procedure support; assume non-zero only after fetching details
                    count_value = None
            except Exception as e:
                logging.error(f"Count fallback failed for {sql_path}: {e}")
                count_value = None

        # Low priority shortcut
        if priority and str(priority).strip().lower() == "low":
            end_time = time.time()
            exec_seconds = int(end_time - start_time)
            formatted = f"{exec_seconds} sec"
            return "Ignore", 0, [], [], "", details_sql, exec_seconds, formatted

        # If count explicitly zero => pass
        if count_value == 0:
            end_time = time.time()
            exec_seconds = int(end_time - start_time)
            formatted = f"{exec_seconds} sec"
            return "Pass", 0, [], [], "", details_sql, exec_seconds, formatted

        # Execute DETAILS and fetch sample
        try:
            execute_sql(cursor, details_sql)
            columns = [c[0] for c in cursor.description] if cursor.description else []
            rows = cursor.fetchmany(sample_size)
            rows = [tuple(r) for r in rows]
            # If we did not have a count and details returned no rows, mark as Pass
            derived_count = count_value if count_value is not None else (len(rows) if rows else 0)
            status = "Fail" if derived_count and derived_count > 0 else "Pass"
            end_time = time.time()
            exec_seconds = int(end_time - start_time)
            formatted = f"{exec_seconds} sec"
            return status, (derived_count or 0), rows, columns, "", details_sql, exec_seconds, formatted
        except Exception as e:
            error_msg = str(e)
            logging.error(f"Details query failed for {sql_path}, Report {reportid}, User {userid}: {error_msg}")
            end_time = time.time()
            exec_seconds = int(end_time - start_time)
            formatted = f"{exec_seconds} sec"
            return "Error", (count_value or 0), [], [], error_msg, details_sql, exec_seconds, formatted
    except Exception as e:
        logging.error(f"Execution error in run_test_case for {sql_path}, Report {reportid}, User {userid}: {e}")
        return "Error", 0, [], [], str(e), "", 0, "0 sec"

# ----------------------- SUMMARY & UTILS -----------------------
def update_customer_summary(conn):
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT AccountName,
                   COUNT(DISTINCT CONCAT(ReportID, '_', TestCaseID)) AS total_count,
                   SUM(CASE WHEN TCStatus = 'Pass' THEN 1 ELSE 0 END) AS pass_count,
                   SUM(CASE WHEN TCStatus = 'Fail' THEN 1 ELSE 0 END) AS fail_count,
                   SUM(CASE WHEN TCStatus = 'Error' THEN 1 ELSE 0 END) AS error_count
            FROM [rg_oprationalbackup].[dbo].[RG_QC_TestRunAutomate_QA]
            WHERE CAST(LogDate AS DATE) = CAST(GETDATE() AS DATE)
              AND TCStatus NOT IN ('Ignore', 'Stats')
            GROUP BY AccountName
        """)
        rows = cursor.fetchall()
        for row in rows:
            account_name, total, pass_count, fail_count, error_count = row
            pass_pct = (pass_count / total * 100) if total else 0
            summary_json = json.dumps({
                "total": total,
                "pass": pass_count,
                "fail": fail_count,
                "error": error_count,
                "pass_pct": round(pass_pct, 1)
            })
            cursor.execute("""
                MERGE [rg_oprationalbackup].[dbo].[RG_QC_SummaryDaily_QA] AS target
                USING (SELECT CAST(GETDATE() AS DATE) AS SummaryDate, ? AS CustomerName) AS source
                ON target.SummaryDate = source.SummaryDate AND target.CustomerName = source.CustomerName
                WHEN MATCHED THEN
                    UPDATE SET SummaryJson = ?, CreatedAt = GETDATE()
                WHEN NOT MATCHED THEN
                    INSERT (SummaryDate, CustomerName, SummaryJson) VALUES (CAST(GETDATE() AS DATE), ?, ?);
            """, (account_name, summary_json, account_name, summary_json))
        conn.commit()
    except Exception as e:
        logging.error(f"Error updating customer summary: {e}")

def deduplicate_results(results):
    deduped = {}
    status_order = {'fail': 4, 'error': 3, 'stats': 2, 'pass': 1, 'ignore': 0}
    for r in results:
        key = (r['CustomerName'], r['ReportId'], r['TestID'])
        current_status = r['Status'].lower()
        if key not in deduped or status_order.get(current_status, 0) > status_order.get(deduped[key]['Status'].lower(), 0):
            deduped[key] = r
    return list(deduped.values())

# ----------------------- MAIN -----------------------
def main():
    start_time = time.time()
    config = load_config("config.yaml")
    if not config:
        return

    sheets = pd.read_excel(config['test']['testfile'], sheet_name=None)
    users_df = sheets.get("Users")
    testcase_df = sheets.get("Test Cases")
    if users_df is None or testcase_df is None:
        logging.error("Missing required sheets (Users, Test Cases) in Excel.")
        return

    exclude_userids = config['test'].get('exclude_userids', [])
    users_df = users_df[(users_df['Enabled'].str.lower() == 'yes') & (~users_df['UserID'].isin(exclude_userids))]
    testcase_df = testcase_df[testcase_df['Enabled'].str.lower() == 'yes']

    conn = get_db_connection(config)
    if not conn:
        return

    cursor = conn.cursor()
    table_exists = False
    try:
        cursor.execute("SELECT TOP 0 * FROM [rg_oprationalbackup].[dbo].[RG_QC_TestRunAutomate_QA]")
        table_exists = True
    except Exception as e:
        error_str = str(e)
        if 'Invalid object name' in error_str:
            logging.error("Table [rg_oprationalbackup].[dbo].[RG_QC_TestRunAutomate_QA] does not exist.")
        else:
            logging.error(f"Error checking table existence (permissions or access issue?): {e}")
        table_exists = False

    sql_folder = os.path.join(os.getcwd(), "queries")
    sample_size = int(config['test'].get('sample_size', 10))

    results = []
    all_report_ids = set()

    for _, user in users_df.iterrows():
        userid = int(user['UserID'])
        report_type = str(user.get('Reportype', "")).strip().lower()
        customername, customer_id = fetch_customer_details(conn, userid)

        user_report_results = {}

        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT reportid FROM RG_Reports WITH (NOLOCK) 
                WHERE customerid = ? 
                AND DeliveryDate >= DATEADD(DAY, -1, CAST(GETDATE() AS DATE)) 
                AND DeliveryDate <= CAST(GETDATE() AS DATE)
            """, (userid,))
            report_ids = [row.reportid for row in cursor.fetchall()]
            all_report_ids.update(report_ids)
        except Exception as e:
            logging.error(f"Error fetching reports for User {userid}: {e}")
            report_ids = []

        for reportid in report_ids:
            table_name = get_dynamic_table_name(conn, userid, reportid)
            if not table_name:
                logging.error(f"Unable to determine table name for User {userid} Report {reportid}, skipping all tests")
                results.append({
                    "CustomerName": customername,
                    "UserId": userid,
                    "ReportId": reportid,
                    "TestID": "N/A",
                    "TestName": "N/A",
                    "Priority": "N/A",
                    "Status": "Error",
                    "FailureCount": 0,
                    "Details": [],
                    "Columns": [],
                    "Error": "Dynamic table not found",
                    "ExecutionTimeSeconds": 0,
                    "ExecutionTimeFormatted": "0 sec",
                    "WebsiteID": ""
                })
                continue

            max_reportdetailid_new = fetch_max_reportdetailid_from_dynamic_table(conn, table_name, reportid)
            max_reportdetailid_prev = fetch_max_reportdetailid_from_qc_table(conn, userid, reportid)

            entry_exists = False
            if table_exists:
                entry_exists = qc_entry_exists(conn, userid, reportid)

            should_run = not entry_exists or has_new_data(conn, table_name, reportid, max_reportdetailid_prev)
            if not should_run:
                continue

            user_report_results[reportid] = []

            for _, test_row in testcase_df.iterrows():
                runtype_list = [v.strip().lower() for v in str(test_row.get("Runtype", "")).split(",")]
                if report_type not in runtype_list:
                    continue

                testid = test_row['TestID']
                testname = test_row['TestName']
                priority = test_row['Priority']

                sql_path = os.path.join(sql_folder, f"{testid}.sql")
                if not os.path.isfile(sql_path):
                    logging.error(f"SQL file {sql_path} not found for TestID {testid}, skipping.")
                    results.append({
                        "CustomerName": customername,
                        "UserId": userid,
                        "ReportId": reportid,
                        "TestID": testid,
                        "TestName": testname,
                        "Priority": priority,
                        "Status": "Error",
                        "FailureCount": 0,
                        "Details": [],
                        "Columns": [],
                        "Error": "SQL file not found",
                        "ExecutionTimeSeconds": 0,
                        "ExecutionTimeFormatted": "0 sec",
                        "WebsiteID": ""
                    })
                    user_report_results[reportid].append(("Error", testid, testname, priority, 0, [], [], "SQL file not found"))
                    print(f"TestCaseID: {testid}, CustomerName: {customername}, ReportID: {reportid}, MaxReportDetailID: {max_reportdetailid_new}")
                    continue

                print(f"TestCaseID: {testid}, CustomerName: {customername}, ReportID: {reportid}, MaxReportDetailID: {max_reportdetailid_new}")

                status, failure_count, details, columns, error_msg, details_sql, exec_seconds, formatted_time = run_test_case(
                    conn, sql_path, table_name, reportid, sample_size, max_reportdetailid_new, priority, userid
                )

                result_entry = {
                    "CustomerName": customername,
                    "UserId": userid,
                    "ReportId": reportid,
                    "TestID": testid,
                    "TestName": testname,
                    "Priority": priority,
                    "Status": status,
                    "FailureCount": failure_count,
                    "Details": details,
                    "Columns": columns,
                    "Error": error_msg,
                    "ExecutionTimeSeconds": exec_seconds,
                    "ExecutionTimeFormatted": formatted_time,
                    "WebsiteID": "",
                    "LogDate": datetime.now()
                }
                results.append(result_entry)
                user_report_results[reportid].append((status, testid, testname, priority, failure_count, details, columns, error_msg))

                # Stats handling (store stats results with TCStatus = 'Stats')
                if str(priority).strip().lower() == "stats":
                    try:
                        cursor = conn.cursor()
                        total_count = len(details) if details else 0
                        failure_count_insert = 0
                        website_id = ""
                        comma_sep_ids = ""
                        errordetails_json = ""
                        if details and columns:
                            try:
                                errordetails_json = json.dumps({
                                    "columns": columns,
                                    "rows": [list(r) for r in details]
                                }, default=str)
                            except Exception as e:
                                logging.error(f"Serialization error for stats: {e}")

                        cursor.execute("""
                            SELECT 1 FROM [rg_oprationalbackup].[dbo].[RG_QC_TestRunAutomate_QA]
                            WHERE ReportID = ? AND UserId = ? AND TestCaseID = ? AND WebsiteID = ?
                        """, (reportid, userid, testid, website_id))
                        exists = cursor.fetchone() is not None

                        customer_id_insert = customer_id if customer_id is not None else 0

                        if exists:
                            update_sql = """
                                UPDATE [rg_oprationalbackup].[dbo].[RG_QC_TestRunAutomate_QA]
                                SET TCStatus = ?, TotalCount = ?, TCFailureCount = ?, Requestsegmentid = ?, MaxReportdetailID = ?, LogDate = ?, errordetails = ?,
                                    ExecutionTimeSeconds = ?, ExecutionTimeFormatted = ?
                                WHERE ReportID = ? AND UserId = ? AND TestCaseID = ? AND WebsiteID = ?
                            """
                            cursor.execute(update_sql, (
                                "Stats", total_count, failure_count_insert, comma_sep_ids, max_reportdetailid_new, datetime.now(), errordetails_json,
                                exec_seconds, formatted_time, reportid, userid, testid, website_id
                            ))
                            conn.commit()
                        else:
                            insert_sql = """
                                INSERT INTO [rg_oprationalbackup].[dbo].[RG_QC_TestRunAutomate_QA]
                                (ReportID, AccountName, CustomerId, UserId, TestCaseID, TestCaseInfo, TCPriority, TCStatus,
                                 TotalCount, TCFailureCount, WebsiteID, Requestsegmentid, MaxReportdetailID, LogDate, errordetails,
                                 ExecutionTimeSeconds, ExecutionTimeFormatted)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """
                            cursor.execute(insert_sql, (
                                reportid, customername, customer_id_insert, userid, testid, testname, priority, "Stats",
                                total_count, failure_count_insert, website_id, comma_sep_ids, max_reportdetailid_new, datetime.now(), errordetails_json,
                                exec_seconds, formatted_time
                            ))
                            conn.commit()
                    except Exception as e:
                        logging.error(f"Stats insert/update failed for {testid}, {reportid}, User {userid}: {e}")
                    continue  # next test case

                # Non-stats result handling and insert/update into QC table
                if table_exists:
                    cursor = conn.cursor()
                    website_ids = []
                    website_col_index = -1
                    req_col_index = -1
                    full_rows = []

                    if status == "Fail" and details and columns:
                        try:
                            website_col_index = next((i for i, col in enumerate(columns) if col.lower() == 'websiteid'), -1)
                            if website_col_index != -1:
                                website_ids = [str(row[website_col_index]) for row in details if row[website_col_index] is not None]
                                website_ids = list(set(website_ids))
                        except Exception as e:
                            logging.error(f"Error extracting WebsiteID for Test {testid}, Report {reportid}: {e}")

                        try:
                            if details_sql:
                                execute_sql(cursor, details_sql)
                                full_rows = cursor.fetchall()
                                full_columns = [col[0] for col in cursor.description] if cursor.description else []
                                website_col_index = next((i for i, col in enumerate(full_columns) if col.lower() == 'websiteid'), -1)
                                req_col_index = next((i for i, col in enumerate(full_columns) if col.lower() in ['requestsegmentid', 'request_segment_id', 'requestsegment_id']), -1)
                        except Exception as e:
                            logging.error(f"Error fetching RequestSegmentIDs for Test {testid}, Report {reportid}: {e}")

                    try:
                        if website_ids:
                            placeholders_str, in_params = safe_in_clause_params(website_ids)
                            total_count_sql = f"SELECT COUNT(*) FROM {table_name} WHERE ReportID = ? AND WebsiteID IN {placeholders_str}"
                            params = (reportid,) + in_params
                            cursor.execute(total_count_sql, params)
                        else:
                            cursor.execute(f"SELECT COUNT(*) FROM {table_name} WHERE ReportID = ?", (reportid,))
                        row = cursor.fetchone()
                        total_count = int(row[0]) if row and row[0] is not None else 0
                    except Exception as e:
                        logging.error(f"Error fetching TotalCount for Report {reportid}, User {userid}: {e}")
                        total_count = 0

                    customer_id_insert = customer_id if customer_id is not None else 0
                    website_ids = website_ids or ['']

                    for website_id in website_ids:
                        comma_sep_ids = ''
                        if status == "Fail" and req_col_index != -1 and full_rows and website_col_index != -1:
                            try:
                                req_ids = [str(row[req_col_index]) for row in full_rows
                                           if row[req_col_index] is not None and str(row[website_col_index]) == website_id][:500]
                                comma_sep_ids = ','.join(req_ids) if req_ids else ''
                            except Exception as e:
                                logging.error(f"Error extracting RequestSegmentIDs for website {website_id}: {e}")

                        errordetails_json = ""
                        if status in ("Fail", "Error") and details and columns:
                            try:
                                errordetails_json = json.dumps({
                                    "columns": columns,
                                    "rows": [list(r) for r in details]
                                }, default=str)
                            except Exception as e:
                                logging.error(f"Error serializing errordetails for Test {testid} Report {reportid}: {e}")

                        try:
                            cursor.execute("""
                                SELECT 1 FROM [rg_oprationalbackup].[dbo].[RG_QC_TestRunAutomate_QA]
                                WHERE ReportID = ? AND UserId = ? AND TestCaseID = ? AND WebsiteID = ?
                            """, (reportid, userid, testid, website_id))
                            exists = cursor.fetchone() is not None

                            if exists:
                                update_sql = """
                                    UPDATE [rg_oprationalbackup].[dbo].[RG_QC_TestRunAutomate_QA]
                                    SET TCStatus = ?, TotalCount = ?, TCFailureCount = ?, Requestsegmentid = ?, MaxReportdetailID = ?, LogDate = ?, errordetails = ?,
                                        ExecutionTimeSeconds = ?, ExecutionTimeFormatted = ?
                                    WHERE ReportID = ? AND UserId = ? AND TestCaseID = ? AND WebsiteID = ?
                                """
                                cursor.execute(update_sql, (
                                    status, total_count, failure_count, comma_sep_ids, max_reportdetailid_new, datetime.now(), errordetails_json,
                                    exec_seconds, formatted_time, reportid, userid, testid, website_id
                                ))
                                conn.commit()
                            else:
                                insert_sql = """
                                    INSERT INTO [rg_oprationalbackup].[dbo].[RG_QC_TestRunAutomate_QA]
                                    (ReportID, AccountName, CustomerId, UserId, TestCaseID, TestCaseInfo, TCPriority, TCStatus, 
                                     TotalCount, TCFailureCount, WebsiteID, Requestsegmentid, MaxReportdetailID, LogDate, errordetails,
                                     ExecutionTimeSeconds, ExecutionTimeFormatted)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """
                                cursor.execute(insert_sql, (
                                    reportid, customername, customer_id_insert, userid, testid, testname, priority, status,
                                    total_count, failure_count, website_id, comma_sep_ids, max_reportdetailid_new, datetime.now(), errordetails_json,
                                    exec_seconds, formatted_time
                                ))
                                conn.commit()
                        except Exception as e:
                            logging.error(f"Error inserting/updating into RG_QC_TestRunAutomate_QA for Test {testid}, Report {reportid}, WebsiteID {website_id}: {e}")

            # Removed 'AllTestsPassed' auto-insert per requirement

    # generate consolidated report and send email
    from scripts.generate_full_report_v9 import fetch_report_data_from_db, generate_reports
    historical_results = fetch_report_data_from_db(config, list(all_report_ids))
    consolidated_results = []
    result_keys = set()

    for result in results:
        key = (result['CustomerName'], result['ReportId'], result['TestID'])
        result_keys.add(key)
        consolidated_results.append(result)

    for result in historical_results:
        key = (result['CustomerName'], result['ReportId'], result['TestID'])
        if key not in result_keys:
            consolidated_results.append(result)
            result_keys.add(key)

    update_customer_summary(conn)

    filtered_results = [r for r in consolidated_results if r['Status'].lower() != 'ignore']
    deduped_results = deduplicate_results(filtered_results)

    total_time_sec = sum(r.get('ExecutionTimeSeconds', 0) for r in deduped_results)
    total_time_str = f"{total_time_sec//60} min {total_time_sec%60} sec"

    from collections import defaultdict
    test_summary = defaultdict(int)
    for r in deduped_results:
        status = r['Status'].lower()
        if status in ['pass', 'stats']:
            test_summary['pass'] += 1
        elif status == 'fail':
            test_summary['fail'] += 1
        elif status == 'error':
            test_summary['error'] += 1
    pass_count = test_summary['pass']
    fail_count = test_summary['fail']
    error_count = test_summary['error']
    ignore_count = sum(1 for r in deduped_results if r['Status'].lower() == 'ignore')

    generate_reports(deduped_results, config, total_time_str)
    send_email(config, "QATestOutput.html", total_time_str, pass_count, fail_count, error_count, ignore_count)

    conn.close()

if __name__ == "__main__":
    main()

