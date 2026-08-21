"""Shift Assignment records, and the FTE reconciliation that guards them.

`Shift Assignment` is per Employee per half-day. autoshift's model is atomic
AM/PM with at most one shift per employee per day, so a full-day agenda row
expands into two assignments.
"""

from __future__ import annotations

import logging

import pandas as pd

from .. import settings
from . import keys, location, presence

log = logging.getLogger(__name__)

#: Weeks of practice operation per year, for the FTE expectation.
#: threshold("weeks_per_year")

#: Ratio band outside which a person's workload is reported for review.
#: threshold("fte_tolerance")


def fte_reconciliation(
	person_level: pd.DataFrame,
	spine: pd.DataFrame,
	styles: pd.DataFrame,
	*,
	tolerance: float | None = None,
) -> pd.DataFrame:
	"""Compare emitted half-days against contracted FTE, per person.

	The expectation is **calibrated, not assumed**. A first attempt used
	`FTE% x 10 half-days/week` and reported everyone at ~0.4, because this
	practice runs half-day shifts of about seven hours: 100% FTE works out at
	roughly 6.6 half-days a week, not 10. The slope is therefore fitted on
	presence-marked staff — whose assignments are read directly from the agenda
	rather than derived — and applied to everyone else.

	This is the strongest available check on reconstruction. It caught two real
	bugs: background full-day absence markers suppressing 98% of one dentist's
	shifts, and orthodontists' parallel columns tripling theirs.
	"""
	prof = settings.get()
	tolerance = prof.threshold("fte_tolerance", 0.40) if tolerance is None else tolerance
	st = styles.rename(columns={"FK_Behandler": "behandler_id"})[["behandler_id", "style"]]
	df = person_level.merge(st, on="behandler_id", how="left")

	per = df.groupby("personnel_no").agg(half_days=("window", "size"))
	per["style"] = df.groupby("personnel_no")["style"].agg(
		lambda s: s.mode().iat[0] if len(s.mode()) else None
	)
	out = per.join(spine.set_index("personnel_no")[["surname", "service", "fte_pct"]]).dropna(
		subset=["fte_pct"]
	)
	if out.empty:
		return out.reset_index()

	calibration = out[out["style"] == "presence_marked"]
	if calibration.empty:
		calibration = out
	slope = (calibration["half_days"] / calibration["fte_pct"]).median()

	out["expected_half_days"] = (out["fte_pct"] * slope).round(0)
	out["ratio"] = (out["half_days"] / out["expected_half_days"]).round(2)
	out["within_tolerance"] = out["ratio"].between(1 - tolerance, 1 + tolerance)

	log.info(
		"FTE check: slope %.2f half-days/FTE-point (%.1f per week at 100%%), %d of %d people within +-%.0f%%",
		slope,
		slope * 100 / prof.threshold("weeks_per_year", 46),
		int(out["within_tolerance"].sum()),
		len(out),
		tolerance * 100,
	)
	return out.reset_index()


def build(person_level: pd.DataFrame, spine: pd.DataFrame) -> pd.DataFrame:
	"""Shape person-level half-days into Frappe Shift Assignment rows.

	`shift_location` names a (branch, discipline) pair, because that is what
	autoshift reads back off it. The branch is settled per half-day by
	`pipeline.location`; the discipline is the person's own, from the spine.
	"""
	if person_level.empty:
		return pd.DataFrame()

	df = person_level.copy()
	discipline = df["personnel_no"].map(spine.set_index("personnel_no")["department"])
	branch = df["branch"] if "branch" in df else pd.Series(location.default_branch(), index=df.index)
	shift_location = [location.location_name(b, d) for b, d in zip(branch, discipline, strict=False)]
	df["zawin_key"] = [
		keys.attested_key(z, w) if pd.notna(z) else keys.reconstructed_key(int(b), d, w)
		for z, b, d, w in zip(
			df["source_zaehler"], df["behandler_id"], df["date"], df["window"], strict=False
		)
	]

	out = pd.DataFrame(
		{
			"custom_zawin_key": df["zawin_key"],
			"employee": df["personnel_no"],
			"shift_type": df["window"].str.upper(),
			"start_date": df["date"],
			"end_date": df["date"],
			"shift_location": shift_location,
			"status": "Active",
			"docstatus": 1,
		}
	)
	out["company"] = settings.get().company

	unplaced = out["shift_location"].isna()
	if unplaced.any():
		log.warning(
			"%d assignments have no Shift Location (person has no department): %s",
			int(unplaced.sum()),
			", ".join(sorted(set(out.loc[unplaced, "employee"].astype(str)))[:10]),
		)

	dupes = out["custom_zawin_key"].duplicated().sum()
	if dupes:
		raise ValueError(f"{dupes} duplicate custom_zawin_key values — keys are not deterministic")

	return out.sort_values(["start_date", "employee", "shift_type"]).reset_index(drop=True)
