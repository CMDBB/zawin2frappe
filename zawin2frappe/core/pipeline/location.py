"""Which Shift Location a reconstructed shift belongs to.

autoshift reads **both** branch and discipline off `Shift Assignment.shift_location`
(`optimizer/data_loader.py`, "Source of truth for branch and discipline"), so a
Shift Location is a *(branch, discipline)* pair — "Balexert - Omnipractice" — not
merely a site.

The two halves come from opposite places:

  discipline  the person's own `discipline_resolved`, already settled by
              `pipeline.discipline` and `pipeline.scope`. Generic.
  branch      which agenda rows, columns or codes mean "the other site". That is
              a fact about one practice's ZaWin install and its staff's typing
              habits, so this module carries none of it: a profile names a
              resolver, and a practice with one site names none.

Resolution is deliberately **asymmetric**. A branch other than the default is
only ever asserted, never inferred from absence, so any signal claiming the
second site wins over any number of rows that merely fail to mention it. That
is what lets several weak, partial signals be combined safely: each one can
only ever add coverage, never take it away.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

import pandas as pd

from .. import settings

log = logging.getLogger(__name__)

#: Profile key naming the branch resolver factory.
RESOLVER = "branch"


@runtime_checkable
class BranchResolver(Protocol):
	"""Practice-specific rules for which site a shift was worked at.

	Both methods may return nothing: a resolver that only knows about agenda
	columns can ignore rows entirely, and vice versa. Anything left unclaimed
	falls to `default_branch`.
	"""

	#: Where a shift is worked when no signal says otherwise.
	default_branch: str

	def row_branches(self, annotated: pd.DataFrame) -> pd.Series:
		"""Branch per agenda row, indexed like `annotated`. NA = no signal."""
		...

	def column_branches(self, columns: pd.DataFrame, query) -> dict[int, str]:
		"""Branch per `behandler_id`, for columns pinned to one site.

		`query` is the ZaWin SQL accessor, passed in so a resolver never has to
		import this package to reach the database.
		"""
		...


def _resolver() -> BranchResolver | None:
	return settings.get().resolver(RESOLVER)


def default_branch() -> str:
	"""The branch everything falls back to.

	The resolver owns it; without one, the profile's first branch is the only
	sensible reading of "the site this practice works at".
	"""
	resolver = _resolver()
	if resolver is not None:
		return resolver.default_branch
	branches = settings.get().branches
	return branches[0] if branches else ""


def row_branches(annotated: pd.DataFrame) -> pd.Series:
	"""Per-agenda-row branch signal, or an all-NA column when none applies."""
	resolver = _resolver()
	if resolver is None or annotated.empty or not hasattr(resolver, "row_branches"):
		return pd.Series(pd.NA, index=annotated.index, dtype="object")

	out = resolver.row_branches(annotated).reindex(annotated.index)
	found = int(out.notna().sum())
	if found:
		log.info(
			"branch: %d agenda rows carry a site signal (%s)",
			found,
			", ".join(f"{k}={v}" for k, v in out.value_counts().items()),
		)
	return out


def column_branches(columns: pd.DataFrame, query) -> dict[int, str]:
	"""Per-column branch signal, for columns that belong to one site."""
	resolver = _resolver()
	if resolver is None or not hasattr(resolver, "column_branches"):
		return {}

	out = resolver.column_branches(columns, query)
	if out:
		log.info("branch: %d agenda columns are pinned to a site", len(out))
	return out


def promote(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
	"""Spread an asserted branch across every row sharing `keys`.

	The asymmetry in this module's docstring, made concrete: within a group,
	one row naming a site settles it for all of them, because the others are
	silent rather than contradictory. Applied wherever rows are about to be
	deduplicated or collapsed — otherwise the survivor is chosen on unrelated
	grounds (lowest `Zähler`, sort order) and the site becomes a coin toss.
	"""
	if df.empty or "branch" not in df:
		return df
	out = df.copy()
	# GroupBy.first skips nulls, so this is "the first row in the group that
	# names a site, NA if none does" — vectorised, over ~20k rows per call.
	out["branch"] = out.groupby(keys, dropna=False)["branch"].transform("first")
	return out


def apply(person_level: pd.DataFrame, columns: pd.DataFrame, query) -> pd.DataFrame:
	"""Settle a `branch` for every person-level half-day.

	Order, strongest first:

	  1. a row-level signal on the source agenda row (carried by `presence`)
	  2. the column the shift was worked in, when that column is site-bound
	  3. anything else asserted for that person on that day
	  4. the default

	Step 3 matters because reconstructed half-days carry no source row: a day
	evidenced by one tagged row and one derived row would otherwise land half
	at each site.
	"""
	if person_level.empty:
		return person_level

	df = person_level.copy()
	if "branch" not in df:
		df["branch"] = pd.NA

	by_column = column_branches(columns, query)
	if by_column:
		df["branch"] = df["branch"].fillna(df["behandler_id"].map(by_column))

	tagged = int(df["branch"].notna().sum())
	df = promote(df, ["personnel_no", "date"])

	fallback = default_branch()
	df["branch"] = df["branch"].fillna(fallback)

	log.info(
		"branch: %s (%d half-days carried a site signal, %d more inherited one from the same day)",
		", ".join(f"{k}={v}" for k, v in df["branch"].value_counts().items()),
		tagged,
		int(df["branch"].ne(fallback).sum() - tagged),
	)
	return df


def location_name(branch, discipline) -> str | None:
	"""Shift Location docname for a (branch, discipline) pair.

	`Shift Location.location_name` is its own docname, so this string is the
	link value every Shift Assignment carries.
	"""
	if branch is None or discipline is None or pd.isna(branch) or pd.isna(discipline):
		return None
	return f"{branch} - {discipline}"
