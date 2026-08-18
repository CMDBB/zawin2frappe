"""Per-column profiling.

Replaces gen_queries.py, which emitted SQL text to be pasted into sqlcmd. The
round-trip through sqlcmd produced 128-char-padded output that could not be
parsed back (see the retired out/samply_sample.txt). Here the queries run
in-process and return DataFrames.
"""
from __future__ import annotations

import logging

import pandas as pd

from .db import query
from .introspect import columns

log = logging.getLogger(__name__)

# Types where MIN/MAX is meaningful. String types included: lexical min/max
# still surfaces encoding damage and stray sentinel values.
COMPARABLE_TYPES = {
    "int", "bigint", "smallint", "tinyint", "decimal", "numeric",
    "float", "real", "money", "smallmoney",
    "date", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "time",
    "char", "varchar", "nchar", "nvarchar",
}

# Types that cannot be DISTINCT-ed or compared in T-SQL.
SKIP_TYPES = {
    "text", "ntext", "image", "xml", "varbinary", "binary",
    "geography", "geometry", "timestamp",
}


def _quote(identifier: str) -> str:
    """Bracket-quote an identifier. ZaWin uses umlauts in column names
    (Zähler, PATIENTARBEITSSTATUSEINTRÄGE), so this is not optional."""
    return "[" + identifier.replace("]", "]]") + "]"


def profile_column(table: str, column: str, dtype: str) -> dict:
    """Null counts, distinct count, and range for a single column."""
    t, c = _quote(table), _quote(column)
    dtype_l = dtype.strip().lower()

    row: dict = {"table": table, "column": column, "data_type": dtype}

    if dtype_l in SKIP_TYPES:
        row["note"] = f"skipped: {dtype} not comparable"
        return row

    agg = [
        f"COUNT(*) AS total",
        f"COUNT({c}) AS non_null",
        f"COUNT(DISTINCT {c}) AS distinct_vals",
    ]
    if dtype_l in COMPARABLE_TYPES:
        agg += [f"MIN({c}) AS min_val", f"MAX({c}) AS max_val"]

    df = query(f"SELECT {', '.join(agg)} FROM {t}")  # noqa: S608 - identifiers quoted
    row.update(df.iloc[0].to_dict())
    return row


def profile_table(table: str) -> pd.DataFrame:
    """Profile every column of a table."""
    cols = columns(table)
    if cols.empty:
        raise ValueError(f"table not found: {table}")

    rows = []
    for _, col in cols.iterrows():
        try:
            rows.append(profile_column(table, col["column_name"], col["data_type"]))
        except Exception as exc:  # one bad column must not sink the run
            log.warning("profiling %s.%s failed: %s", table, col["column_name"], exc)
            rows.append(
                {
                    "table": table,
                    "column": col["column_name"],
                    "data_type": col["data_type"],
                    "note": f"error: {exc}",
                }
            )
    return pd.DataFrame(rows)


def sample_values(table: str, column: str, n: int = 10) -> pd.DataFrame:
    """N distinct random values — the exploratory workhorse."""
    t, c = _quote(table), _quote(column)
    return query(
        f"SELECT DISTINCT TOP {int(n)} {c} FROM {t} "  # noqa: S608 - identifiers quoted
        f"WHERE {c} IS NOT NULL ORDER BY NEWID()"
    )


def value_counts(table: str, column: str, top: int = 30) -> pd.DataFrame:
    """Frequency distribution — the fastest way to spot an enum."""
    t, c = _quote(table), _quote(column)
    return query(
        f"SELECT TOP {int(top)} {c} AS value, COUNT(*) AS n "  # noqa: S608
        f"FROM {t} GROUP BY {c} ORDER BY COUNT(*) DESC"
    )
