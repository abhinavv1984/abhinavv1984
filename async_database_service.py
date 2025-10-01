import os
import asyncio
import logging
from typing import Optional, List, Dict, Any, Tuple, Sequence

import pyodbc


# Ensure pyodbc connection pooling is enabled (process-wide)
pyodbc.pooling = True

logger = logging.getLogger(__name__)


def build_connection_string(config: dict, db_type: str = "test_db") -> str:
    """Build connection string for specified database type.

    Supports both legacy single-DB config and new dual-DB config under
    config['database'] with keys 'test_db' and 'auth_db'.
    """
    db_config = config.get("database", {})

    # Handle both old single database format and new dual database format
    if "test_db" in db_config and "auth_db" in db_config:
        db = db_config.get(db_type, {})
    else:
        # Old single database format (backward compatibility)
        db = db_config

    driver = db.get("driver") or os.getenv("DB_DRIVER") or "{ODBC Driver 17 for SQL Server}"
    server = db.get("server") or os.getenv("DB_SERVER", "")
    database = db.get("database") or os.getenv("DB_NAME", "")
    username = db.get("username") or os.getenv("DB_USER", "")
    password = db.get("password") or os.getenv("DB_PASSWORD", "")
    trust = db.get("trust_cert", True)
    trust_part = "TrustServerCertificate=yes;" if trust else ""

    return (
        f"DRIVER={driver};SERVER={server};DATABASE={database};UID={username};PWD={password};" + trust_part
    )


def get_connection(config: dict, db_type: str = "test_db"):
    """Get a pooled pyodbc connection using the provided config."""
    conn_str = build_connection_string(config, db_type)
    return pyodbc.connect(conn_str)


class AsyncDatabaseService:
    """Async wrapper for database operations using a thread pool.

    Each operation creates and closes its own pyodbc connection to remain
    thread-safe with run_in_executor.
    """

    def __init__(self, config: dict, db_type: str = "test_db") -> None:
        self.config = config
        self.db_type = db_type
        self.conn_str = build_connection_string(config, db_type)

    async def execute_query(self, query: str, params: Optional[Tuple[Any, ...]] = None) -> List[Dict[str, Any]]:
        """Execute a SELECT query and return results as list of dicts."""

        def _execute() -> List[Dict[str, Any]]:
            conn = pyodbc.connect(self.conn_str)
            cursor = conn.cursor()
            try:
                if params:
                    cursor.execute(query, params)
                else:
                    cursor.execute(query)

                columns = [column[0] for column in cursor.description] if cursor.description else []
                rows = cursor.fetchall()

                results: List[Dict[str, Any]] = []
                for row in rows:
                    results.append(dict(zip(columns, row)))
                return results
            finally:
                conn.close()

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _execute)

    async def execute_scalar(self, query: str, params: Optional[Tuple[Any, ...]] = None) -> Any:
        """Execute a query and return a single scalar value."""

        def _execute() -> Any:
            conn = pyodbc.connect(self.conn_str)
            cursor = conn.cursor()
            try:
                if params:
                    cursor.execute(query, params)
                else:
                    cursor.execute(query)
                row = cursor.fetchone()
                return row[0] if row else None
            finally:
                conn.close()

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _execute)

    async def execute_non_query(self, query: str, params: Optional[Tuple[Any, ...]] = None) -> int:
        """Execute INSERT/UPDATE/DELETE and return affected rows count."""

        def _execute() -> int:
            conn = pyodbc.connect(self.conn_str)
            cursor = conn.cursor()
            try:
                if params:
                    cursor.execute(query, params)
                else:
                    cursor.execute(query)
                conn.commit()
                return cursor.rowcount
            finally:
                conn.close()

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _execute)

    async def execute_many(self, query: str, params_list: Sequence[Tuple[Any, ...]]) -> int:
        """Execute query with multiple parameter sets."""

        def _execute() -> int:
            conn = pyodbc.connect(self.conn_str)
            cursor = conn.cursor()
            try:
                cursor.executemany(query, params_list)
                conn.commit()
                return cursor.rowcount
            finally:
                conn.close()

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _execute)

    async def authenticate_user(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        """Authenticate user against RG_QC_Users table using the auth database."""
        auth_service = AsyncDatabaseService(self.config, "auth_db")

        query = (
            """
            SELECT username, password, mailid, role, is_active 
            FROM RG_QC_Users 
            WHERE username = ? AND is_active = 1
            """
        )
        try:
            result = await auth_service.execute_query(query, (username,))
            logger.info(
                f"Authentication attempt for user {username}: found {len(result) if result else 0} records"
            )

            if result:
                user = result[0]
                logger.info(
                    f"User {username} found: role='{user.get('role')}', is_active={user.get('is_active')}"
                )
                if user.get("password") == password:
                    logger.info(f"Authentication successful for user {username}")
                    return user
                else:
                    logger.warning(f"Password mismatch for user {username}")
            else:
                logger.warning(f"No active user found with username {username}")
            return None
        except Exception as exc:  # Broad except to allow fallback
            logger.error(f"Authentication error for user {username}: {exc}")
            return await self._fallback_auth(username, password)

    async def _fallback_auth(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        """Fallback authentication using config file if database table doesn't exist."""
        try:
            auth_config = self.config.get("auth", {})
            fallback_users = auth_config.get("fallback_users", [])

            if not fallback_users:
                # Default fallback users
                fallback_users = [
                    {"username": "admin", "password": "admin123", "role": "admin"},
                    {"username": "user", "password": "user123", "role": "user"},
                ]

            for user in fallback_users:
                if user.get("username") == username and user.get("password") == password:
                    logger.info(f"Fallback authentication successful for user {username}")
                    return user

            logger.warning(f"Fallback authentication failed for user {username}")
            return None
        except Exception as exc:  # Keep resilient
            logger.error(f"Fallback authentication error: {exc}")
            return None

    async def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """Get user details by username using the auth database."""
        auth_service = AsyncDatabaseService(self.config, "auth_db")

        query = (
            """
            SELECT username, password, mailid, role, is_active, created_at, last_login
            FROM RG_QC_Users 
            WHERE username = ?
            """
        )
        try:
            result = await auth_service.execute_query(query, (username,))
            return result[0] if result else None
        except Exception as exc:
            logger.error(f"Error fetching user {username}: {exc}")
            return None

    async def __aenter__(self) -> "AsyncDatabaseService":
        """Allow using `async with` without requiring persistent connections."""
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool:
        """No persistent resources to release; return False to propagate exceptions."""
        return False

    # -----------------------------
    # Stored Procedure Helpers
    # -----------------------------
    def _build_sp_exec_sql(
        self,
        procedure_name: str,
        params: Optional[Dict[str, Any]] = None,
        types: Optional[Dict[str, str]] = None,
    ) -> Tuple[str, Tuple[Any, ...]]:
        """Build an EXEC statement for a stored procedure with explicit type casts.

        - If `types` provides a SQL type for a parameter (e.g., 'INT', 'BIGINT', 'NVARCHAR(100)'),
          the placeholder will be wrapped as CAST(? AS <TYPE>).
        - If no type is provided for an integer parameter, auto-cast based on value range:
            - |value| <= 2_147_483_647 -> INT
            - otherwise -> BIGINT
        - Other types will use an untyped placeholder `?` by default.
        """

        if not params:
            return f"EXEC {procedure_name}", tuple()

        assignments: List[str] = []
        values: List[Any] = []

        for name, value in params.items():
            sql_type: Optional[str] = None
            if types and name in types and types[name]:
                sql_type = types[name].strip()
            else:
                # Auto-type only for ints if not provided
                if isinstance(value, int):
                    if abs(value) <= 2_147_483_647:
                        sql_type = "INT"
                    else:
                        sql_type = "BIGINT"

            if sql_type:
                assignments.append(f"@{name} = CAST(? AS {sql_type})")
            else:
                assignments.append(f"@{name} = ?")
            values.append(value)

        assign_sql = ", ".join(assignments)
        sql = f"EXEC {procedure_name} {assign_sql}"
        return sql, tuple(values)

    async def execute_sp_query(
        self,
        procedure_name: str,
        params: Optional[Dict[str, Any]] = None,
        types: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        """Execute a stored procedure and return a list of rows (as dicts)."""

        def _execute() -> List[Dict[str, Any]]:
            sql, values = self._build_sp_exec_sql(procedure_name, params, types)
            conn = pyodbc.connect(self.conn_str)
            cursor = conn.cursor()
            try:
                cursor.execute(sql, values)
                columns = [column[0] for column in cursor.description] if cursor.description else []
                rows = cursor.fetchall() if cursor.description else []
                results: List[Dict[str, Any]] = []
                for row in rows:
                    results.append(dict(zip(columns, row)))
                return results
            finally:
                conn.close()

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _execute)

    async def execute_sp_scalar(
        self,
        procedure_name: str,
        params: Optional[Dict[str, Any]] = None,
        types: Optional[Dict[str, str]] = None,
    ) -> Any:
        """Execute a stored procedure and return the first column of the first row."""

        def _execute() -> Any:
            sql, values = self._build_sp_exec_sql(procedure_name, params, types)
            conn = pyodbc.connect(self.conn_str)
            cursor = conn.cursor()
            try:
                cursor.execute(sql, values)
                row = cursor.fetchone()
                return row[0] if row else None
            finally:
                conn.close()

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _execute)

    async def execute_sp_non_query(
        self,
        procedure_name: str,
        params: Optional[Dict[str, Any]] = None,
        types: Optional[Dict[str, str]] = None,
    ) -> int:
        """Execute a stored procedure that does not return a result set.

        Returns the cursor.rowcount (may be -1 for procedures that do not set it).
        """

        def _execute() -> int:
            sql, values = self._build_sp_exec_sql(procedure_name, params, types)
            conn = pyodbc.connect(self.conn_str)
            cursor = conn.cursor()
            try:
                cursor.execute(sql, values)
                conn.commit()
                return cursor.rowcount
            finally:
                conn.close()

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _execute)


def get_test_db_service(config: dict) -> AsyncDatabaseService:
    """Get database service for test execution (RG_Centralizeddb)."""
    return AsyncDatabaseService(config, "test_db")


def get_auth_db_service(config: dict) -> AsyncDatabaseService:
    """Get database service for authentication (RG_OprationalBackup)."""
    return AsyncDatabaseService(config, "auth_db")

