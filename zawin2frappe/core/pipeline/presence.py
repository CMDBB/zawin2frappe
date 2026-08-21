"""Deriving who worked when.

The practice keeps agendas two opposite ways (findings.md §3b), so a single
rule loses about half the workforce:

    presence-marked   the agenda records when the person IS there (PRES /
                      RECEPTION / ADMIN blocks). Reception, admin, assistants —
                      people with no patient bookings to imply presence.
    absence-marked    the agenda records only when the person is AWAY. Working
                      time is the practice day minus those blocks, and the
                      patient bookings fill the gaps. Clinical staff.

Reading only presence rows — the obvious approach — silently drops every
clinical employee, i.e. exactly the staff the optimiser most needs.
"""

from __future__ import annotations

import hashlib
import logging

import pandas as pd

from .. import settings, shifts
from . import calendar, location

log = logging.getLogger(__name__)

#: Kinds that are real assignments. `transition` and `meeting` are almost
#: entirely sub-120-minute markers (chair changeovers), not shifts — they still
#: count as corroboration, just not as output.
ASSIGNABLE_KINDS = frozenset({"present", "reception", "admin", "remote", "on_call", "training"})

#: Kinds that attest presence without being assignments themselves.
CORROBORATING_KINDS = ASSIGNABLE_KINDS | {"transition", "meeting", "break", "do_not_book"}

WINDOWS = ("am", "pm")

DERIVATION_ATTESTED = "attested"
DERIVATION_RECONSTRUCTED = "reconstructed"


def _windows_of(row_window: str) -> tuple[str, ...]:
	"""Expand a shift_window into the atomic AM/PM windows it covers."""
	if row_window == "full_day":
		return WINDOWS
	if row_window in WINDOWS:
		return (row_window,)
	return ()


def attested(annotated: pd.DataFrame) -> pd.DataFrame:
	"""Assignments read directly from the agenda.

	Applies to presence-marked staff, and to any presence row belonging to an
	absence-marked one — an explicit row always beats a derived one.
	"""
	rows = annotated[annotated["shift_kind"].isin(ASSIGNABLE_KINDS) & ~annotated["is_absence"]]
	# The site a row was worked at is a property of that row, so it is read here
	# while the source row is still in hand; see pipeline.location.
	branches = location.row_branches(rows)
	out = []
	for r in rows.itertuples():
		for window in _windows_of(r.shift_window):
			out.append(
				{
					"behandler_id": r.FK_Behandler,
					"date": pd.Timestamp(r.Datum).date(),
					"window": window,
					"derivation": DERIVATION_ATTESTED,
					"source_zaehler": getattr(r, "Zähler", None),
					"shift_kind": r.shift_kind,
					"von_zeit": r.VonZeit,
					"bis_zeit": r.BisZeit,
					"branch": branches.get(r.Index, pd.NA),
				}
			)
	return pd.DataFrame(out)


def absence_windows(annotated: pd.DataFrame) -> set[tuple]:
	"""(column, date, window) tuples the person is explicitly away for."""
	away = annotated[annotated["shift_kind"] == "absence"]
	out = set()
	for r in away.itertuples():
		for window in _windows_of(r.shift_window):
			out.add((r.FK_Behandler, pd.Timestamp(r.Datum).date(), window))
	return out


def corroboration(annotated: pd.DataFrame, patient: pd.DataFrame) -> tuple[set[tuple], set[tuple]]:
	"""Evidence of presence, split by strength.

	Returns `(hard, soft)`:

	  hard  patient appointments in that half-day, and explicit presence rows.
	        Both are direct assertions that the person was working.
	  soft  weak markers — transitions, breaks, do-not-book blocks. Enough to
	        corroborate, not enough to overrule an absence.

	The split matters because absence markers are unreliable at full-day
	granularity: a column can carry more `full_day` absence rows than the
	period has dates, which makes them background banners rather than true
	absences. Letting those veto days with real patient bookings can suppress
	almost all of a clinician's shifts.
	Only counts are read from the patient side — never identity.
	"""
	hard: set[tuple] = set()
	soft: set[tuple] = set()

	if not patient.empty:
		for r in patient.itertuples():
			day = pd.Timestamp(r.Datum).date()
			if r.am_appts > 0:
				hard.add((r.FK_Behandler, day, "am"))
			if r.pm_appts > 0:
				hard.add((r.FK_Behandler, day, "pm"))

	for r in annotated[annotated["shift_kind"].isin(CORROBORATING_KINDS)].itertuples():
		day = pd.Timestamp(r.Datum).date()
		target = hard if r.shift_kind in ASSIGNABLE_KINDS else soft
		for window in _windows_of(r.shift_window) or WINDOWS:
			target.add((r.FK_Behandler, day, window))

	return hard, soft


def reconstruct(
	annotated: pd.DataFrame,
	patient: pd.DataFrame,
	styles: pd.DataFrame,
	practice: pd.DataFrame,
) -> pd.DataFrame:
	"""Corroborated reconstruction for absence-marked columns.

	A window is emitted only when corroborated AND not absorbed by an absence.
	The absence-complement extends a corroborated day to the full practice
	window; it never creates a day on its own.
	"""
	absence_marked = set(styles.loc[styles["style"] == shifts.STYLE_ABSENCE, "FK_Behandler"])
	if not absence_marked:
		return pd.DataFrame(columns=["behandler_id", "date", "window", "derivation", "shift_kind", "branch"])

	away = absence_windows(annotated)
	hard, soft = corroboration(annotated, patient)
	open_dates = calendar.open_days(annotated)

	out = []
	for behandler_id, day, window in sorted(hard | soft):
		if behandler_id not in absence_marked or day not in open_dates:
			continue
		key = (behandler_id, day, window)
		# Hard evidence outranks an absence marker; soft evidence does not.
		if key in away and key not in hard:
			continue
		out.append(
			{
				"behandler_id": behandler_id,
				"date": day,
				"window": window,
				"derivation": DERIVATION_RECONSTRUCTED,
				"source_zaehler": None,
				"shift_kind": "reconstructed",
				"von_zeit": None,
				"bis_zeit": None,
				# No source row to carry a site tag; pipeline.location fills this
				# in from the column the shift was worked in.
				"branch": pd.NA,
			}
		)
	df = pd.DataFrame(out)
	log.info(
		"reconstructed %d half-days for %d absence-marked columns",
		len(df),
		len(absence_marked),
	)
	return df


def build(
	annotated: pd.DataFrame,
	patient: pd.DataFrame,
	practice: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
	"""All assignments, plus the per-column bookkeeping styles used.

	Attested rows win over reconstructed ones for the same (column, date,
	window) — an explicit agenda entry is better evidence than a derivation.
	"""
	styles = shifts.classify_style(annotated)

	att = attested(annotated)
	rec = reconstruct(annotated, patient, styles, practice)

	combined = pd.concat([att, rec], ignore_index=True) if not rec.empty else att
	if combined.empty:
		return combined, styles

	# A site signal on any of the tied rows settles the half-day before the
	# dedup below picks a survivor on grounds that have nothing to do with site.
	combined = location.promote(combined, ["behandler_id", "date", "window"])

	# Stable sort with an explicit tiebreaker: quicksort is not stable, so
	# without this the winner among tied rows varies between runs and the
	# output stops being byte-identical.
	combined["_rank"] = combined["derivation"].map({DERIVATION_ATTESTED: 0, DERIVATION_RECONSTRUCTED: 1})
	combined = combined.sort_values(
		["behandler_id", "date", "window", "_rank", "source_zaehler"],
		kind="stable",
		na_position="last",
	)
	combined = (
		combined.drop_duplicates(subset=["behandler_id", "date", "window"], keep="first")
		.drop(columns="_rank")
		.reset_index(drop=True)
	)

	log.info(
		"assignments: %d total (%s)",
		len(combined),
		", ".join(f"{k}={v}" for k, v in combined["derivation"].value_counts().items()),
	)
	return combined, styles


def to_person_level(assignments: pd.DataFrame, columns: pd.DataFrame) -> pd.DataFrame:
	"""Collapse column-level assignments onto people.

	Necessary because `FK_Behandler` is an agenda column, not a person, and
	orthodontists hold two or three parallel columns — one per chair they
	supervise. Summing those would triple their shifts: before this collapse
	the three ortho staff scored ~3.0x their contracted FTE, which is exactly
	their column count.

	A person working two columns in the same half-day is still one shift. The
	parallel columns are room capacity, and autoshift models that separately
	via `max_rooms_for_employee_type`.
	"""
	link = columns.dropna(subset=["personnel_no"])[["behandler_id", "personnel_no"]]
	out = assignments.merge(link, on="behandler_id", how="left")

	orphans = int(out["personnel_no"].isna().sum())
	if orphans:
		log.info(
			"%d assignments belong to columns with no roster match (historical "
			"staff); dropped from the import",
			orphans,
		)
	out = out.dropna(subset=["personnel_no"])

	# Same-day site evidence survives the collapse across a person's columns: an
	# ortho parallel column or a branch column can be the only one that names it.
	out = location.promote(out, ["personnel_no", "date", "window"])

	# Attested beats reconstructed when the same half-day arrives from several
	# columns, and a real source row is worth keeping over a derivation.
	out["_rank"] = out["derivation"].map({DERIVATION_ATTESTED: 0, DERIVATION_RECONSTRUCTED: 1})
	out = out.sort_values(
		["personnel_no", "date", "window", "_rank", "behandler_id"],
		kind="stable",
		na_position="last",
	)
	before = len(out)
	out = out.drop_duplicates(subset=["personnel_no", "date", "window"], keep="first").drop(columns="_rank")
	log.info(
		"person-level: %d assignments (%d collapsed from parallel columns)",
		len(out),
		before - len(out),
	)
	return out.reset_index(drop=True)


#: A row spanning at least this many minutes is an all-day banner ("in today"),
#: not a shift — its clock times say nothing about which half was worked.
#: threshold("banner_minutes")

#: Minutes by which one half must exceed the other for the span to decide it.
#: threshold("dominance_margin")


def _dominant_half(df: pd.DataFrame) -> list:
	"""Which half of the day each source row actually occupies.

	Returns "am" / "pm" / None per row; None where the row is an all-day banner
	or the two halves are within DOMINANCE_MARGIN of each other.
	"""
	prof = settings.get()
	out = []
	for von, bis in zip(df.get("von_zeit"), df.get("bis_zeit"), strict=False):
		if von is None or bis is None or pd.isna(von) or pd.isna(bis):
			out.append(None)
			continue
		von, bis = int(von), int(bis)
		if bis - von >= prof.threshold("banner_minutes", 780):
			out.append(None)
			continue
		am = max(0, min(bis, prof.midday) - max(von, prof.day_start))
		pm = max(0, min(bis, prof.day_end) - max(von, prof.midday))
		if abs(am - pm) < prof.threshold("dominance_margin", 30):
			out.append(None)
		else:
			out.append("am" if am > pm else "pm")
	return out


def collapse_daily(person_level: pd.DataFrame, patient: pd.DataFrame, columns: pd.DataFrame) -> pd.DataFrame:
	"""Reduce to at most one shift per person per day.

	The practice contracts 100% FTE as **five** seven-hour shifts a week, plus
	one slightly longer shift whose extra time is left unaccounted as admin and
	prep. So a person works one shift on a working day, never two.

	Splitting full-day agenda rows into an AM and a PM assignment therefore
	over-counted badly: it produced 6.35 shifts per week at 100% FTE against a
	contractual 5.00. Collapsing to one brings it to 4.93 — within 1.4%.

	The full-day rows were never two shifts. 15,201 of them span exactly
	420-1275, which is the practice-day bound from TAGPLAN, and another 1,688
	span midnight to midnight: they are banners meaning "in today", not
	14-hour shifts. Spans of 8-9 hours (07:00-16:30, 08:15-16:45) are the
	contract's single longer shift, not two halves either.

	This also matches autoshift's own model ("one shift per employee per day
	maximum") and stock HRMS, which rejects a second assignment for the same
	employee and date — so no HR Settings change is needed.

	Which half survives is decided by evidence, not alphabetical order:
	patient appointments first, then whether the row was attested.
	"""
	if person_level.empty:
		return person_level

	df = person_level.copy()
	df["date"] = pd.to_datetime(df["date"]).dt.date

	# Patient appointment counts per column/day, mapped onto people.
	pref: dict[tuple, str] = {}
	if not patient.empty:
		link = columns.dropna(subset=["personnel_no"])[["behandler_id", "personnel_no"]]
		pa = patient.merge(link, left_on="FK_Behandler", right_on="behandler_id", how="inner")
		pa["date"] = pd.to_datetime(pa["Datum"]).dt.date
		agg = pa.groupby(["personnel_no", "date"])[["am_appts", "pm_appts"]].sum()
		for (pno, day), row in agg.iterrows():
			if row["am_appts"] != row["pm_appts"]:
				pref[(pno, day)] = "am" if row["am_appts"] > row["pm_appts"] else "pm"

	# Days where only one window survived need no decision.
	counts = df.groupby(["personnel_no", "date"])["window"].transform("nunique")
	unambiguous = df[counts == 1]

	# Each person's own AM share, measured on the days that needed no guess.
	# A global AM default skewed the result 3:1 (9,697 AM against 3,408 PM) on
	# 4,352 arbitrary calls; propensity keeps each person's observed mix.
	share = unambiguous.assign(is_am=unambiguous["window"].eq("am")).groupby("personnel_no")["is_am"].mean()
	global_share = float(unambiguous["window"].eq("am").mean()) if len(unambiguous) else 0.5

	def _rank(pno, day, window):
		"""Stable pseudo-random rank in [0,1) for one person-day."""
		h = hashlib.blake2b(f"{pno}|{day}".encode(), digest_size=8).digest()
		return int.from_bytes(h, "big") / 2**64

	# Preference order:
	#   1. patient appointments in that half-day
	#   2. which half the source row actually occupies, by clock time
	#   3. the employee's own AM share, assigned deterministically
	#
	# Step 2 does most of the work and was originally missed. Of 4,352 days with
	# no patient evidence, only 186 are true all-day banners; 3,756 carry a real
	# span such as 08:15-16:45 or 11:30-21:15 where one half plainly dominates.
	# Guessing those from propensity threw away the clock times already in hand.
	dominant = _dominant_half(df)
	prefs = []
	for idx, (pno, day, window) in enumerate(zip(df["personnel_no"], df["date"], df["window"], strict=False)):
		decided = pref.get((pno, day)) or dominant[idx]
		if decided is not None:
			prefs.append(0 if decided == window else 1)
			continue
		p_am = share.get(pno, global_share)
		want_am = _rank(pno, day, window) < p_am
		prefs.append(0 if (window == "am") == want_am else 1)
	df["_pref"] = prefs
	df["_att"] = (df["derivation"] != DERIVATION_ATTESTED).astype(int)
	df["_am"] = (df["window"] != "am").astype(int)

	before = len(df)
	df = (
		df.sort_values(["personnel_no", "date", "_pref", "_att", "_am"], kind="stable")
		.drop_duplicates(subset=["personnel_no", "date"], keep="first")
		.drop(columns=["_pref", "_att", "_am"])
		.reset_index(drop=True)
	)
	log.info(
		"daily collapse: %d -> %d shifts (%d second-halves dropped); window mix %s",
		before,
		len(df),
		before - len(df),
		df["window"].value_counts().to_dict(),
	)
	return df
