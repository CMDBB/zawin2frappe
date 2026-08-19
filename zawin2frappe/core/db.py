"""Single source of truth for the ZaWin connection.

Replaces the credentials that were previously copy-pasted across main.py,
query.sh and assorted shell history. Reads from the environment, falling back
to the compose.yaml defaults so a bare checkout still works locally.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import warnings
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pymssql

log = logging.getLogger(__name__)

#: App root — data/ lives beside the package, not inside it.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
DERIVED_DIR = DATA_DIR / "derived"
RAW_DIR = DATA_DIR / "raw"
REFERENCE_DIR = DATA_DIR / "reference"


def _site_conf() -> dict:
	"""Frappe's site config, but only if we are already inside a site.

	Deliberately checks `sys.modules` rather than importing: the core must not
	pull Frappe into a standalone process just because it happens to be
	installed in the same environment. Inside bench, frappe is already imported
	by the time any of this runs.
	"""
	frappe = sys.modules.get("frappe")
	try:
		return dict(frappe.conf or {}) if frappe is not None else {}
	except Exception:
		return {}


def _load_dotenv() -> None:
	"""Minimal .env loader — avoids a dependency for five variables."""
	env_file = PROJECT_ROOT / ".env"
	if not env_file.is_file():
		return
	for line in env_file.read_text(encoding="utf-8").splitlines():
		line = line.strip()
		if not line or line.startswith("#") or "=" not in line:
			continue
		key, _, value = line.partition("=")
		os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def connection_settings() -> dict[str, str | int]:
	"""Where ZaWin lives.

	Inside a Frappe site, `zawin_mssql` in site_config.json wins — that is the
	supported place for per-site credentials, and it keeps them out of the
	environment and out of git. Otherwise the environment is used, so the
	exploration CLI still works with no site at all.
	"""
	site = _site_conf().get("zawin_mssql") or {}
	_load_dotenv()
	return {
		"server": site.get("host") or os.environ.get("MSSQL_HOST", "localhost"),
		"port": int(site.get("port") or os.environ.get("MSSQL_PORT", "14330")),
		"user": site.get("user") or os.environ.get("MSSQL_USER", "sa"),
		"password": site.get("password") or os.environ.get("MSSQL_PASSWORD", ""),
		"database": site.get("database") or os.environ.get("MSSQL_DATABASE", "ZaWin"),
	}


@contextlib.contextmanager
def get_conn() -> Iterator[pymssql.Connection]:
	"""Yield a ZaWin connection, always closed on exit."""
	settings = connection_settings()
	log.debug("connecting to %(server)s:%(port)s/%(database)s", settings)
	conn = pymssql.connect(**settings)
	try:
		yield conn
	finally:
		conn.close()


def query(sql: str, params: tuple | None = None) -> pd.DataFrame:
	"""Run a read-only query and return a tidy DataFrame.

	This is the replacement for the old `query.sh` sqlcmd wrapper. sqlcmd's
	`-W -s,` output was still padded to the column's declared width (128 chars
	for sysname columns), which made every artifact in out/ unparseable. Going
	through pymssql sidesteps that entirely.
	"""
	with get_conn() as conn, warnings.catch_warnings():
		# pandas warns that only SQLAlchemy connectables are tested. pymssql
		# DBAPI2 works fine here and adding SQLAlchemy buys nothing for
		# read-only exploration.
		warnings.filterwarnings(
			"ignore",
			message="pandas only supports SQLAlchemy connectable",
			category=UserWarning,
		)
		return pd.read_sql(sql, conn, params=params)


def scalar(sql: str, params: tuple | None = None):
	"""Run a query expected to return exactly one value."""
	df = query(sql, params)
	if df.empty:
		return None
	return df.iloc[0, 0]


def write_derived(df: pd.DataFrame, name: str, *, index: bool = False) -> Path:
	"""Persist a small, reviewable artifact to data/derived/ (committed)."""
	DERIVED_DIR.mkdir(parents=True, exist_ok=True)
	path = DERIVED_DIR / name
	df.to_csv(path, index=index, encoding="utf-8")
	log.info("wrote %s (%d rows)", path.relative_to(PROJECT_ROOT), len(df))
	return path


def write_raw(df: pd.DataFrame, name: str, *, index: bool = False) -> Path:
	"""Persist a bulk extract to data/raw/ (gitignored — may contain PII)."""
	RAW_DIR.mkdir(parents=True, exist_ok=True)
	path = RAW_DIR / name
	if name.endswith(".parquet"):
		df.to_parquet(path, index=index)
	else:
		df.to_csv(path, index=index, encoding="utf-8")
	log.info("wrote %s (%d rows)", path.relative_to(PROJECT_ROOT), len(df))
	return path
