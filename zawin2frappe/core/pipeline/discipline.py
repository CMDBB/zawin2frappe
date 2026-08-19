"""Splitting the assistant pool into orthodontics and omnipractice.

Accounting files all 31 assistants under service 500 (`Assist`) and does not
record which discipline they work in. autoshift needs the split, because its
room-staffing ratios are per discipline.

ZaWin recovers it from the free-text agenda label, where ortho staff are marked
`PRES ORTHO` and variants. Measured over 2024 as each employee's ortho share of
their presence rows, the distribution is sharply bimodal:

    0%        41 employees
    0-5%       5
    5-25%      2
    25-75%     0     <- empty band
    75-100%    9

So any threshold in 25-75% separates them; 50% is used. The 5-25% pair are
occasional ortho cover, not ortho staff.
"""

from __future__ import annotations

import logging

import pandas as pd

from .. import settings

log = logging.getLogger(__name__)

#: Ortho share above which an employee is treated as ortho-side.
#: threshold("ortho")

#: Below this many presence rows the share is too noisy to trust.
#: threshold("min_presence_rows")

#: Shift kinds that positively place someone at work in a discipline.
PRESENCE_KINDS = ("present", "reception", "admin")


def ortho_share(annotated: pd.DataFrame) -> pd.DataFrame:
	"""Per-column ortho share of presence rows.

	`annotated` is an `extract.agenda_with_labels()` frame. Returns one row per
	`FK_Behandler` with the counts and share, plus `is_ortho` where the sample
	is large enough to judge.
	"""
	pres = annotated[annotated["shift_kind"].isin(PRESENCE_KINDS)].copy()
	if pres.empty:
		return pd.DataFrame(
			columns=["FK_Behandler", "presence_rows", "ortho_rows", "ortho_share", "is_ortho"]
		)

	pres["is_ortho_row"] = pres["label_clean"].str.contains("ortho", case=False, na=False)
	out = (
		pres.groupby("FK_Behandler")
		.agg(presence_rows=("is_ortho_row", "size"), ortho_rows=("is_ortho_row", "sum"))
		.reset_index()
	)
	out["ortho_share"] = out["ortho_rows"] / out["presence_rows"]
	out["is_ortho"] = pd.NA
	big = out["presence_rows"] >= settings.get().threshold("min_presence_rows", 20)
	out.loc[big, "is_ortho"] = out.loc[big, "ortho_share"] >= settings.get().threshold("ortho", 0.50)

	return out


#: Windows tried in turn, in months back from the most recent agenda date.
#: None means "everything". Wide first, because more rows make a steadier
#: estimate; narrower only for the columns that need it.
NARROWING_MONTHS = (None, 36, 24, 18, 12, 9, 6)

#: Never narrower than this, however ambiguous the column remains — below it
#: there is too little evidence to prefer over the wider reading.
#: Overridable as threshold("signal_floor_months").
DEFAULT_FLOOR_MONTHS = 6

#: A share inside this band describes nobody: the distribution is bimodal, so
#: landing in the middle means the window is averaging over a change of role.
#: Overridable as threshold("ambiguous_low"/"ambiguous_high").
DEFAULT_AMBIGUOUS = (0.25, 0.75)


def _window(annotated: pd.DataFrame, months: int | None) -> pd.DataFrame:
	"""The trailing `months` of the frame, anchored to its own latest date.

	Anchored to the data rather than to today so the result does not change
	from one day to the next.
	"""
	if months is None:
		return annotated
	dates = pd.to_datetime(annotated["Datum"])
	cutoff = dates.max() - pd.DateOffset(months=months)
	return annotated[dates >= cutoff]


def resolve_ortho(annotated: pd.DataFrame) -> pd.DataFrame:
	"""Per-column ortho share, narrowing the window only where it is ambiguous.

	A wide window is the better estimator for anyone whose role has been
	steady — more rows, less noise. It is the *wrong* estimator for someone who
	changed discipline partway through, because the average then describes
	neither era: one apprentice reads 49% over all history while sitting at
	89-99% in every individual year since 2022.

	So: measure wide, then re-measure just the ambiguous columns over
	progressively shorter recent windows until they resolve or the floor is
	reached. A column that never resolves keeps its widest reading and is
	reported, not guessed at.

	Adds `window_months` (None = all history) and `resolved` so every
	classification says which evidence produced it.
	"""
	prof = settings.get()
	low = prof.threshold("ambiguous_low", DEFAULT_AMBIGUOUS[0])
	high = prof.threshold("ambiguous_high", DEFAULT_AMBIGUOUS[1])
	floor = prof.threshold("signal_floor_months", DEFAULT_FLOOR_MONTHS)
	min_rows = prof.threshold("min_presence_rows", 20)

	base = ortho_share(annotated)
	base["window_months"] = None
	base["resolved"] = ~base["ortho_share"].between(low, high, inclusive="neither")

	# Only chase columns that have enough rows to be judged at all. A column
	# below the minimum is not *ambiguous*, it is simply unmeasured, and
	# narrowing the window can only make that worse.
	judgeable = base["presence_rows"] >= min_rows
	base.loc[~judgeable, "resolved"] = False
	unresolved = set(base.loc[~base["resolved"] & judgeable, "FK_Behandler"])
	if not unresolved:
		return base

	for months in NARROWING_MONTHS[1:]:
		if not unresolved or months < floor:
			break
		narrowed = ortho_share(_window(annotated, months))
		narrowed = narrowed[narrowed["FK_Behandler"].isin(unresolved)]
		for row in narrowed.itertuples():
			if row.presence_rows < min_rows:
				continue  # too little evidence at this width; keep looking wider
			settled = not (low < row.ortho_share < high)
			mask = base["FK_Behandler"] == row.FK_Behandler
			base.loc[mask, ["ortho_share", "presence_rows", "ortho_rows"]] = [
				row.ortho_share,
				row.presence_rows,
				row.ortho_rows,
			]
			base.loc[mask, "window_months"] = months
			base.loc[mask, "resolved"] = settled
			if settled:
				unresolved.discard(row.FK_Behandler)

	narrowed_count = int(base["window_months"].notna().sum())
	if narrowed_count:
		log.info(
			"ortho signal: %d column(s) needed a narrower window (%s)",
			narrowed_count,
			", ".join(
				f"{int(r.FK_Behandler)}:{int(r.window_months)}m"
				for r in base[base["window_months"].notna()].itertuples()
			),
		)
	unmeasured = int((~judgeable).sum())
	if unmeasured:
		log.info("%d column(s) have too few presence rows to classify", unmeasured)
	if unresolved:
		log.warning(
			"%d column(s) stay between %.0f%% and %.0f%% ortho even at the "
			"%g-month floor, so their discipline is a guess: %s",
			len(unresolved),
			low * 100,
			high * 100,
			floor,
			sorted(unresolved),
		)
	return base


def refine(spine: pd.DataFrame, columns: pd.DataFrame, annotated: pd.DataFrame) -> pd.DataFrame:
	"""Add `discipline_resolved` to the spine.

	Accounting's `discipline` wins where it is set. For the assistant and
	apprentice pools (services 500/550) it is null, and the ZaWin ortho signal
	fills it in, aggregated across all of a person's agenda columns.
	"""
	share = resolve_ortho(annotated)
	linked = columns.dropna(subset=["personnel_no"]).merge(
		share[["FK_Behandler", "presence_rows", "ortho_rows", "window_months"]],
		left_on="behandler_id",
		right_on="FK_Behandler",
		how="left",
	)

	per_person = (
		linked.groupby("personnel_no")
		.agg(presence_rows=("presence_rows", "sum"), ortho_rows=("ortho_rows", "sum"))
		.reset_index()
	)
	per_person["ortho_share"] = (per_person["ortho_rows"] / per_person["presence_rows"]).where(
		per_person["presence_rows"] >= settings.get().threshold("min_presence_rows", 20)
	)

	out = spine.merge(per_person, on="personnel_no", how="left")

	inferred = out["ortho_share"] >= settings.get().threshold("ortho", 0.50)
	out["discipline_resolved"] = out["discipline"]
	unresolved = out["discipline"].isna()
	out.loc[unresolved & inferred, "discipline_resolved"] = "Orthodontics"
	out.loc[
		unresolved & (out["ortho_share"] < settings.get().threshold("ortho", 0.50)), "discipline_resolved"
	] = "Omnipractice"
	out["discipline_source"] = "accounting"
	out.loc[unresolved & out["ortho_share"].notna(), "discipline_source"] = "zawin_label"
	out.loc[out["discipline_resolved"].isna(), "discipline_source"] = "unresolved"

	log.info(
		"discipline: %s",
		", ".join(f"{k}={v}" for k, v in out["discipline_resolved"].value_counts(dropna=False).items()),
	)
	return out
