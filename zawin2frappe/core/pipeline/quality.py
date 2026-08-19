"""Statistical report on source-data inconsistency.

The practice is redesigning away from its current HR conventions, so the point
here is not to fix ZaWin's idiosyncrasies but to measure them: how much of the
agenda is signal, how much is one-off noise, and how far the data can be trusted
as a training target.

Nothing here gates the import. Assignments are ground truth for autoshift
regardless; these numbers say how much confidence to attach to them.
"""

from __future__ import annotations

import logging

import pandas as pd

from .. import shifts

log = logging.getLogger(__name__)


def shift_patterns(annotated: pd.DataFrame) -> dict:
	"""How many distinct start/end pairs the agenda actually uses.

	The practice's own complaint is that it has more shift types than people.
	This quantifies it, and shows the distribution is extremely head-heavy: a
	handful of real patterns plus a long tail of one-offs.
	"""
	counts = annotated.groupby(["VonZeit", "BisZeit"]).size().sort_values(ascending=False)
	share = counts.cumsum() / counts.sum()
	return {
		"distinct_patterns": len(counts),
		"patterns_for_50pct": int((share <= 0.50).sum() + 1),
		"patterns_for_90pct": int((share <= 0.90).sum() + 1),
		"used_once": int((counts == 1).sum()),
		"top": [
			{
				"start": int(v),
				"end": int(b),
				"rows": int(n),
				"label": f"{shifts.__name__ and ''}{v // 60:02d}:{v % 60:02d}-{b // 60:02d}:{b % 60:02d}",
			}
			for (v, b), n in counts.head(8).items()
		],
	}


def label_vocabulary(annotated: pd.DataFrame) -> dict:
	"""How chaotic the free-text shift labels are."""
	kinds = annotated["shift_kind"].value_counts()
	total = len(annotated)
	return {
		"rows": int(total),
		"distinct_raw": int(annotated["Beschreibung"].fillna("").nunique()),
		"distinct_cleaned": int(annotated["label_clean"].nunique()),
		"unclassified": int(kinds.get("other", 0)),
		"unclassified_pct": round(100 * kinds.get("other", 0) / total, 1),
		"blank": int(kinds.get("unlabelled", 0)),
		"blank_pct": round(100 * kinds.get("unlabelled", 0) / total, 1),
		"by_kind": {k: int(v) for k, v in kinds.items()},
	}


def absence_reliability(annotated: pd.DataFrame) -> dict:
	"""Whether full-day absence markers can be believed.

	They frequently cannot: some columns carry more full-day absence rows than
	the window has dates, which means they are background banners rather than
	real absences. This is why reconstruction treats patient appointments as
	outranking an absence marker.
	"""
	dates = int(annotated["Datum"].nunique())
	absence = annotated[annotated["shift_kind"] == "absence"]
	full_day = absence[absence["shift_window"] == "full_day"]
	per_column = full_day.groupby("FK_Behandler").size()
	return {
		"distinct_dates": dates,
		"full_day_absence_rows": len(full_day),
		"columns_exceeding_dates": int((per_column > dates).sum()),
		"columns_with_absences": len(per_column),
		"worst_column_rows": int(per_column.max()) if len(per_column) else 0,
	}


def window_durations(annotated: pd.DataFrame) -> dict:
	counts = annotated["shift_window"].value_counts()
	total = len(annotated)
	return {k: {"rows": int(v), "pct": round(100 * v / total, 1)} for k, v in counts.items()}


def bookkeeping_styles(annotated: pd.DataFrame) -> dict:
	styles = shifts.classify_style(annotated)
	return {k: int(v) for k, v in styles["style"].value_counts().items()}


def report(annotated: pd.DataFrame, fte: pd.DataFrame | None = None) -> dict:
	"""Everything, as a plain dict — JSON-serialisable for the manifest."""
	out = {
		"shift_patterns": shift_patterns(annotated),
		"labels": label_vocabulary(annotated),
		"absence_reliability": absence_reliability(annotated),
		"window_durations": window_durations(annotated),
		"bookkeeping_styles": bookkeeping_styles(annotated),
	}
	if fte is not None and not fte.empty:
		out["fte"] = {
			"people": len(fte),
			"median_ratio": float(fte["ratio"].median()),
			"within_tolerance": int(fte["within_tolerance"].sum()),
			"by_style": {
				str(k): {
					"n": len(g),
					"median": round(float(g["ratio"].median()), 2),
					"within_band": round(float(g["ratio"].between(0.6, 1.4).mean()), 2),
				}
				for k, g in fte.groupby("style")
			},
		}
	return out


def drift(earlier: pd.DataFrame, later: pd.DataFrame) -> pd.DataFrame:
	"""Per-person change in workload ratio between two periods.

	A low correlation here means individual workloads are genuinely unstable
	year to year, so a single-period extract should not be read as a stable
	description of anyone's contract.
	"""
	a = earlier.set_index("personnel_no")[["ratio", "fte_pct", "surname", "service"]]
	b = later.set_index("personnel_no")[["ratio"]]
	out = a.join(b, rsuffix="_later", how="inner")
	out["drift"] = (out["ratio_later"] - out["ratio"]).round(2)
	return out.reset_index()
