"""Which discipline someone works in, when accounting does not say.

Accounting files whole pools under a single service — every assistant under one
code, every apprentice under another — and records no discipline for them,
because payroll has no use for one. autoshift does: its room-staffing ratios
are per discipline, so a pool filed under the wrong one over-states that
discipline's capacity and under-states another's.

ZaWin recovers the split from the free-text agenda label. Three kinds of
evidence are read from it, in precedence order, and none of them is knowledge
this module holds:

  a named discipline   the label says which discipline it is. Which words mean
                       which discipline is `profile.discipline_labels`, read
                       via `shifts.discipline_hint`.
  a named practitioner  the label names the person being assisted, by their
                       `BEHANDLER.Initialen` — "PRES CFO", "PRES FH". Whoever
                       that is, accounting has already placed them, so their
                       discipline transfers. This needs no configuration at
                       all: both the initials and the placements are already
                       in the data.
  nothing named        a bare "PRES" names no discipline. At most practices
                       that means the main floor, which is what
                       `profile.default_discipline` says; a practice that
                       leaves it null simply has fewer rows to judge on.

Each presence row is attributed to one discipline that way, and a person works
in whichever discipline holds most of their rows. Plurality, not majority: with
more than two disciplines in play, "more of their time than any other
discipline" is the claim the evidence actually supports, and requiring half of
everything would push genuinely mixed staff back onto the default.

Two further signals sit outside the label entirely:

  a colour rule marked `primary` places its people directly (see
  `colour_placements`), and a curated `staff_scope` override overrides
  everything (see `pipeline.scope`).

Accounting always wins where it has an answer. All of this only fills gaps.

Historically this module split one assistant pool two ways — ortho or not —
against a threshold. The distribution that justified it is still the reason the
narrowing below exists: measured as each employee's ortho share of their
presence rows over 2024, it was sharply bimodal, 41 employees at 0%, nine above
75%, and nobody at all between 25% and 75%. A share landing in that empty band
never described a person; it described a window averaging over a change of
role.
"""

from __future__ import annotations

import logging
import re

import pandas as pd

from .. import settings

log = logging.getLogger(__name__)

#: Shift kinds that positively place someone at work in a discipline.
PRESENCE_KINDS = ("present", "reception", "admin")

#: Windows tried in turn, in months back from the most recent agenda date.
#: None means "everything". Wide first, because more rows make a steadier
#: estimate; narrower only for the columns that need it.
NARROWING_MONTHS = (None, 36, 24, 18, 12, 9, 6)

#: Never narrower than this, however ambiguous the column remains — below it
#: there is too little evidence to prefer over the wider reading.
#: Overridable as threshold("signal_floor_months").
DEFAULT_FLOOR_MONTHS = 6

#: Share the leading discipline must reach for the reading to stand on its own.
#: Below it the window is averaging over a change of role, so it is worth
#: re-measuring over a shorter one. Overridable as threshold("ambiguous_high").
DEFAULT_CONFIDENT = 0.75

#: Below this share the leading discipline is still assigned — it remains the
#: best answer available — but the build says so. Overridable as
#: threshold("discipline_dominance").
DEFAULT_DOMINANCE = 0.50

#: Words in a label that could be someone's initials. One-letter tokens are
#: excluded: "PRES A BALEXERT" is a preposition, not a person.
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9'-]+")


def accounting_discipline(spine: pd.DataFrame) -> pd.Series:
	"""What accounting says, for everyone it says anything about.

	`roster` fills `discipline` only for people the accounting export lists.
	Payroll-only rows — leavers, mostly — carry a service number all the same,
	and the profile maps that service to a discipline just as authoritatively.
	Reading it back here is what stops a departed hygienist being handed to the
	agenda signal and coming back as whatever their assistants happened to type.
	"""
	prof = settings.get()
	from_service = spine["service_no"].map(lambda code: prof.service(code).discipline)
	return spine["discipline"].fillna(from_service)


def practitioner_disciplines(spine: pd.DataFrame, columns: pd.DataFrame) -> dict[str, str]:
	"""Agenda-column initials -> the discipline accounting placed that person in.

	Only people accounting has *already* placed contribute, which is what keeps
	the signal acyclic: the pools being resolved here never vote on themselves.
	Initials that two differently-placed people share are dropped rather than
	guessed at — one shared abbreviation is not worth a wrong discipline.
	"""
	placed = spine.assign(discipline=accounting_discipline(spine)).dropna(subset=["discipline"])
	linked = columns.dropna(subset=["personnel_no"]).merge(
		placed[["personnel_no", "discipline"]], on="personnel_no", how="inner"
	)
	if linked.empty:
		return {}
	linked = linked.assign(token=linked["initials"].astype(str).str.strip().str.upper())
	linked = linked[linked["token"].str.len() > 1]
	by_token = linked.groupby("token")["discipline"].unique()
	return {token: names[0] for token, names in by_token.items() if len(names) == 1}


def _named_practitioner(label: str, by_initials: dict[str, str]) -> str | None:
	"""The discipline of the first practitioner a label names, if any.

	The leading word is skipped: it is the kind of entry ("PRES"), never a
	person, and skipping it costs nothing while removing a whole class of
	collision with practices whose vocabulary happens to look like initials.
	"""
	tokens = _TOKEN.findall(label or "")
	for token in tokens[1:]:
		hit = by_initials.get(token.upper())
		if hit:
			return hit
	return None


def attribute(presence: pd.DataFrame, by_initials: dict[str, str]) -> pd.Series:
	"""The discipline each presence row points at, or NA if none does."""
	named = presence["discipline_hint"]
	if by_initials:
		assisted = presence["label_clean"].map(lambda s: _named_practitioner(s, by_initials))
		named = named.fillna(assisted)
	default = settings.get().default_discipline
	return named.fillna(default) if default else named


def presence_counts(
	annotated: pd.DataFrame,
	by_initials: dict[str, str],
	key: str = "FK_Behandler",
) -> pd.DataFrame:
	"""Long: presence rows per (`key`, discipline).

	Long rather than one column per discipline because the same counts are
	summed twice at different grains — per agenda column while the window is
	being chosen, then per person once it is.
	"""
	presence = annotated[annotated["shift_kind"].isin(PRESENCE_KINDS)]
	if presence.empty:
		return pd.DataFrame(columns=[key, "discipline", "rows"])
	return (
		presence.assign(discipline=attribute(presence, by_initials))
		.dropna(subset=["discipline"])
		.groupby([key, "discipline"])
		.size()
		.reset_index(name="rows")
	)


def leading(counts: pd.DataFrame, key: str = "FK_Behandler") -> pd.DataFrame:
	"""The discipline holding most of each key's attributed rows.

	`presence_rows` is the attributed total, not every presence row: a share is
	only meaningful against the rows that carried a signal at all.
	"""
	out_columns = [key, "discipline", "discipline_rows", "presence_rows", "share"]
	if counts.empty:
		return pd.DataFrame(columns=out_columns)

	totals = counts.groupby(key)["rows"].transform("sum")
	ranked = counts.assign(presence_rows=totals, share=counts["rows"] / totals)
	# Ties break on the profile's own ordering of disciplines, so the answer
	# does not depend on row order or on how pandas happened to group.
	order = {name: i for i, name in enumerate(settings.get().all_disciplines)}
	ranked["_order"] = ranked["discipline"].map(lambda d: order.get(d, len(order)))
	ranked = ranked.sort_values([key, "share", "_order", "discipline"], ascending=[True, False, True, True])
	top = ranked.groupby(key, as_index=False).first()
	return top.rename(columns={"rows": "discipline_rows"})[out_columns]


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


def resolve(annotated: pd.DataFrame, by_initials: dict[str, str]) -> pd.DataFrame:
	"""Per-column discipline counts, narrowing the window only where ambiguous.

	A wide window is the better estimator for anyone whose role has been
	steady — more rows, less noise. It is the *wrong* estimator for someone who
	changed discipline partway through, because the average then describes
	neither era: one apprentice read 49% ortho over all history while sitting at
	89-99% in every individual year since 2022.

	So: measure wide, then re-measure just the ambiguous columns over
	progressively shorter recent windows until they resolve or the floor is
	reached. A column that never resolves keeps its narrowest usable reading
	and is reported, not silently trusted.

	Returns the long counts frame with `window_months` (NA = all history)
	attached, so every later count says which evidence produced it.
	"""
	prof = settings.get()
	confident = prof.threshold("ambiguous_high", DEFAULT_CONFIDENT)
	floor = prof.threshold("signal_floor_months", DEFAULT_FLOOR_MONTHS)
	min_rows = prof.threshold("min_presence_rows", 20)

	counts = presence_counts(annotated, by_initials)
	counts["window_months"] = pd.NA
	top = leading(counts)

	# Only chase columns that have enough rows to be judged at all. A column
	# below the minimum is not *ambiguous*, it is simply unmeasured, and
	# narrowing the window can only make that worse.
	judgeable = top["presence_rows"] >= min_rows
	unresolved = set(top.loc[judgeable & (top["share"] < confident), "FK_Behandler"])
	unmeasured = int((~judgeable).sum())

	for months in NARROWING_MONTHS[1:]:
		if not unresolved or months < floor:
			break
		# Window first, then drop the columns already settled: the cutoff is
		# anchored to the whole frame's latest date, so it must not depend on
		# which columns happen to still be open.
		recent = _window(annotated, months)
		narrowed = presence_counts(recent[recent["FK_Behandler"].isin(unresolved)], by_initials)
		if narrowed.empty:
			continue
		narrowed_top = leading(narrowed).set_index("FK_Behandler")
		# Too little evidence at this width; keep the wider reading and look on.
		usable = narrowed_top[narrowed_top["presence_rows"] >= min_rows]
		if usable.empty:
			continue
		taken = set(usable.index)
		counts = pd.concat(
			[
				counts[~counts["FK_Behandler"].isin(taken)],
				narrowed[narrowed["FK_Behandler"].isin(taken)].assign(window_months=months),
			],
			ignore_index=True,
		)
		unresolved -= set(usable.index[usable["share"] >= confident])

	narrowed_columns = counts.loc[counts["window_months"].notna(), ["FK_Behandler", "window_months"]]
	narrowed_columns = narrowed_columns.drop_duplicates()
	if not narrowed_columns.empty:
		log.info(
			"discipline signal: %d column(s) needed a narrower window (%s)",
			len(narrowed_columns),
			", ".join(
				f"{int(r.FK_Behandler)}:{int(r.window_months)}m" for r in narrowed_columns.itertuples()
			),
		)
	if unmeasured:
		log.info("%d column(s) have too few presence rows to classify", unmeasured)
	if unresolved:
		log.warning(
			"%d column(s) never reach %.0f%% in any one discipline, even at the "
			"%g-month floor, so their discipline is the best of a mixed picture: %s",
			len(unresolved),
			confident * 100,
			floor,
			sorted(unresolved),
		)
	return counts


def colour_placements(columns: pd.DataFrame) -> dict[str, str]:
	"""People a colour rule places in a discipline outright.

	A `role_color_rules` entry normally grants a *second* Scheduling Role on
	top of the designation-derived one (`pipeline.roles`). An entry marked
	`primary` says something stronger: the colour is what this person actually
	does, so it decides their discipline rather than adding to it.

	That matters where the work leaves no trace in the label — sterilization
	staff type a bare "PRES" like everyone else, so no share of anything will
	ever find them, but the practice has already marked them by giving their
	agenda column its own default appointment colour.
	"""
	prof = settings.get()
	primary = {
		colour: rule["discipline"]
		for colour, rule in prof.role_color_rules.items()
		if rule.get("primary") and rule.get("discipline")
	}
	if not primary:
		return {}

	from .. import extract

	colours = extract.employees()[["behandler_id", "default_color"]]
	linked = columns.dropna(subset=["personnel_no"]).merge(colours, on="behandler_id", how="left")
	linked["placed"] = linked["default_color"].map(lambda c: primary.get(int(c)) if pd.notna(c) else None)
	hits = linked.dropna(subset=["placed"])
	return dict(zip(hits["personnel_no"], hits["placed"], strict=False))


def unnamed_vocabulary(
	annotated: pd.DataFrame, by_initials: dict[str, str], limit: int = 10
) -> list[tuple[str, int]]:
	"""The commonest label words no rule and no practitioner claims.

	Purely a prompt for whoever maintains the profile: a word near the top of
	this list that turns out to mean a discipline is one `discipline_labels`
	entry away from being counted properly. Without it those rows fall to the
	default and quietly inflate whatever discipline that is.
	"""
	presence = annotated[annotated["shift_kind"].isin(PRESENCE_KINDS)]
	unclaimed = presence[presence["discipline_hint"].isna()]
	if unclaimed.empty:
		return []
	counted: dict[str, int] = {}
	for label in unclaimed["label_clean"]:
		if _named_practitioner(label, by_initials):
			continue
		for token in _TOKEN.findall(label or "")[1:]:
			counted[token.upper()] = counted.get(token.upper(), 0) + 1
	return sorted(counted.items(), key=lambda kv: -kv[1])[:limit]


def refine(spine: pd.DataFrame, columns: pd.DataFrame, annotated: pd.DataFrame) -> pd.DataFrame:
	"""Add `discipline_resolved` to the spine.

	Accounting's `discipline` wins wherever it is set. For the pools it leaves
	null, a colour rule places whoever it marks and the label signal fills in
	the rest, aggregated across all of a person's agenda columns.

	`discipline_signal` and `discipline_signal_share` stay on the spine whether
	or not they were used, so what the agenda said about someone accounting had
	already placed is still there to compare against.
	"""
	prof = settings.get()
	min_rows = prof.threshold("min_presence_rows", 20)
	by_initials = practitioner_disciplines(spine, columns)
	counts = resolve(annotated, by_initials)

	linked = columns.dropna(subset=["personnel_no"])[["behandler_id", "personnel_no"]]
	per_person = (
		counts.merge(linked, left_on="FK_Behandler", right_on="behandler_id", how="inner")
		.groupby(["personnel_no", "discipline"], as_index=False)["rows"]
		.sum()
	)
	signal = leading(per_person, key="personnel_no")
	signal = signal[signal["presence_rows"] >= min_rows].rename(
		columns={"discipline": "discipline_signal", "share": "discipline_signal_share"}
	)

	out = spine.merge(
		signal[["personnel_no", "discipline_signal", "discipline_signal_share", "presence_rows"]],
		on="personnel_no",
		how="left",
	)

	accounting = accounting_discipline(out)
	out["discipline_resolved"] = accounting
	out["discipline_source"] = "accounting"
	gap = accounting.isna()

	placements = colour_placements(columns)
	if placements:
		placed = out["personnel_no"].map(placements)
		by_colour = gap & placed.notna()
		out.loc[by_colour, "discipline_resolved"] = placed[by_colour]
		out.loc[by_colour, "discipline_source"] = "zawin_colour"
		gap = gap & ~by_colour

	by_label = gap & out["discipline_signal"].notna()
	out.loc[by_label, "discipline_resolved"] = out.loc[by_label, "discipline_signal"]
	out.loc[by_label, "discipline_source"] = "zawin_label"
	out.loc[out["discipline_resolved"].isna(), "discipline_source"] = "unresolved"

	weak = by_label & (
		out["discipline_signal_share"] < prof.threshold("discipline_dominance", DEFAULT_DOMINANCE)
	)
	if weak.any():
		log.info(
			"%d person(s) work no single discipline more than %.0f%% of the time; placed on plurality: %s",
			int(weak.sum()),
			prof.threshold("discipline_dominance", DEFAULT_DOMINANCE) * 100,
			", ".join(
				f"{r.personnel_no}->{r.discipline_resolved} ({r.discipline_signal_share:.0%})"
				for r in out[weak].itertuples()
			),
		)

	unclaimed = unnamed_vocabulary(annotated, by_initials)
	if unclaimed:
		log.info(
			"label words no discipline rule or practitioner claims (add to "
			"discipline_labels if any names a discipline): %s",
			", ".join(f"{word}={n}" for word, n in unclaimed),
		)

	log.info(
		"discipline: %s",
		", ".join(f"{k}={v}" for k, v in out["discipline_resolved"].value_counts(dropna=False).items()),
	)
	return out
