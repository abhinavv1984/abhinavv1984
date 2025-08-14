from __future__ import annotations

import os
import re
import time
from typing import Dict, Any, Optional, List, Tuple

import yaml


class ConfigError(Exception):
    pass


def _substitute_env(value: Any) -> Any:
    if isinstance(value, str):
        # ${VAR:default} or ${VAR}
        pattern = re.compile(r"\$\{([^}:]+)(?::([^}]*))?\}")
        def repl(match: re.Match[str]) -> str:
            var = match.group(1)
            default = match.group(2) if match.group(2) is not None else ""
            return os.getenv(var, default)
        return pattern.sub(repl, value)
    return value


def load_db_config(config_path: str, env: str) -> Dict[str, Any]:
    if not os.path.exists(config_path):
        raise ConfigError(f"DB config not found: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    environments = data.get("environments", {})
    if env not in environments:
        raise ConfigError(f"Environment '{env}' not found in {config_path}")
    raw_cfg = environments[env]
    cfg = {k: _substitute_env(v) for k, v in raw_cfg.items()}
    return cfg


def build_connection_string(cfg: Dict[str, Any], database_override: Optional[str] = None) -> str:
    driver = cfg.get("driver", "ODBC Driver 18 for SQL Server")
    server = cfg.get("server")
    port = cfg.get("port")
    database = database_override or cfg.get("database")
    username = cfg.get("username")
    password = cfg.get("password")
    encrypt = cfg.get("encrypt", True)
    trust = cfg.get("trust_server_certificate", False)

    server_part = server if not port else f"{server},{port}"
    parts = [
        f"DRIVER={{{{ {driver} }}}}",
        f"SERVER={server_part}",
        f"DATABASE={database}",
        f"UID={username}",
        f"PWD={password}",
        f"Encrypt={'yes' if encrypt else 'no'}",
        f"TrustServerCertificate={'yes' if trust else 'no'}",
    ]
    return ";".join(parts)


def execute_query(cfg: Dict[str, Any], sql_text: str, timeout_seconds: Optional[int] = None) -> Tuple[List[Dict[str, Any]], float]:
    # Lazy import so that dry-run can work without pyodbc installed
    import pyodbc  # type: ignore

    login_timeout = int(cfg.get("login_timeout_seconds", 15))
    query_timeout = int(timeout_seconds or cfg.get("query_timeout_seconds", 60))

    conn_str = build_connection_string(cfg)
    start = time.time()
    try:
        with pyodbc.connect(conn_str, timeout=login_timeout) as conn:
            conn.setencoding(encoding="utf-8")
            with conn.cursor() as cursor:
                cursor.timeout = query_timeout
                cursor.execute(sql_text)
                if cursor.description is None:
                    rows_list: List[Dict[str, Any]] = []
                else:
                    columns = [column[0] for column in cursor.description]
                    rows = cursor.fetchall()
                    rows_list = [
                        {col: row[idx] for idx, col in enumerate(columns)}
                        for row in rows
                    ]
    finally:
        end = time.time()
    duration = end - start
    return rows_list, duration