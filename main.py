import os
import sys
import json
import time
import glob
import yaml
import re
import logging
from datetime import datetime
from collections import defaultdict

import asyncio

try:
	import pyodbc
except Exception as _:
	pyodbc = None

try:
	import pandas as pd
except Exception as _:
	pd = None


logging.basicConfig(
	filename='error_log.txt',
	level=logging.INFO,
	format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# -----------------------------
# Config
# -----------------------------
def load_config(config_file: str = "config.yaml") -> dict:
	config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), config_file))
	if not os.path.exists(config_path):
		raise FileNotFoundError(f"Configuration file not found: {config_path}")
	with open(config_path, 'r', encoding='utf-8') as f:
		config = yaml.safe_load(f) or {}
	if not config:
		raise ValueError("Empty config")
	return config


# -----------------------------
# DB Service (async facade)
# -----------------------------
class AuthDbService:
	def __init__(self, conn):
		self._conn = conn
		self._cursor = None

	async def execute_query(self, sql: str, params: tuple | None = None):
		# Simple run in thread to avoid blocking event loop
		return await asyncio.to_thread(self._execute_query_blocking, sql, params)

	def _execute_query_blocking(self, sql: str, params: tuple | None):
		cur = self._conn.cursor()
		try:
			if params:
				cur.execute(sql, params)
			else:
				cur.execute(sql)
			rows = cur.fetchall() if cur.description else []
			self._cursor = cur
			return rows
		finally:
			try:
				cur.close()
			except Exception:
				pass

	async def execute_non_query(self, sql: str, params: tuple | None = None):
		return await asyncio.to_thread(self._execute_non_query_blocking, sql, params)

	def _execute_non_query_blocking(self, sql: str, params: tuple | None):
		cur = self._conn.cursor()
		try:
			if params:
				cur.execute(sql, params)
			else:
				cur.execute(sql)
			self._conn.commit()
			return True
		finally:
			try:
				cur.close()
			except Exception:
				pass


def get_db_connection(config: dict):
	db = config.get('database', {})
	if 'test_db' in db and 'auth_db' in db:
		db = db.get('test_db', {})
	conn_str = (
		f"DRIVER={db['driver']};"
		f"SERVER={db['server']};"
		f"DATABASE={db['database']};"
		f"UID={db['username']};"
		f"PWD={db['password']};"
		f"TrustServerCertificate={'yes' if db.get('trust_cert', False) else 'no'};"
	)
	conn = pyodbc.connect(conn_str, autocommit=True)
	try:
		with conn.cursor() as c:
			c.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;")
	except Exception:
		pass
	return conn


# -----------------------------
# Helpers
# -----------------------------
def get_fully_qualified_name(tbl_name: str | None) -> str | None:
	if not tbl_name:
		return None
	t = tbl_name.strip()
	if t.startswith("[") and t.endswith("]"):
		t = t[1:-1]
	parts = t.split('.')
	if len(parts) == 1:
		return f"[dbo].[{parts[0]}]"
	if len(parts) == 2:
		schema, table = parts
		schema = schema or 'dbo'
		return f"[{schema}].[{table}]"
	if len(parts) == 3:
		database, schema, table = parts
		schema = schema or 'dbo'
		return f"[{database}].[{schema}].[{table}]"
	return f"[{t}]"


def load_test_sql_from_file(test_id: str, config: dict, reportid: int, userid: int, max_reportdetail_id: int) -> tuple[str, tuple | None]:
	queries_dir = config.get('test', {}).get('queries_dir', 'queries')
	# resolve absolute
	if not os.path.isabs(queries_dir):
		queries_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), queries_dir))
	pattern = os.path.join(queries_dir, f"{test_id}.sql")
	matches = glob.glob(pattern)
	if not matches:
		matches = glob.glob(os.path.join(queries_dir, f"{test_id.lower()}.sql")) or glob.glob(os.path.join(queries_dir, f"{test_id.upper()}.sql"))
	if not matches:
		raise FileNotFoundError(f"Missing SQL file for {test_id} under {queries_dir}")
	path = matches[0]
	with open(path, 'r', encoding='utf-8') as f:
		raw_sql = f.read().strip()

	# Extract parameter names in order
	param_names = [p.lower() for p in re.findall(r"@[A-Za-z0-9_]+", raw_sql)]
	name_to_value = {
		"@reportid": reportid,
		"@userid": userid,
		"@user": userid,
		"@maxreportdetailid": max_reportdetail_id,
		"@max_reportdetailid": max_reportdetail_id,
	}
	params = []
	unknown = []
	for p in param_names:
		if p in name_to_value:
			params.append(name_to_value[p])
		else:
			unknown.append(p)

	# If the SQL uses named params, convert to positional markers in the same order
	if param_names:
		normalized_sql = re.sub(r"@[A-Za-z0-9_]+", "?", raw_sql)
		if unknown:
			raise ValueError(f"Unknown parameters in {path}: {', '.join(unknown)}")
		return normalized_sql, tuple(params)

	# No params found; default to standard order
	return f"{{CALL {extract_proc_name(raw_sql)} (?, ?, ?)}}", (reportid, userid, max_reportdetail_id)


def extract_proc_name(raw_sql: str) -> str:
	m = re.search(r"EXEC\s+(?:\[(?:[^\]]+)\]\.)?\[?([A-Za-z0-9_]+)\]?", raw_sql, flags=re.IGNORECASE)
	if not m:
		raise ValueError("Could not detect stored procedure name in SQL text")
	return m.group(1)


def fetch_customer_details(conn, userid: int) -> tuple[str, int | None]:
	try:
		cursor = conn.cursor()
		cursor.execute(
			"SELECT TOP 1 DBO.FNEMAILENCODEDECODE(username,'D') as customername, customerid FROM rg_users WITH (NOLOCK) WHERE userid = ?",
			(userid,)
		)
		row = cursor.fetchone()
		return (row.customername if row and getattr(row, 'customername', None) else "N/A", getattr(row, 'customerid', None) if row else None)
	finally:
		try:
			cursor.close()
		except Exception:
			pass


def fetch_latest_report_ids(conn, user_ids: list[int]) -> dict[int, list[int]]:
	report_ids: dict[int, list[int]] = {}
	cursor = conn.cursor()
	try:
		for user_id in user_ids:
			cursor.execute(
				"""
				SELECT ReportID
				FROM RG_Reports WITH (NOLOCK)
				WHERE customerid = ?
				AND DeliveryDate >= DATEADD(DAY, -1, CAST(GETDATE() AS DATE))
				AND DeliveryDate <= CAST(GETDATE() AS DATE)
				""",
				(user_id,)
			)
			rows = cursor.fetchall()
			if rows:
				report_ids[user_id] = [row.ReportID for row in rows]
		return report_ids
	finally:
		try:
			cursor.close()
		except Exception:
			pass


# -----------------------------
# Test runners
# -----------------------------
async def fetch_max_reportdetailid(service: AuthDbService, userid: int, reportid: int, testid: str) -> int:
	try:
		result = await service.execute_query(
			"""
			SELECT ISNULL(MAX(MaxReportdetailID), 0)
			FROM [dbo].[RG_QC_TestRunAutomate_QA]
			WHERE ReportID = ? AND UserId = ? AND TestCaseID = ?
			""",
			(reportid, userid, testid)
		)
		return int(result[0][0]) if result else 0
	except Exception:
		return 0


async def get_new_max_reportdetailid(service: AuthDbService, userid: int, reportid: int) -> int:
	try:
		result = await service.execute_query(
			"""
			DECLARE @TableName NVARCHAR(256);
			EXEC USP_GetResponseTable @UserID = ?, @ReportID = ?, @ResponseTable = @TableName OUTPUT;
			SELECT @TableName;
			""",
			(userid, reportid)
		)
		table_name = result[0][0] if result else None
		if not table_name:
			return 0
		table_name = get_fully_qualified_name(table_name)
		res2 = await service.execute_query(
			f"SELECT ISNULL(MAX(ReportdetailID), 0) FROM {table_name} WHERE reportid = ?",
			(reportid,)
		)
		return int(res2[0][0]) if res2 else 0
	except Exception:
		return 0


async def run_single_test(config: dict, test_id: str):
	start_time = time.time()
	conn = get_db_connection(config)
	service = AuthDbService(conn)

	# Load users
	if pd is None:
		raise RuntimeError("pandas not installed; required to read Excel test file")
	df_users = pd.read_excel(config['test']['testfile'], sheet_name='Users')
	enabled_users = df_users[df_users['Enabled'].str.lower() == 'yes']['UserID'].tolist()
	sample_size = config['test'].get('sample_size', len(enabled_users))
	enabled_users = [uid for uid in enabled_users if uid not in config['test'].get('exclude_userids', [])][:sample_size]

	report_ids_map = fetch_latest_report_ids(conn, enabled_users)
	results = []
	any_report_ids = False

	for userid in enabled_users:
		customername, customer_id = fetch_customer_details(conn, userid)
		report_ids = report_ids_map.get(userid, [])
		if report_ids:
			any_report_ids = True
		for reportid in report_ids:
			max_rdid = await fetch_max_reportdetailid(service, userid, reportid, test_id)
			sql, params = load_test_sql_from_file(test_id, config, reportid, userid, max_rdid)
			try:
				rows = await service.execute_query(sql, params)
				count = int(rows[0][0]) if rows and rows[0] else 0
				new_max = await get_new_max_reportdetailid(service, userid, reportid)
				failure_count = count if test_id != 'TC-11' else 0
				status = 'fail' if failure_count > 0 else 'pass'
				if test_id == 'TC-11':
					status = 'stats'
				exec_seconds = int(time.time() - start_time)
				formatted_time = f"{exec_seconds//60} min {exec_seconds%60} sec"
				await service.execute_non_query(
					"""
					INSERT INTO [dbo].[RG_QC_TestRunAutomate_QA]
					(ReportID, AccountName, CustomerId, UserId, TestCaseID, TestCaseInfo, TCPriority, TCStatus,
					 TotalCount, TCFailureCount, WebsiteID, Requestsegmentid, MaxReportdetailID, LogDate,
					 errordetails, ExecutionTimeSeconds, ExecutionTimeFormatted)
					VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
					""",
					(
						reportid, customername, customer_id or 0, userid, test_id, test_id, 'P1', status,
						count, failure_count, 0, '', new_max, datetime.now(), json.dumps({}), exec_seconds, formatted_time
					)
				)
				results.append({
					"CustomerName": customername,
					"UserId": userid,
					"ReportId": reportid,
					"TestID": test_id,
					"Status": status,
					"FailureCount": failure_count,
					"ExecutionTimeSeconds": exec_seconds,
					"ExecutionTimeFormatted": formatted_time,
					"LogDate": datetime.now(),
				})
			except Exception as e:
				results.append({
					"CustomerName": customername,
					"UserId": userid,
					"ReportId": reportid,
					"TestID": test_id,
					"Status": "error",
					"FailureCount": 0,
					"ExecutionTimeSeconds": int(time.time() - start_time),
					"ExecutionTimeFormatted": f"{int(time.time() - start_time)//60} min {int(time.time() - start_time)%60} sec",
					"LogDate": datetime.now(),
					"Error": str(e),
				})

	if not any_report_ids:
		return {
			"ok": False,
			"results": [],
			"summary": {"pass": 0, "fail": 0, "error": 0, "ignore": 0, "total": 0},
		}

	pass_count = sum(1 for r in results if r['Status'] in ('pass', 'stats'))
	fail_count = sum(1 for r in results if r['Status'] == 'fail')
	error_count = sum(1 for r in results if r['Status'] == 'error')
	return {
		"ok": True,
		"results": results,
		"summary": {
			"pass": pass_count,
			"fail": fail_count,
			"error": error_count,
			"ignore": 0,
			"total": len(results),
		},
	}


async def run_once(config: dict):
	start_time = time.time()
	conn = get_db_connection(config)
	service = AuthDbService(conn)

	if pd is None:
		raise RuntimeError("pandas not installed; required to read Excel test file")
	# read tests and users
	df_tests = pd.read_excel(config['test']['testfile'], sheet_name='Test Cases')
	df_users = pd.read_excel(config['test']['testfile'], sheet_name='Users')
	enabled_tests = df_tests[df_tests['Enabled'].str.lower() == 'yes']
	enabled_users = df_users[df_users['Enabled'].str.lower() == 'yes']['UserID'].tolist()

	sample_size = config['test'].get('sample_size', len(enabled_users))
	enabled_users = [uid for uid in enabled_users if uid not in config['test'].get('exclude_userids', [])][:sample_size]

	report_ids_map = fetch_latest_report_ids(conn, enabled_users)
	results = []
	any_report_ids = False

	for userid in enabled_users:
		customername, customer_id = fetch_customer_details(conn, userid)
		report_ids = report_ids_map.get(userid, [])
		if report_ids:
			any_report_ids = True
		for reportid in report_ids:
			for _, test in enabled_tests.iterrows():
				test_id = test['TestID']
				max_rdid = await fetch_max_reportdetailid(service, userid, reportid, test_id)
				try:
					sql, params = load_test_sql_from_file(test_id, config, reportid, userid, max_rdid)
					rows = await service.execute_query(sql, params)
					count = int(rows[0][0]) if rows and rows[0] else 0
					new_max = await get_new_max_reportdetailid(service, userid, reportid)
					failure_count = count if test_id != 'TC-11' else 0
					status = 'fail' if failure_count > 0 else 'pass'
					if test_id == 'TC-11':
						status = 'stats'
					exec_seconds = int(time.time() - start_time)
					formatted_time = f"{exec_seconds//60} min {exec_seconds%60} sec"
					await service.execute_non_query(
						"""
						INSERT INTO [dbo].[RG_QC_TestRunAutomate_QA]
						(ReportID, AccountName, CustomerId, UserId, TestCaseID, TestCaseInfo, TCPriority, TCStatus,
						 TotalCount, TCFailureCount, WebsiteID, Requestsegmentid, MaxReportdetailID, LogDate,
						 errordetails, ExecutionTimeSeconds, ExecutionTimeFormatted)
						VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
						""",
						(
							reportid, customername, customer_id or 0, userid, test_id, test['TestName'], test['Priority'], status,
							count, failure_count, 0, '', new_max, datetime.now(), json.dumps({}), exec_seconds, formatted_time
						)
					)
					results.append({
						"CustomerName": customername,
						"UserId": userid,
						"ReportId": reportid,
						"TestID": test_id,
						"TestName": test['TestName'],
						"Priority": test['Priority'],
						"Status": status,
						"FailureCount": failure_count,
						"ExecutionTimeSeconds": exec_seconds,
						"ExecutionTimeFormatted": formatted_time,
						"LogDate": datetime.now(),
					})
				except Exception as e:
					results.append({
						"CustomerName": customername,
						"UserId": userid,
						"ReportId": reportid,
						"TestID": test_id,
						"TestName": test.get('TestName', test_id),
						"Priority": test.get('Priority', ''),
						"Status": "error",
						"FailureCount": 0,
						"ExecutionTimeSeconds": int(time.time() - start_time),
						"ExecutionTimeFormatted": f"{int(time.time() - start_time)//60} min {int(time.time() - start_time)%60} sec",
						"LogDate": datetime.now(),
						"Error": str(e),
					})

	if not any_report_ids:
		return {
			"ok": False,
			"results": [],
			"summary": {"pass": 0, "fail": 0, "error": 0, "ignore": 0, "total": 0},
		}

	pass_count = sum(1 for r in results if r['Status'] in ('pass', 'stats'))
	fail_count = sum(1 for r in results if r['Status'] == 'fail')
	error_count = sum(1 for r in results if r['Status'] == 'error')
	return {
		"ok": True,
		"results": results,
		"summary": {
			"pass": pass_count,
			"fail": fail_count,
			"error": error_count,
			"ignore": 0,
			"total": len(results),
		},
	}


# -----------------------------
# CLI
# -----------------------------
def main():
	import argparse
	parser = argparse.ArgumentParser(description="QA Runner using stored procedures from queries folder")
	parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
	parser.add_argument("--once", action="store_true", help="Run all enabled tests for enabled users")
	parser.add_argument("--test", help="Run a single test id, e.g., TC-01")
	args = parser.parse_args()

	config = load_config(args.config)
	if pyodbc is None:
		raise RuntimeError("pyodbc is required to connect to SQL Server")

	if args.test:
		result = asyncio.run(run_single_test(config, args.test))
		print(json.dumps(result, default=str))
		return
	if args.once:
		result = asyncio.run(run_once(config))
		print(json.dumps(result, default=str))
		return
	parser.print_help()


if __name__ == "__main__":
	main()

