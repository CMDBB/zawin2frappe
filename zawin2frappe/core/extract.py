"""Agenda and employee extracts.

The agenda ground truth is TAGPLANTERMIN (+ TAGPLANTERMINARCHIV for pre-2018).
Shift assignment at this practice is done manually, one calendar entry at a
time, so the schedule is not encoded in the rule tables (BEHANDLERPAUSE,
TAGPLANPROFIL, TAGPLANPAUSE) — those are effectively unused. It lives in the
490,922 patient-less rows of TAGPLANTERMIN.

Patient columns are deliberately never selected: client data is out of scope.
"""

from __future__ import annotations

import pandas as pd

from . import settings
from .db import query

#: Columns safe to extract — no patient identifiers, no free-text that could
#: carry clinical detail (Bemerkung/OABemerkung/Dokumente are excluded).
AGENDA_COLUMNS = [
	"Zähler",
	"FK_Behandler",
	"FK_BehOrt",
	"FK_OAPraxis",
	"Datum",
	"VonZeit",
	"BisZeit",
	"FK_TagPlanTerminKategorie",
	"Status",
	"Abgesagt",
	"Farbe",
	"Beschreibung",
	"Herkunft",
	"ErstelltDatum",
	"MutDatum",
	"MutBehandler",
]

#: Category names are whatever the practice typed into TAGPLANTERMINKATEGORIE,
#: so which of them mean "absent" and which mean "present but not chairside"
#: is profile data, not a constant.


def categories() -> pd.DataFrame:
	"""The appointment-category lookup, incl. inactive ones."""
	return query(
		"""
        SELECT [Zähler] AS category_id,
               Bezeichnung_1 AS label,
               Sortierung AS sort_order,
               Farbe AS colour,
               Shell AS shell_code,
               inaktiv AS inactive
        FROM TAGPLANTERMINKATEGORIE
        ORDER BY Sortierung
        """
	)


def employees(active_only: bool = False) -> pd.DataFrame:
	"""BEHANDLER as an HR record.

	The name fields are dirty in a specific way: `Name` sometimes holds the
	whole name with `Vorname` NULL, and sometimes holds the person's initials
	with the real name in `Vorname`. Callers must normalise; see crosswalk.py.
	"""
	sql = """
        SELECT [Zähler] AS behandler_id,
               Name AS name_raw,
               Vorname AS vorname_raw,
               Initialen AS initials,
               Funktion AS funktion_code,
               Gruppe AS is_group,
               Arbeitstage AS arbeitstage,
               Eintritts_Datum AS joined_on,
               Austritts_Datum AS left_on,
               FK_BehOrt AS beh_ort_id,
               FK_Praxis AS praxis_id,
               EMail AS email,
               Reihenfolge AS sort_order
        FROM BEHANDLER
    """
	if active_only:
		sql += " WHERE Austritts_Datum IS NULL"
	sql += " ORDER BY [Zähler]"
	return query(sql)


def agenda(
	date_from: str | None = None,
	date_to: str | None = None,
	*,
	include_archive: bool = False,
	patient_rows: bool = False,
) -> pd.DataFrame:
	"""Agenda entries, patient-less by default.

	Args:
	    date_from/date_to: ISO dates, inclusive.
	    include_archive: union TAGPLANTERMINARCHIV (2009-01-05 .. 2017-12-31).
	        TAGPLANTERMIN itself covers 2018-01-01 .. 2028-12-31.
	    patient_rows: if False (default) restrict to rows with no patient,
	        i.e. the staff-agenda half of the table.
	"""
	cols = ", ".join(f"[{c}]" for c in AGENDA_COLUMNS)

	def _one(table: str) -> str:
		where = []
		if not patient_rows:
			where.append("(FK_Patient IS NULL OR FK_Patient = 0)")
		if date_from:
			where.append("Datum >= %(date_from)s")
		if date_to:
			where.append("Datum <= %(date_to)s")
		clause = (" WHERE " + " AND ".join(where)) if where else ""
		return f"SELECT {cols}, '{table}' AS source FROM {table}{clause}"

	sql = _one("TAGPLANTERMIN")
	if include_archive:
		sql += "\nUNION ALL\n" + _one("TAGPLANTERMINARCHIV")

	params = {}
	if date_from:
		params["date_from"] = date_from
	if date_to:
		params["date_to"] = date_to

	df = query(sql, params or None)
	return df.sort_values(["Datum", "FK_Behandler", "VonZeit"]).reset_index(drop=True)


def agenda_with_labels(**kwargs) -> pd.DataFrame:
	"""agenda() joined to category labels and employee initials, with
	VonZeit/BisZeit decoded to clock times and the free-text Beschreibung
	normalised into the shift taxonomy."""
	from . import shifts

	df = agenda(**kwargs)
	cats = categories().set_index("category_id")["label"]
	emps = employees().set_index("behandler_id")["initials"]

	df["category"] = df["FK_TagPlanTerminKategorie"].map(cats)
	df["initials"] = df["FK_Behandler"].map(emps)
	df["start_time"] = df["VonZeit"].map(minutes_to_clock)
	df["end_time"] = df["BisZeit"].map(minutes_to_clock)
	df["duration_min"] = df["BisZeit"] - df["VonZeit"]
	df["weekday"] = pd.to_datetime(df["Datum"]).dt.day_name()
	prof = settings.get()
	df["is_absence"] = df["category"].isin(prof.absence_categories)
	df["is_non_clinical"] = df["category"].isin(prof.non_clinical_categories)
	df = shifts.annotate(df)
	# A categorised absence outranks whatever was typed into Beschreibung.
	df.loc[df["is_absence"], ["shift_kind", "counts_as_worked"]] = ["absence", False]
	return df


def minutes_to_clock(value) -> str | None:
	"""Decode a VonZeit/BisZeit smallint to HH:MM.

	Encoding is minutes since midnight — see docs/schema-map.md for the
	evidence. Values outside a plausible day are passed through as None so
	they surface as nulls rather than silently wrapping.
	"""
	if value is None or pd.isna(value):
		return None
	v = int(value)
	if not 0 <= v <= 24 * 60:
		return None
	return f"{v // 60:02d}:{v % 60:02d}"


def patient_activity(
	date_from: str | None = None,
	date_to: str | None = None,
	*,
	include_archive: bool = False,
) -> pd.DataFrame:
	"""Per (column, date) patient-appointment counts, split AM/PM.

	Used purely as corroboration for presence reconstruction: it answers "was
	this person seeing patients that half-day?" and nothing else.

	**No patient data leaves the database here** — only COUNT and the earliest
	and latest appointment minute per column per day. No identifier, no name,
	no free text. This is what keeps reconstruction from having to touch the
	patient half of TAGPLANTERMIN.
	"""
	midday = settings.get().midday

	def _one(table: str) -> str:
		where = ["FK_Patient > 0"]
		if date_from:
			where.append("Datum >= %(date_from)s")
		if date_to:
			where.append("Datum <= %(date_to)s")
		return f"""
            SELECT FK_Behandler, Datum,
                   SUM(CASE WHEN VonZeit <  {midday} THEN 1 ELSE 0 END) AS am_appts,
                   SUM(CASE WHEN VonZeit >= {midday} THEN 1 ELSE 0 END) AS pm_appts,
                   MIN(VonZeit) AS first_appt, MAX(BisZeit) AS last_appt
            FROM {table}
            WHERE {" AND ".join(where)}
            GROUP BY FK_Behandler, Datum
        """

	sql = _one("TAGPLANTERMIN")
	if include_archive:
		sql += "\nUNION ALL\n" + _one("TAGPLANTERMINARCHIV")

	params = {}
	if date_from:
		params["date_from"] = date_from
	if date_to:
		params["date_to"] = date_to

	df = query(sql, params or None)
	if include_archive and not df.empty:
		df = df.groupby(["FK_Behandler", "Datum"], as_index=False).agg(
			am_appts=("am_appts", "sum"),
			pm_appts=("pm_appts", "sum"),
			first_appt=("first_appt", "min"),
			last_appt=("last_appt", "max"),
		)
	return df
