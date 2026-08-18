"""The practice-day model.

Working windows are per agenda, not global. `TAGPLAN` records `Start`/`Ende`
in minutes since midnight and they genuinely differ — Ortho closes at 20:30,
Administration opens at 07:30, Prophylaxie runs 06:30-21:30 — so reconstructing
against a single window would be wrong for three disciplines.

Several `TAGPLAN` rows are not disciplines at all: `Occupation Cabinets` is a
room-occupancy agenda (`mitArbeitszeit` false), `10. Vacances` and
`11. Salle de conférence` are purpose agendas, `09. Zone HD` is a zone. They
carry no working time and are excluded.
"""
from __future__ import annotations

import logging

import pandas as pd

from .. import settings

log = logging.getLogger(__name__)

#: TAGPLAN ids that do not describe a person's working day.
_LEGACY_NON_DISCIPLINE = {
    6,   # Occupation Cabinets  — room occupancy
    12,  # 10. Vacances         — holiday marker agenda
    13,  # 11. Salle de conférence
    23,  # 09. Zone HD          — zone, not a discipline
}

#: Fallback window when an employee belongs to no discipline agenda.
#: 07:00-21:15, the bounds shared by most agendas.
#: fallback comes from the profile's practice_day


def agendas(query_fn) -> pd.DataFrame:
    """TAGPLAN rows with their working-day bounds."""
    df = query_fn(
        """
        SELECT [Zähler] AS agenda_id, Beschreibung AS label,
               Start AS day_start, Ende AS day_end,
               ZeitrasterMin AS grid_min, BehandlerArbeitstage AS has_workdays,
               mitArbeitszeit AS has_worktime, FK_BehOrt AS beh_ort_id
        FROM TAGPLAN ORDER BY [Zähler]
        """
    )
    prof = settings.get()
    df["is_discipline"] = (
        ~df["agenda_id"].isin(prof.non_discipline_agendas)
        & df["has_worktime"].astype(bool)
    )
    return df


def employee_agendas(query_fn) -> pd.DataFrame:
    """Which agendas each BEHANDLER column belongs to."""
    return query_fn(
        """
        SELECT tb.FK_Behandler AS behandler_id, tb.FK_TagPlan AS agenda_id,
               tb.Reihenfolge AS sort_order
        FROM TAGPLANBEHANDLER tb
        """
    )


def practice_days(query_fn) -> pd.DataFrame:
    """Per-column working window, from the employee's discipline agenda(s).

    Where a column belongs to several discipline agendas the union is taken —
    the widest plausible day — because reconstruction subtracts absences from
    it, and too narrow a window silently truncates real shifts.
    """
    ag = agendas(query_fn)
    link = employee_agendas(query_fn)

    disc = ag[ag["is_discipline"]]
    joined = link.merge(disc, on="agenda_id", how="inner")
    if joined.empty:
        return pd.DataFrame(columns=["behandler_id", "day_start", "day_end", "agendas"])

    out = (
        joined.groupby("behandler_id")
        .agg(
            day_start=("day_start", "min"),
            day_end=("day_end", "max"),
            agendas=("agenda_id", lambda s: ",".join(map(str, sorted(s)))),
        )
        .reset_index()
    )
    log.info(
        "practice day resolved for %d columns (%d discipline agendas)",
        len(out), len(disc),
    )
    return out


def window_for(practice: pd.DataFrame, behandler_id: int) -> tuple[int, int]:
    """Working window for one column, falling back to the shared default."""
    prof = settings.get()
    row = practice.loc[practice["behandler_id"] == behandler_id]
    if row.empty:
        return prof.day_start, prof.day_end
    return int(row.iloc[0]["day_start"]), int(row.iloc[0]["day_end"])


def open_days(annotated: pd.DataFrame) -> set:
    """Dates on which the practice operated at all.

    Derived rather than configured: a date with no agenda row anywhere is a
    closure. Used to stop reconstruction inventing shifts on closed days.
    """
    return set(pd.to_datetime(annotated["Datum"]).dt.date.unique())
