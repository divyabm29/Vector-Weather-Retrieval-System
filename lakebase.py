"""
Lakebase (Databricks-managed Postgres) connection helper.

Generates OAuth tokens dynamically for Lakebase connections using the
Databricks SDK. Works for both notebooks (user context) and Databricks Apps
(service principal context).
"""

import base64
import os
from contextlib import contextmanager
from urllib.parse import urlparse

import psycopg2
from databricks.sdk import WorkspaceClient
from psycopg2.extras import RealDictCursor
from sqlalchemy import create_engine

_w = WorkspaceClient()

_SCOPE = os.environ.get("LAKEBASE_SECRET_SCOPE", "database")
_KEY = os.environ.get("LAKEBASE_SECRET_KEY", "lakebase-url")

# Cache for endpoint discovery and user info
_endpoint_cache = {}
_username_cache = None


def _get_base_url() -> str:
    """Fetch and decode the Lakebase connection URL from the Databricks secret scope."""
    secret = _w.secrets.get_secret(scope=_SCOPE, key=_KEY)
    return base64.b64decode(secret.value).decode("utf-8")


def _discover_endpoint(host: str) -> str:
    """
    Find the Lakebase endpoint name for the given host by iterating through
    all projects, branches, and endpoints.
    """
    if host in _endpoint_cache:
        return _endpoint_cache[host]
    
    for project in _w.postgres.list_projects():
        for branch in _w.postgres.list_branches(parent=project.name):
            for endpoint in _w.postgres.list_endpoints(parent=branch.name):
                if endpoint.status and endpoint.status.hosts and endpoint.status.hosts.host == host:
                    _endpoint_cache[host] = endpoint.name
                    return endpoint.name
    
    raise RuntimeError(
        f"No Lakebase endpoint found matching host: {host}. "
        f"Ensure the endpoint exists and is accessible."
    )


def _get_username() -> str:
    """Get the username for database authentication (cached)."""
    global _username_cache
    if _username_cache is None:
        _username_cache = _w.current_user.me().user_name
    return _username_cache


def _lakebase_url() -> str:
    """
    Generate a Lakebase connection URL with a fresh OAuth token.
    Works for both user context and service principal context.
    """
    base_url = _get_base_url()
    parsed = urlparse(base_url)
    
    host = parsed.hostname
    port = parsed.port or 5432
    database = parsed.path.lstrip("/") or "databricks_postgres"
    
    # Discover the endpoint name from the host
    endpoint_name = _discover_endpoint(host)
    
    # Generate OAuth token for this endpoint (works for service principals too)
    creds = _w.postgres.generate_database_credential(endpoint=endpoint_name)
    oauth_token = creds.token
    
    # Get the username (email for users, UUID for service principals)
    username = _get_username()
    
    # Construct connection URL with OAuth credentials
    return f"postgresql://{username}:{oauth_token}@{host}:{port}/{database}?sslmode=require"


@contextmanager
def get_connection():
    """Yield a raw psycopg2 connection with a RealDictCursor factory."""
    conn = psycopg2.connect(_lakebase_url(), cursor_factory=RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()


def get_engine():
    """Return a SQLAlchemy engine for Lakebase."""
    return create_engine(_lakebase_url())


def run_query(sql: str, params: tuple | dict | None = None) -> list[dict]:
    """Run a read query against Lakebase and return rows as list[dict]."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def run_write(sql: str, params: tuple | dict | None = None) -> int:
    """Run an INSERT/UPDATE/DELETE against Lakebase, return affected row count."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            return cur.rowcount