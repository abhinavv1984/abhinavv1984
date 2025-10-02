import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import time
from datetime import datetime
import yaml
import pyodbc
import pandas as pd
import smtplib
from email.mime.text import MIMEText
import json
import logging


logging.basicConfig(
    filename='error_log.txt',
    level=logging.ERROR,
    format='%(asctime)s - %(levelname)s - %(message)s'
)


def load_config(config_file="config.yaml"):
    try:
        with open(config_file) as f:
            return yaml.safe_load(f)
    except Exception as e:
        logging.error(f"Failed to load config: {e}")
        return None


def _drain_remaining_results(cursor) -> None:
    """Best-effort: consume and discard any remaining results to free the connection.

    - Safely handles None or closed cursors
    - Consumes current result rows (if any) in chunks
    - Advances through any additional result sets via nextset()
    """
    if cursor is None:
        return
    try:
        while True:
            # If current result set has a description, attempt to consume rows
            try:
                if getattr(cursor, "description", None):
                    while True:
                        chunk = cursor.fetchmany(1000)
                        if not chunk:
                            break
            except Exception:
                # Ignore fetch errors during draining
                pass

            try:
                more_results = cursor.nextset()
            except Exception:
                # No more results or cursor not in a state to advance
                break
            if not more_results:
                break
    except Exception:
        # Best-effort drain; ignore secondary errors
        pass


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
            "MARS_Connection=Yes;"
        )
        conn = pyodbc.connect(conn_str, autocommit=False)
        return conn
    except Exception as e:
        logging.error(f"Failed to connect DB: {e}")
        return None


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


def get_fully_qualified_name(tbl_name):
    if not tbl_name:
        return None
    tbl_name = str(tbl_name).strip()
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


def execute_sql(cursor, sql_text, params=()):
    try:
        if params:
            cursor.execute(sql_text, params)
        else:
            cursor.execute(sql_text)
    except Exception:
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


def compute_stats_counts(columns, rows):
    try:
        if not rows:
            return 0, 0
        lower_cols = [c.lower() for c in columns] if columns else []
        def col_index(name):
            return next((i for i, c in enumerate(lower_cols) if c == name), -1)

        idx_totalrequest = col_index('totalrequest')
        idx_totalrecords = col_index('totalrecords')
        idx_available = col_index('available')
        idx_failed = col_index('failed')

        total_count = 0
        failure_count = 0

        if idx_totalrequest != -1:
            total_count = sum(int(r[idx_totalrequest] or 0) for r in rows)
        elif idx_totalrecords != -1:
            total_count = sum(int(r[idx_totalrecords] or 0) for r in rows)
        else:
            total_count = len(rows)

        if idx_failed != -1:
            failure_count = sum(int(r[idx_failed] or 0) for r in rows)
        elif idx_available != -1:
            try:
                available_sum = sum(int(r[idx_available] or 0) for r in rows)
                failure_count = max(total_count - available_sum, 0)
            except Exception:
                failure_count = 0

        return total_count, failure_count
    except Exception as e:
        logging.error(f"compute_stats_counts error: {e}")
        return (len(rows) if rows else 0), 0


def fetch_customer_details(conn, userid):
    cursor = None
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
    finally:
        try:
            _drain_remaining_results(cursor)
        except Exception:
            pass
        try:
            if cursor is not None:
                cursor.close()
        except Exception:
            pass


def get_dynamic_table_name(conn, userid, reportid):
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute("""
            DECLARE @UserDetailTable VARCHAR(256);
            EXEC USP_GetResponseTable @UserID = ?, @ReportID = ?, @ResponseTable = @UserDetailTable OUTPUT;
            SELECT @UserDetailTable AS TableName;
        """, (userid, reportid))

        found_table = None
        while True:
            # If current result set has the TableName column, read it
            if cursor.description:
                cols = [d[0] for d in cursor.description]
                if len(cols) == 1 and cols[0].lower() == 'tablename':
                    row = cursor.fetchone()
                    if row and row[0] is not None:
                        found_table = row[0]
            try:
                more = cursor.nextset()
            except pyodbc.Error:
                break
            if not more:
                break

        if found_table:
            return get_fully_qualified_name(found_table)
        return None
    except Exception as e:
        logging.error(f"Error fetching dynamic table name for User {userid}, Report {reportid}: {e}")
        return None
    finally:
        try:
            _drain_remaining_results(cursor)
        except Exception:
            pass
        try:
            if cursor is not None:
                cursor.close()
        except Exception:
            pass


def fetch_max_reportdetailid_from_qc_table(conn, userid, reportid):
    cursor = None
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
    finally:
        try:
            _drain_remaining_results(cursor)
        except Exception:
            pass
        try:
            if cursor is not None:
                cursor.close()
        except Exception:
            pass


def fetch_max_reportdetailid_from_dynamic_table(conn, table_name, reportid):
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT MAX(ReportDetailID) FROM {table_name} WHERE ReportID = ?", (reportid,))
        row = cursor.fetchone()
        return row[0] if row and row[0] is not None else 0
    except Exception as e:
        logging.error(f"Error fetching max ReportDetailID from {table_name} for Report {reportid}: {e}")
        return 0
    finally:
        try:
            _drain_remaining_results(cursor)
        except Exception:
            pass
        try:
            if cursor is not None:
                cursor.close()
        except Exception:
            pass


def has_new_data(conn, table_name, reportid, max_reportdetailid_prev):
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT MAX(ReportDetailID) FROM {table_name} WHERE ReportID = ?", (reportid,))
        row = cursor.fetchone()
        max_dynamic_reportdetailid = row[0] if row and row[0] is not None else 0
        return max_dynamic_reportdetailid > max_reportdetailid_prev
    except Exception as e:
        logging.error(f"Error checking new data for Report {reportid}: {e}")
        return False
    finally:
        try:
            _drain_remaining_results(cursor)
        except Exception:
            pass
        try:
            if cursor is not None:
                cursor.close()
        except Exception:
            pass


def build_replacements(table_name, reportid, userid, lower_max_reportdetailid, upper_max_reportdetailid):
    lower_val = str(int(lower_max_reportdetailid) if lower_max_reportdetailid is not None else 0)
    upper_val = str(int(upper_max_reportdetailid) if upper_max_reportdetailid is not None else 0)
    return {
        "{table}": table_name or "",
        "{table_name}": table_name or "",
        "{reportid}": str(int(reportid) if reportid is not None else 0),
        "{userid}": str(int(userid) if userid is not None else 0),
        "{user}": str(int(userid) if userid is not None else 0),
        "{max_reportdetailid}": lower_val,
        "{details}": lower_val,
        "{max_reportdetailid_lower}": lower_val,
        "{max_reportdetailid_upper}": upper_val,
        "{from_max_reportdetailid}": lower_val,
        "{to_max_reportdetailid}": upper_val,
        "{prev_max_reportdetailid}": lower_val,
        "{new_max_reportdetailid}": upper_val,
    }


def apply_replacements(sql_text, replacements):
    out = sql_text
    for key, value in replacements.items():
        out = out.replace(key, value)
    return out


def run_test_case(conn, sql_path, table_name, reportid, sample_size, lower_max_reportdetailid, upper_max_reportdetailid, priority, userid):
    count_cursor = None
    details_cursor = None
    stats_cursor = None
    try:
        start_time = time.time()
        with open(sql_path, "r", encoding="utf-8") as f:
            raw_sql = f.read().strip()

        replacements = build_replacements(table_name, reportid, userid, lower_max_reportdetailid, upper_max_reportdetailid)
        sql_text = apply_replacements(raw_sql, replacements)

        details_marker = "-- DETAILS_QUERY"
        count_marker = "-- COUNT_QUERY"

        # Stats priority: single query returning summary
        if priority and str(priority).strip().lower() == "stats":
            try:
                stats_cursor = conn.cursor()
                execute_sql(stats_cursor, sql_text)
                full_rows = stats_cursor.fetchall()
                columns = [col[0] for col in stats_cursor.description] if stats_cursor.description else []
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
            finally:
                try:
                    _drain_remaining_results(stats_cursor)
                except Exception:
                    pass
                try:
                    if stats_cursor is not None:
                        stats_cursor.close()
                except Exception:
                    pass

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

        count_value = 0
        if count_sql:
            try:
                count_cursor = conn.cursor()
                execute_sql(count_cursor, count_sql)
                row = count_cursor.fetchone()
                count_value = int(row[0]) if row and row[0] is not None else 0
            except Exception as e:
                error_msg = str(e)
                logging.error(f"Count query failed for {sql_path}, Report {reportid}, User {userid}: {error_msg}")
                end_time = time.time()
                exec_seconds = int(end_time - start_time)
                formatted = f"{exec_seconds} sec"
                return "Error", 0, [], [], error_msg, details_sql, exec_seconds, formatted
            finally:
                try:
                    _drain_remaining_results(count_cursor)
                except Exception:
                    pass
                try:
                    if count_cursor is not None:
                        count_cursor.close()
                except Exception:
                    pass
        else:
            try:
                stripped = details_sql.lstrip().lower()
                if stripped.startswith("select"):
                    count_wrapper = f"SELECT COUNT(*) FROM ({details_sql}) AS _ct"
                    count_cursor = conn.cursor()
                    execute_sql(count_cursor, count_wrapper)
                    row = count_cursor.fetchone()
                    count_value = int(row[0]) if row and row[0] is not None else 0
                else:
                    count_value = None
            except Exception as e:
                logging.error(f"Count fallback failed for {sql_path}: {e}")
                count_value = None
            finally:
                try:
                    _drain_remaining_results(count_cursor)
                except Exception:
                    pass
                try:
                    if count_cursor is not None:
                        count_cursor.close()
                except Exception:
                    pass

        if priority and str(priority).strip().lower() == "low":
            end_time = time.time()
            exec_seconds = int(end_time - start_time)
            formatted = f"{exec_seconds} sec"
            return "Ignore", 0, [], [], "", details_sql, exec_seconds, formatted

        if count_value == 0:
            end_time = time.time()
            exec_seconds = int(end_time - start_time)
            formatted = f"{exec_seconds} sec"
            return "Pass", 0, [], [], "", details_sql, exec_seconds, formatted

        try:
            details_cursor = conn.cursor()
            execute_sql(details_cursor, details_sql)
            columns = [c[0] for c in details_cursor.description] if details_cursor.description else []
            rows = details_cursor.fetchmany(sample_size)
            rows = [tuple(r) for r in rows]
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
        finally:
            try:
                _drain_remaining_results(details_cursor)
            except Exception:
                pass
            try:
                if details_cursor is not None:
                    details_cursor.close()
            except Exception:
                pass
    except Exception as e:
        logging.error(f"Execution error in run_test_case for {sql_path}, Report {reportid}, User {userid}: {e}")
        return "Error", 0, [], [], str(e), "", 0, "0 sec"


def safe_in_clause_params(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return "(NULL)", tuple()
    placeholders = ", ".join(["?"] * len(vals))
    return f"({placeholders})", tuple(vals)


def upsert_result_via_sp(conn, row):
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            EXEC RG_OprationalBackup.dbo.USP_QC_TestRunAutomate_Upsert
              ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            """,
            (
                row['ReportId'],
                row['AccountName'],
                row['CustomerId'],
                row['UserId'],
                row['TestCaseID'],
                row['TestCaseInfo'],
                row['TCPriority'],
                row['TCStatus'],
                row['TotalCount'],
                row['TCFailureCount'],
                row['WebsiteID'] or '',
                row['Requestsegmentid'],
                row['MaxReportdetailID'],
                row['LogDate'],
                row['errordetails'],
                row['ExecutionTimeSeconds'],
                row['ExecutionTimeFormatted'],
            )
        )
        _drain_remaining_results(cursor)
        conn.commit()
    except Exception as e:
        logging.error(f"SP upsert failed for Report {row['ReportId']} User {row['UserId']} Test {row['TestCaseID']} Website {row['WebsiteID']}: {e}")
    finally:
        try:
            if cursor is not None:
                cursor.close()
        except Exception:
            pass


def recompute_summary_via_sp(conn):
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute("EXEC RG_OprationalBackup.dbo.USP_QC_SummaryDaily_RecomputeForToday")
        _drain_remaining_results(cursor)
        conn.commit()
    except Exception as e:
        logging.error(f"SP summary recompute failed: {e}")
    finally:
        try:
            if cursor is not None:
                cursor.close()
        except Exception:
            pass


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
    users_df = users_df[(users_df['Enabled'].astype(str).str.lower() == 'yes') & (~users_df['UserID'].isin(exclude_userids))]
    testcase_df = testcase_df[testcase_df['Enabled'].astype(str).str.lower() == 'yes']

    conn = get_db_connection(config)
    if not conn:
        return

    # Check QC table existence
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT TOP 0 * FROM [rg_oprationalbackup].[dbo].[RG_QC_TestRunAutomate_QA]")
        table_exists = True
    except Exception as e:
        logging.error(f"QC table check failed: {e}")
        table_exists = False
    finally:
        try:
            _drain_remaining_results(cursor)
        except Exception:
            pass
        try:
            if cursor is not None:
                cursor.close()
        except Exception:
            pass

    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    sql_folder = os.path.join(base_dir, "queries")
    sample_size = int(config['test'].get('sample_size', 10))

    all_report_ids = set()

    for _, user in users_df.iterrows():
        userid = int(user['UserID'])
        report_type_raw = user.get('Reportype', None)
        if pd.isna(report_type_raw):
            report_type_raw = user.get('ReportType', "")
        report_type = str(report_type_raw).strip().lower()
        customername, customer_id = fetch_customer_details(conn, userid)

        # Fetch reports for this customer
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT reportid FROM RG_Reports WITH (NOLOCK)
                WHERE customerid = ?
                AND DeliveryDate >= DATEADD(DAY, -1, CAST(GETDATE() AS DATE))
                AND DeliveryDate < DATEADD(DAY, 1, CAST(GETDATE() AS DATE))
                """,
                (customer_id,)
            )
            report_rows = cursor.fetchall()
            report_ids = [row.reportid for row in report_rows]
            # Fallback: if none found (or customer_id missing), try by userid
            if not report_ids:
                try:
                    _drain_remaining_results(cursor)
                except Exception:
                    pass
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT reportid FROM RG_Reports WITH (NOLOCK)
                    WHERE userid = ?
                    AND DeliveryDate >= DATEADD(DAY, -1, CAST(GETDATE() AS DATE))
                    AND DeliveryDate < DATEADD(DAY, 1, CAST(GETDATE() AS DATE))
                    """,
                    (userid,)
                )
                report_ids = [row.reportid for row in cursor.fetchall()]
            all_report_ids.update(report_ids)
        except Exception as e:
            logging.error(f"Error fetching reports for User {userid}: {e}")
            report_ids = []
        finally:
            try:
                _drain_remaining_results(cursor)
            except Exception:
                pass
            try:
                if cursor is not None:
                    cursor.close()
            except Exception:
                pass

        for reportid in report_ids:
            table_name = get_dynamic_table_name(conn, userid, reportid)
            if not table_name:
                logging.error(f"Unable to determine table name for User {userid} Report {reportid}, skipping all tests")
                continue

            max_reportdetailid_new = fetch_max_reportdetailid_from_dynamic_table(conn, table_name, reportid)
            max_reportdetailid_prev = fetch_max_reportdetailid_from_qc_table(conn, userid, reportid)

            def should_run_for_report():
                if not table_exists:
                    return True
                c2 = None
                try:
                    c2 = conn.cursor()
                    c2.execute(
                        """
                        SELECT 1 FROM [rg_oprationalbackup].[dbo].[RG_QC_TestRunAutomate_QA]
                        WHERE UserId = ? AND ReportID = ?
                        """,
                        (userid, reportid)
                    )
                    exists = c2.fetchone() is not None
                    if not exists:
                        return True
                    return has_new_data(conn, table_name, reportid, max_reportdetailid_prev)
                except Exception as e_inner:
                    logging.error(f"Error determining should_run for User {userid} Report {reportid}: {e_inner}")
                    return True
                finally:
                    try:
                        _drain_remaining_results(c2)
                    except Exception:
                        pass
                    try:
                        if c2 is not None:
                            c2.close()
                    except Exception:
                        pass

            if not should_run_for_report():
                continue

            for _, test_row in testcase_df.iterrows():
                runtype_raw = str(test_row.get("Runtype", ""))
                runtype_list = [v.strip().lower() for v in runtype_raw.split(",") if v.strip()]
                # If no runtype specified on test case, treat as applicable to all
                if runtype_list and report_type not in runtype_list:
                    continue

                testid = test_row['TestID']
                testname = test_row['TestName']
                priority = test_row['Priority']

                sql_path = os.path.join(sql_folder, f"{testid}.sql")
                if not os.path.isfile(sql_path):
                    logging.error(f"SQL file {sql_path} not found for TestID {testid}, skipping.")
                    print(f"TestCaseID: {testid}, CustomerName: {customername}, ReportID: {reportid}, MaxReportDetailID: {max_reportdetailid_new}")
                    continue

                print(f"TestCaseID: {testid}, CustomerName: {customername}, ReportID: {reportid}, MaxReportDetailID: {max_reportdetailid_new}")

                status, failure_count, details, columns, error_msg, details_sql, exec_seconds, formatted_time = run_test_case(
                    conn, sql_path, table_name, reportid, sample_size, max_reportdetailid_prev, max_reportdetailid_new, priority, userid
                )

                if str(priority).strip().lower() == "stats":
                    errordetails_json = ""
                    if details and columns:
                        try:
                            errordetails_json = json.dumps({
                                "columns": columns,
                                "rows": [list(r) for r in details]
                            }, default=str)
                        except Exception as e:
                            logging.error(f"Serialization error for stats: {e}")

                    total_count_stats, failure_count_stats = compute_stats_counts(columns, details)

                    upsert_result_via_sp(conn, {
                        'ReportId': reportid,
                        'AccountName': customername,
                        'CustomerId': customer_id or 0,
                        'UserId': userid,
                        'TestCaseID': testid,
                        'TestCaseInfo': testname,
                        'TCPriority': priority,
                        'TCStatus': 'Stats',
                        'TotalCount': total_count_stats,
                        'TCFailureCount': failure_count_stats,
                        'WebsiteID': '',
                        'Requestsegmentid': '',
                        'MaxReportdetailID': max_reportdetailid_new,
                        'LogDate': datetime.now(),
                        'errordetails': errordetails_json,
                        'ExecutionTimeSeconds': exec_seconds,
                        'ExecutionTimeFormatted': formatted_time
                    })
                    continue

                website_ids = []
                req_col_index = -1
                website_col_index = -1
                full_rows = []

                if status == "Fail" and details and columns:
                    try:
                        website_col_index = next((i for i, col in enumerate(columns) if col.lower() == 'websiteid'), -1)
                        if website_col_index != -1:
                            website_ids = [str(row[website_col_index]) for row in details if row[website_col_index] is not None]
                            website_ids = list(set(website_ids))
                    except Exception as e:
                        logging.error(f"Error extracting WebsiteID for Test {testid}, Report {reportid}: {e}")

                    c3 = None
                    try:
                        c3 = conn.cursor()
                        execute_sql(c3, details_sql)
                        full_rows = c3.fetchall()
                        full_columns = [col[0] for col in c3.description] if c3.description else []
                        website_col_index = next((i for i, col in enumerate(full_columns) if col.lower() == 'websiteid'), -1)
                        req_col_index = next((i for i, col in enumerate(full_columns) if col.lower() in ['requestsegmentid', 'request_segment_id', 'requestsegment_id']), -1)
                    except Exception as e:
                        logging.error(f"Error fetching RequestSegmentIDs for Test {testid}, Report {reportid}: {e}")
                    finally:
                        try:
                            _drain_remaining_results(c3)
                        except Exception:
                            pass
                        try:
                            if c3 is not None:
                                c3.close()
                        except Exception:
                            pass

                errordetails_json = ""
                if status in ("Fail", "Error") and details and columns:
                    try:
                        errordetails_json = json.dumps({
                            "columns": columns,
                            "rows": [list(r) for r in details]
                        }, default=str)
                    except Exception as e:
                        logging.error(f"Error serializing errordetails for Test {testid} Report {reportid}: {e}")

                if not website_ids:
                    website_ids = ['']

                for website_id in website_ids:
                    comma_sep_ids = ''
                    if status == "Fail" and req_col_index != -1 and full_rows and website_col_index != -1:
                        try:
                            req_ids = [str(row[req_col_index]) for row in full_rows
                                       if row[req_col_index] is not None and str(row[website_col_index]) == website_id][:500]
                            comma_sep_ids = ','.join(req_ids) if req_ids else ''
                        except Exception as e:
                            logging.error(f"Error extracting RequestSegmentIDs for website {website_id}: {e}")

                    upsert_result_via_sp(conn, {
                        'ReportId': reportid,
                        'AccountName': customername,
                        'CustomerId': customer_id or 0,
                        'UserId': userid,
                        'TestCaseID': testid,
                        'TestCaseInfo': testname,
                        'TCPriority': priority,
                        'TCStatus': status,
                        'TotalCount': failure_count if failure_count is not None else 0,
                        'TCFailureCount': failure_count if failure_count is not None else 0,
                        'WebsiteID': website_id,
                        'Requestsegmentid': comma_sep_ids,
                        'MaxReportdetailID': max_reportdetailid_new,
                        'LogDate': datetime.now(),
                        'errordetails': errordetails_json,
                        'ExecutionTimeSeconds': exec_seconds,
                        'ExecutionTimeFormatted': formatted_time
                    })

    # Generate report and email as before
    from scripts.generate_full_report_v9 import fetch_report_data_from_db, generate_reports
    historical_results = fetch_report_data_from_db(config, list(all_report_ids))

    filtered_results = [r for r in historical_results if r['Status'].lower() != 'ignore']
    from collections import defaultdict
    test_summary = defaultdict(int)
    total_time_sec = sum(r.get('ExecutionTimeSeconds', 0) for r in filtered_results)
    for r in filtered_results:
        status = r['Status'].lower()
        if status in ['pass', 'stats']:
            test_summary['pass'] += 1
        elif status == 'fail':
            test_summary['fail'] += 1
        elif status == 'error':
            test_summary['error'] += 1

    total_time_str = f"{total_time_sec//60} min {total_time_sec%60} sec"
    pass_count = test_summary['pass']
    fail_count = test_summary['fail']
    error_count = test_summary['error']
    ignore_count = sum(1 for r in filtered_results if r['Status'].lower() == 'ignore')

    generate_reports(filtered_results, config, total_time_str)

    recompute_summary_via_sp(conn)
    send_email(config, "QATestOutput.html", total_time_str, pass_count, fail_count, error_count, ignore_count)

    conn.close()


if __name__ == "__main__":
    main()

