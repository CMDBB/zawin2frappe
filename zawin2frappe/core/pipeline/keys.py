"""Deterministic natural keys.

Frappe's Data Import inserts when a row has no `ID` column and updates when it
does. Stable keys let a re-run update rather than duplicate.

`Shift Assignment` autonames as a series, so it carries an explicit `zawin_key`
custom field instead (Data, unique, hidden — shipped as an autoshift fixture).
"""
from __future__ import annotations

import datetime as dt

#: Attested: the row exists in TAGPLANTERMIN and carries its primary key.
PREFIX_ATTESTED = "zt"
#: Reconstructed: derived from the practice day minus absences, so keyed on the
#: fields that identify the assignment rather than on a source row.
PREFIX_RECONSTRUCTED = "rc"


def attested_key(zaehler: int, window: str) -> str:
    """Key an assignment read from TAGPLANTERMIN.

    The window is part of the key, not decoration: a full-day agenda row
    expands into two atomic assignments, so the source primary key alone is not
    unique — omitting it collides on every full-day row.
    """
    return f"{PREFIX_ATTESTED}:{int(zaehler)}:{window}"


def reconstructed_key(behandler_id: int, date: dt.date | str, window: str) -> str:
    day = date.isoformat() if hasattr(date, "isoformat") else str(date)[:10]
    return f"{PREFIX_RECONSTRUCTED}:{int(behandler_id)}:{day}:{window}"


def is_reconstructed(key: str) -> bool:
    return str(key).startswith(PREFIX_RECONSTRUCTED + ":")
