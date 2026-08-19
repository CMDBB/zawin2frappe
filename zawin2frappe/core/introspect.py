"""Schema introspection for ZaWin.

The DB declares no foreign keys at all (verified: sys.foreign_keys returns
0 rows), so introspection here is limited to what the catalog *does* know —
tables, row counts, columns, primary keys, indexes. Relationship inference
lives in fkscan.py.
"""

from __future__ import annotations

import re

import pandas as pd

from .db import query

# Domain vocabulary for shortlisting agenda/HR-relevant tables.
#
# The original shell pipeline applied a `grep -ivE 'log|audit|config|lookup|
# history|temp|backup|sys|status'` noise filter BEFORE this domain filter,
# which silently dropped ZUTRITT/ZUTRITTLOG (818k / 2.4M rows) and every
# *LOG table from the shortlist. We do not pre-filter; noise is ranked down,
# never removed.
DOMAIN_TERMS = [
	# original vocabulary
	"termin",
	"mitarbeiter",
	"personal",
	"arzt",
	"behandler",
	"zimmer",
	"raum",
	"plan",
	"kalender",
	"patient",
	"filiale",
	"standort",
	"buchung",
	"agenda",
	# added: HR / working-time vocabulary the original run missed
	"schicht",
	"dienst",
	"arbeit",
	"zeit",
	"präsenz",
	"praesenz",
	"anwes",
	"abwes",
	"ferien",
	"urlaub",
	"stunde",
	"einsatz",
	"verfüg",
	"verfueg",
	"woche",
	"pause",
	"profil",
	"person",
	"mitarb",
	"team",
	"gruppe",
	"zutritt",
	"praxis",
	"beh",
]

_DOMAIN_RE = re.compile("|".join(DOMAIN_TERMS), re.IGNORECASE)


def tables() -> pd.DataFrame:
	"""All base tables with row counts, descending."""
	return query(
		"""
        SELECT t.name AS table_name, SUM(p.rows) AS rows
        FROM sys.tables t
        JOIN sys.partitions p
          ON t.object_id = p.object_id AND p.index_id IN (0, 1)
        GROUP BY t.name
        ORDER BY SUM(p.rows) DESC
        """
	)


def candidate_tables(min_rows: int = 0) -> pd.DataFrame:
	"""Tables whose name matches the domain vocabulary, with row counts."""
	df = tables()
	df = df[df["table_name"].str.contains(_DOMAIN_RE, na=False)]
	if min_rows:
		df = df[df["rows"] >= min_rows]
	return df.reset_index(drop=True)


def columns(table: str | None = None) -> pd.DataFrame:
	"""Column metadata for one table, or the whole database."""
	sql = """
        SELECT TABLE_NAME AS table_name,
               ORDINAL_POSITION AS position,
               COLUMN_NAME AS column_name,
               DATA_TYPE AS data_type,
               CHARACTER_MAXIMUM_LENGTH AS max_length,
               IS_NULLABLE AS is_nullable
        FROM INFORMATION_SCHEMA.COLUMNS
    """
	if table:
		sql += " WHERE TABLE_NAME = %s"
		sql += " ORDER BY TABLE_NAME, ORDINAL_POSITION"
		return query(sql, (table,))
	sql += " ORDER BY TABLE_NAME, ORDINAL_POSITION"
	return query(sql)


def primary_keys() -> pd.DataFrame:
	"""Primary key columns for every table that declares one."""
	return query(
		"""
        SELECT tc.TABLE_NAME AS table_name,
               kcu.COLUMN_NAME AS column_name,
               tc.CONSTRAINT_NAME AS constraint_name
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
          ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
        WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
        ORDER BY tc.TABLE_NAME, kcu.ORDINAL_POSITION
        """
	)


def declared_foreign_keys() -> pd.DataFrame:
	"""Declared FKs. Expected to be empty for ZaWin — kept so the claim stays
	verifiable rather than folklore."""
	return query(
		"""
        SELECT fk.name AS fk_name,
               tp.name AS parent_table, cp.name AS parent_column,
               tr.name AS referenced_table, cr.name AS referenced_column
        FROM sys.foreign_keys fk
        JOIN sys.foreign_key_columns fkc
          ON fk.object_id = fkc.constraint_object_id
        JOIN sys.tables tp ON fkc.parent_object_id = tp.object_id
        JOIN sys.columns cp
          ON fkc.parent_object_id = cp.object_id
         AND fkc.parent_column_id = cp.column_id
        JOIN sys.tables tr ON fkc.referenced_object_id = tr.object_id
        JOIN sys.columns cr
          ON fkc.referenced_object_id = cr.object_id
         AND fkc.referenced_column_id = cr.column_id
        ORDER BY tp.name
        """
	)
