"""Implicit foreign-key discovery.

ZaWin declares no foreign keys, so relationships must be inferred. The test is
value-set containment: if every distinct non-null value of a candidate column
appears in some table's primary key, that is strong evidence of a reference.

Sentinel values matter here. ZaWin uses 0 as "unset" rather than NULL in most
FK_* columns (FK_BehOrt is 0 for the overwhelming majority of TAGPLANTERMIN
rows), so 0 is excluded from the containment test by default — otherwise every
real relationship scores <1.0 and gets missed.
"""
from __future__ import annotations

import logging

import pandas as pd

from .db import query
from .introspect import columns, primary_keys

log = logging.getLogger(__name__)

INT_TYPES = {"int", "bigint", "smallint", "tinyint"}

# Values meaning "no reference" rather than "reference to row N".
SENTINELS = (0, -1)


def _quote(identifier: str) -> str:
    return "[" + identifier.replace("]", "]]") + "]"


def candidate_columns(tables: list[str] | None = None) -> pd.DataFrame:
    """Integer columns that look like references, by naming convention."""
    cols = columns()
    cols = cols[cols["data_type"].str.lower().isin(INT_TYPES)]
    looks_like_fk = (
        cols["column_name"].str.match(r"(?i)^fk[_]?")
        | cols["column_name"].str.contains(r"(?i)id$", na=False)
    )
    cols = cols[looks_like_fk]
    if tables:
        cols = cols[cols["table_name"].isin(tables)]
    return cols.reset_index(drop=True)


def containment(
    child_table: str,
    child_column: str,
    parent_table: str,
    parent_column: str,
    *,
    exclude_sentinels: bool = True,
) -> dict:
    """Fraction of distinct child values present in the parent key.

    Returns 1.0 for a perfect reference. Also reports how many rows were
    sentinel/null, since a column that is 99% zeros is a weak signal even at
    containment 1.0.
    """
    ct, cc = _quote(child_table), _quote(child_column)
    pt, pc = _quote(parent_table), _quote(parent_column)

    sentinel_clause = ""
    if exclude_sentinels:
        sentinel_clause = f" AND {cc} NOT IN ({', '.join(str(s) for s in SENTINELS)})"

    sql = f"""
        WITH child AS (
            SELECT DISTINCT {cc} AS v FROM {ct}
            WHERE {cc} IS NOT NULL{sentinel_clause}
        )
        SELECT
            (SELECT COUNT(*) FROM child) AS distinct_child,
            (SELECT COUNT(*) FROM child
              WHERE v IN (SELECT {pc} FROM {pt})) AS matched,
            (SELECT COUNT(*) FROM {ct}) AS child_rows,
            (SELECT COUNT(*) FROM {ct}
              WHERE {cc} IS NULL{
                  ' OR ' + cc + ' IN (' + ', '.join(str(s) for s in SENTINELS) + ')'
                  if exclude_sentinels else ''
              }) AS unset_rows
    """  # noqa: S608 - identifiers quoted
    r = query(sql).iloc[0]

    distinct_child = int(r["distinct_child"] or 0)
    matched = int(r["matched"] or 0)
    return {
        "child_table": child_table,
        "child_column": child_column,
        "parent_table": parent_table,
        "parent_column": parent_column,
        "distinct_child": distinct_child,
        "matched": matched,
        "containment": (matched / distinct_child) if distinct_child else None,
        "child_rows": int(r["child_rows"] or 0),
        "unset_rows": int(r["unset_rows"] or 0),
    }


def scan(
    child_tables: list[str],
    parent_tables: list[str] | None = None,
    *,
    min_containment: float = 0.95,
) -> pd.DataFrame:
    """Scan candidate columns against every single-column primary key.

    Restrict child_tables — a full 473-table cross product is not worth the
    query time when the shortlist is already known.
    """
    pks = primary_keys()
    # single-column integer PKs only
    pk_counts = pks.groupby("table_name").size()
    single = pk_counts[pk_counts == 1].index
    pks = pks[pks["table_name"].isin(single)]
    if parent_tables:
        pks = pks[pks["table_name"].isin(parent_tables)]

    cands = candidate_columns(child_tables)
    log.info(
        "scanning %d candidate columns against %d primary keys",
        len(cands), len(pks),
    )

    results = []
    for _, child in cands.iterrows():
        for _, parent in pks.iterrows():
            if child["table_name"] == parent["table_name"]:
                continue
            try:
                res = containment(
                    child["table_name"], child["column_name"],
                    parent["table_name"], parent["column_name"],
                )
            except Exception as exc:
                log.debug(
                    "containment %s.%s -> %s failed: %s",
                    child["table_name"], child["column_name"],
                    parent["table_name"], exc,
                )
                continue
            if res["containment"] is not None and res["containment"] >= min_containment:
                results.append(res)

    df = pd.DataFrame(results)
    if df.empty:
        return df
    return df.sort_values(
        ["child_table", "child_column", "containment"], ascending=[True, True, False]
    ).reset_index(drop=True)
