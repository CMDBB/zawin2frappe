"""Whose schedule is settled, and settled enough to be treated as binding.

At some practices a group of staff — practitioners, usually — decide their own
working week, and the practice schedules everyone else around them. autoshift
models that as `Scheduling Role.assignments_binding`: a bound holder keeps
exactly the Shift Assignments already on the books and the optimiser may not
add, move or drop any of them (see autoshift's "Role binding" notes).

Two separate questions, answered from two separate places:

  *which jobs may bind*   Who has that pull is a fact about one practice's
                          power structure, so it is profile data: a service
                          marked `assignments_binding` makes the Scheduling
                          Roles built from it binding. Nothing here knows or
                          guesses which jobs those are.
  *whose week has settled* An eligible person whose week is not actually
                          regular yet — a recent hire, someone mid-change —
                          must not be frozen to a pattern that does not exist.
                          That is a measurement, and it is this module's.

The second is deliberately **conservative in one direction only**. The profile
grants binding to a whole job; this can only ever take it away again from an
individual, by writing `Employee Scheduling Role.binding_override = "Not
Binding"`. It never marks anyone binding whom the profile did not.

### Measuring a settled week

A settled schedule is one that repeats. For each person, over the
recency-weighted lookback window:

    weeks      ISO weeks in which they worked at all. An empty week is leave,
               not a change of pattern, so it is dropped rather than scored as
               a week they worked nothing — otherwise a fortnight's holiday
               reads exactly like an unstable schedule.
    excused    days spent on booked leave or off sick, which the practice
               records *in the slot the shift would have taken*. Those are left
               out of both the vote and the comparison — see `excused_days`.
    weight     0.5 ** (weeks_ago / half_life). A schedule that changed a year
               ago and has been steady since is settled *now*, which is the
               only tense the question has.
    modal      the weekdays worked in at least half the weighted weeks that had
               anything to say about that weekday — the person's usual week.
    settled    the weighted mean Jaccard overlap between each week actually
               worked and that modal week.

So 1.0 is "the same days, every week" and 0.5 is roughly "half the days I work
in a given week are not the days I usually work". Measured on **weekdays, not
half-days**: the AM/PM split of a full-day agenda row is partly decided by
`presence.collapse_daily` rather than recorded, so scoring it would measure
this pipeline's guesswork alongside the person's regularity.

### Rotas longer than a week

A week is not the only cycle a practice runs. Measured here, the practice's
orthodontists turned out to be on rotas longer than one — one works
`MTW.FS. / MTW.... / off / off` and has done so all year — and a weekly measure
rejected all five of them as unsettled. That is exactly backwards: theirs are
among the most settled schedules in the building, and the measure was blind to
them rather than looking at something irregular. The commonest case is smaller
and comes straight off the practice's own wall chart, which has a
"VENDREDI SEM. PAIRE / IMPAIRE" column: a Friday worked on alternate weeks.

So the modal pattern is fitted per *phase* of a cycle, for each period up to
`binding_max_cycle_weeks`, and the period scoring best wins. Longer periods fit
better by having more parameters, so each extra phase costs
`binding_cycle_penalty` — a four-week rota has to beat the weekly reading by
three times that before it is believed, and a phase with too few weeks behind it
(`binding_min_phase_weeks`) is not fitted at all. Phase is counted off absolute
week numbers rather than off the start of the data, so the numbering is stable
between runs. That is a *different* anchor from the chart's own odd/even ISO
week, but it partitions the weeks the same way as long as no ISO year in the
window has 53 weeks, which is the only thing that breaks the chart's alternation
too.

The reported period is the **shortest sufficient** one, which is not always the
one the practice would name: two weeks on and two off comes back as a two-week
cycle, because the off weeks were dropped as leave and the weeks that remain
simply alternate. That is the right reading of the evidence in hand — the score
is what the decision turns on, and it is the same either way.

Validated by holdout — fit on a year, score the thirteen weeks after it. The
measure ranks people by how well their usual week describes the weeks that
follow it (Pearson 0.50 against holdout agreement, rising monotonically across
score bands), and excusing leave is what made the absolute numbers good rather
than merely ordered: agreement in the top band went from 0.88 to 0.92 and its
floor from 0.60 to 0.75, at unchanged correlation. People fitted to a rota
predict better than weekly ones (0.93 against 0.89), which is the clearest sign
the longer cycles are real structure and not extra parameters.

It is not a forecast and does not need to be: binding freezes the assignments
the import itself carries, not a prediction. What the score decides is only
whether this person's schedule is the *kind of thing* that is settled.

The distribution is continuous, with no natural break to read a threshold off,
so `binding_settled_min` is a judgement rather than a discovery. `zawin binding`
prints the ranked scores to calibrate it against, and a build reports where
everyone landed.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from .. import settings

log = logging.getLogger(__name__)

#: Weeks of history the score is read from. A year covers a full cycle of
#: school holidays and summer without reaching back into a previous job.
DEFAULT_LOOKBACK_WEEKS = 52

#: Weeks after which a week's evidence counts half. A quarter: long enough that
#: a fortnight's leave cannot move the score, short enough that a schedule
#: changed two quarters ago no longer dominates the one in force now.
DEFAULT_HALF_LIFE_WEEKS = 13

#: Worked weeks below which there is nothing to measure. Someone with fewer has
#: no established week *yet*, which is exactly the case the override exists for.
DEFAULT_MIN_WEEKS = 12

#: Settledness at or above which an eligible person's schedule is treated as
#: settled. Calibrate with `zawin binding`; see the module docstring on why this
#: is a judgement and not a discovery.
DEFAULT_SETTLED_MIN = 0.75

#: Longest rota to look for, in weeks. Four covers the four-week orthodontic
#: rota this was built against and the fortnightly patterns either side of it;
#: beyond that a year of history stops being enough to fit a phase on.
DEFAULT_MAX_CYCLE_WEEKS = 4

#: What each extra phase of a longer cycle must earn to be believed. A longer
#: period always fits at least as well — it has more parameters — so without a
#: price every schedule reads as a four-week rota.
#:
#: Calibrated against a control group rather than by feel: staff the practice
#: schedules itself should essentially never read as being on a rota, so they
#: measure the false-positive rate directly. On this practice's year, 0.03 is
#: the lowest value at which none of the thirty-five controls flips while
#: seven of the thirty-two self-scheduling staff do; at 0.025 the first control
#: goes. Redo that sweep on your own data before moving it.
DEFAULT_CYCLE_PENALTY = 0.03

#: Weeks a phase needs before it is fitted at all. Below this the "pattern" is
#: one or two weeks read back as a rule.
DEFAULT_MIN_PHASE_WEEKS = 4

#: Monday, so `to_period("W").start_time` differences are whole weeks. Only the
#: consistency of the phase numbering matters, never which Monday it counts from.
_EPOCH = pd.Timestamp("1970-01-05")

OVERRIDE_BINDING = "Binding"
OVERRIDE_NOT_BINDING = "Not Binding"
#: Blank inherits the Scheduling Role's own flag — autoshift's nullable-override
#: convention, the same one `max_rooms` uses.
OVERRIDE_INHERIT = ""

WEEKDAYS = range(7)

#: Columns of the person-level frame that describe a worked day without being
#: part of what makes a schedule regular. Carried through the fit so
#: `modal_pattern` can report them, never scored — see its docstring.
ATTRIBUTES = ("window", "branch")


def _threshold(key: str, default: float) -> float:
	return settings.get().threshold(key, default)


def binding_services() -> set[str]:
	"""Service codes whose holders set their own schedules.

	Empty unless the profile says otherwise, which is the whole feature off:
	no role is emitted binding and no override is ever written.
	"""
	return {code for code, s in settings.get().services.items() if s.assignments_binding}


def load_overrides(path: Path | None = None) -> pd.DataFrame:
	"""Binding decisions a human has made by hand.

	The score is evidence, not a verdict. A practitioner who has just changed
	their week reads as unsettled for a quarter afterwards even though everyone
	in the building knows the new week is final, and the reverse happens too.
	Empty unless the active profile ships the file.
	"""
	path = path or settings.get().override_path("binding_scope")
	if path is None or not Path(path).is_file():
		return pd.DataFrame(columns=["personnel_no", "binding_override", "reason"])
	df = pd.read_csv(path, dtype={"personnel_no": str})
	df["binding_override"] = df["binding_override"].fillna("").astype(str).str.strip()
	bad = set(df["binding_override"]) - {OVERRIDE_BINDING, OVERRIDE_NOT_BINDING, OVERRIDE_INHERIT}
	if bad:
		raise ValueError(
			f"{path}: binding_override must be one of "
			f"{OVERRIDE_BINDING!r}, {OVERRIDE_NOT_BINDING!r} or blank; got {sorted(bad)}"
		)
	return df


def excused_days(annotated: pd.DataFrame, columns: pd.DataFrame) -> pd.DataFrame:
	"""Person-days spent on booked leave or off sick, from the agenda.

	The practice records those *in the slot the shift would otherwise have
	occupied*, which makes them evidence about someone's pattern rather than
	against it: a week with Thursday marked "Vacances" is a week that says
	nothing about Thursday, not a week they chose not to work it.

	Keyed on the agenda **category**, never on the free-text label or on
	`shift_kind`. That distinction is the whole point. Measured over this
	practice's history, the two booked categories land on a weekday the person
	never works 0.2% and 0.0% of the time, while the generic "away" category
	does so 8.3% of the time and carries up to 1,267 full-day rows for a single
	person — it is a background banner, not a slot, and excusing it would blank
	out most of the calendar. Which categories are which is profile data
	(`categories.excused`); empty means nothing is excused.
	"""
	out_columns = ["personnel_no", "date"]
	excused = settings.get().excused_categories
	if not excused or annotated is None or annotated.empty or columns is None or columns.empty:
		return pd.DataFrame(columns=out_columns)

	link = columns.dropna(subset=["personnel_no"])[["behandler_id", "personnel_no"]]
	rows = annotated[annotated["category"].isin(excused)].merge(
		link, left_on="FK_Behandler", right_on="behandler_id", how="inner"
	)
	if rows.empty:
		return pd.DataFrame(columns=out_columns)

	out = pd.DataFrame(
		{"personnel_no": rows["personnel_no"], "date": pd.to_datetime(rows["Datum"]).dt.normalize()}
	).drop_duplicates()
	log.info(
		"binding: %d excused person-days for %d people (%s)",
		len(out),
		out["personnel_no"].nunique(),
		", ".join(excused),
	)
	return out.reset_index(drop=True)


def _jaccard(a: set[int], b: set[int]) -> float:
	union = a | b
	return len(a & b) / len(union) if union else 1.0


def _fit_cycle(
	worked: dict[pd.Timestamp, set[int]],
	weight: dict[pd.Timestamp, float],
	excused: dict[pd.Timestamp, set[int]],
	period: int,
	min_phase_weeks: int,
) -> tuple[float, list[set[int]]] | None:
	"""Fit one modal week per phase of a `period`-week cycle, and score the fit.

	A day the person was on booked leave or off sick is **excused**: it is left
	out of both the vote that decides the usual week and the comparison against
	it, so a fortnight in Spain neither creates a pattern nor breaks one. That
	only works because the practice records leave in the slot the shift would
	have occupied — see `excused_days`.

	Returns None when any phase that has weeks in it has too few to fit, which
	is what stops a rota being read off two observations.
	"""
	phases: dict[int, list[pd.Timestamp]] = {}
	for week in worked:
		phases.setdefault(((week - _EPOCH).days // 7) % period, []).append(week)
	if any(len(weeks) < min_phase_weeks for weeks in phases.values()):
		return None

	modal: dict[int, set[int]] = {}
	for phase, weeks in phases.items():
		for wd in WEEKDAYS:
			# Only the weeks that had something to say about this weekday vote on
			# it. Someone away every Friday in August has not stopped working
			# Fridays, and counting those weeks as "no" would say they had.
			votes = [w for w in weeks if wd not in excused.get(w, ())]
			if not votes:
				continue
			total = sum(weight[w] for w in votes)
			if sum(weight[w] for w in votes if wd in worked[w]) / total >= 0.5:
				modal.setdefault(phase, set()).add(wd)
		modal.setdefault(phase, set())

	total = sum(weight.values())
	score = (
		sum(
			weight[w]
			* _jaccard(
				days - excused.get(w, set()),
				modal[((w - _EPOCH).days // 7) % period] - excused.get(w, set()),
			)
			for w, days in worked.items()
		)
		/ total
	)
	return score, [modal.get(phase, set()) for phase in range(period)]


def _best_cycle(
	worked: dict[pd.Timestamp, set[int]],
	weight: dict[pd.Timestamp, float],
	excused: dict[pd.Timestamp, set[int]],
) -> tuple[float, int, list[set[int]]]:
	"""The cycle length that describes this person best, and how well it does.

	A longer period cannot fit worse, so each extra phase is charged
	`binding_cycle_penalty` and the winner is decided on the adjusted score. The
	*reported* score is the unadjusted one: the penalty exists to choose between
	readings, not to mark someone down for being on a rota.
	"""
	max_period = int(_threshold("binding_max_cycle_weeks", DEFAULT_MAX_CYCLE_WEEKS))
	penalty = _threshold("binding_cycle_penalty", DEFAULT_CYCLE_PENALTY)
	min_phase_weeks = int(_threshold("binding_min_phase_weeks", DEFAULT_MIN_PHASE_WEEKS))

	best = None
	for period in range(1, max(1, max_period) + 1):
		fit = _fit_cycle(worked, weight, excused, period, min_phase_weeks if period > 1 else 1)
		if fit is None:
			continue
		score, modal = fit
		adjusted = score - penalty * (period - 1)
		if best is None or adjusted > best[0]:
			best = (adjusted, score, period, modal)
	# period 1 always fits, so `best` is never None for a person with any weeks.
	_adjusted, score, period, modal = best
	return score, period, modal


def _excused_cells(excused: pd.DataFrame | None, first_week, last_week) -> dict[str, dict]:
	"""`{personnel_no: {week: {weekday, ...}}}` for `excused_days`' output."""
	if excused is None or excused.empty:
		return {}
	df = excused[["personnel_no", "date"]].copy()
	df["date"] = pd.to_datetime(df["date"]).dt.normalize()
	df["week"] = df["date"].dt.to_period("W").dt.start_time
	df = df[(df["week"] >= first_week) & (df["week"] <= last_week)]
	out: dict[str, dict] = {}
	for row in df.itertuples():
		out.setdefault(row.personnel_no, {}).setdefault(row.week, set()).add(row.date.weekday())
	return out


def _fits(person_level: pd.DataFrame, as_of=None, excused: pd.DataFrame | None = None):
	"""Yield one fitted person at a time: `(personnel_no, fit)`.

	The single place a person's weeks are assembled, weighted and fitted, so
	that the score and the shift-type breakdown built from it cannot disagree
	about what someone's usual week is.
	"""
	# Never later than today. A build may legitimately reach forward — the
	# agenda is booked a year ahead — but a schedule the practice has pencilled
	# in is the thing being judged, not evidence for it, and anchoring the
	# lookback in the future would score people on their own booked pattern.
	today = pd.Timestamp.today().normalize()
	as_of = min(pd.Timestamp(as_of).normalize(), today) if as_of is not None else today
	half_life = _threshold("binding_half_life_weeks", DEFAULT_HALF_LIFE_WEEKS)
	lookback = int(_threshold("binding_lookback_weeks", DEFAULT_LOOKBACK_WEEKS))
	min_weeks = int(_threshold("binding_min_weeks", DEFAULT_MIN_WEEKS))

	extras = [c for c in ATTRIBUTES if c in person_level]
	df = person_level[["personnel_no", "date", *extras]].copy()
	df["date"] = pd.to_datetime(df["date"]).dt.normalize()
	df["week"] = df["date"].dt.to_period("W").dt.start_time
	df["weekday"] = df["date"].dt.weekday

	# Whole weeks only, and strictly in the past. A week clipped by either end of
	# the window looks like a week somebody worked fewer days than usual, and the
	# clipped one at the recent end carries the most weight of any week there is.
	last_week = (as_of - pd.Timedelta(days=6)).to_period("W").start_time
	first_week = (as_of - pd.Timedelta(weeks=lookback)).to_period("W").start_time
	df = df[(df["week"] >= first_week) & (df["week"] <= last_week)]
	if df.empty:
		return
	excused_cells = _excused_cells(excused, first_week, last_week)

	for personnel_no, sub in df.groupby("personnel_no"):
		worked: dict[pd.Timestamp, set[int]] = {}
		attributes: dict[str, dict[tuple[pd.Timestamp, int], object]] = {c: {} for c in extras}
		for row in sub.itertuples():
			cell = (row.week, int(row.weekday))
			worked.setdefault(row.week, set()).add(cell[1])
			for column in extras:
				attributes[column][cell] = getattr(row, column)
		if len(worked) < min_weeks:
			continue

		# A day both worked and marked absent is a day they worked: the shift is
		# the stronger evidence, and an excused day is only ever a day with
		# nothing on it.
		away = {
			week: days - worked.get(week, set()) for week, days in excused_cells.get(personnel_no, {}).items()
		}
		weight = {w: 0.5 ** (((as_of - w).days / 7.0) / half_life) for w in worked}
		settledness, period, modal = _best_cycle(worked, weight, away)
		yield (
			personnel_no,
			{
				"worked": worked,
				"weight": weight,
				"attributes": attributes,
				"settledness": settledness,
				"period": period,
				"modal": modal,
			},
		)


def weekly_settledness(person_level: pd.DataFrame, as_of=None, excused=None) -> pd.DataFrame:
	"""Per person: how closely their weeks repeat, and what their usual week is.

	`person_level` is the collapsed half-day frame `pipeline.presence` produces
	— one row per person per worked day. Nothing here needs the ZaWin database:
	the same frame the Shift Assignments are built from is the evidence.

	Returns one row per person with enough history to judge; people below
	`binding_min_weeks` are absent rather than scored zero, because too little
	history is not the same finding as an irregular week.
	"""
	columns = [
		"personnel_no",
		"settledness",
		"cycle_weeks",
		"modal_week",
		"modal_days",
		"weeks",
		"days_per_week",
	]
	if person_level is None or person_level.empty:
		return pd.DataFrame(columns=columns)

	rows = []
	for personnel_no, fit in _fits(person_level, as_of, excused):
		weight, worked, modal = fit["weight"], fit["worked"], fit["modal"]
		total = sum(weight.values())
		rows.append(
			{
				"personnel_no": personnel_no,
				"settledness": round(fit["settledness"], 4),
				"cycle_weeks": fit["period"],
				"modal_week": "/".join(
					"".join("MTWTFSS"[wd] if wd in phase else "." for wd in WEEKDAYS) for phase in modal
				),
				"modal_days": sum(len(phase) for phase in modal) / len(modal),
				"weeks": len(worked),
				"days_per_week": round(sum(weight[w] * len(days) for w, days in worked.items()) / total, 2),
			}
		)

	out = pd.DataFrame(rows, columns=columns)
	return out.sort_values("settledness", ascending=False).reset_index(drop=True)


def _modal_attribute(
	observed: dict[tuple[pd.Timestamp, int], object],
	weight: dict[pd.Timestamp, float],
	period: int,
	phase: int,
	weekday: int,
) -> tuple[object, float] | None:
	"""The commonest value on this cell of the cycle, and its weighted share."""
	seen: dict[object, float] = {}
	for week, w in weight.items():
		if ((week - _EPOCH).days // 7) % period != phase:
			continue
		value = observed.get((week, weekday))
		if value is not None and not (isinstance(value, float) and pd.isna(value)):
			seen[value] = seen.get(value, 0.0) + w
	if not seen:
		return None
	best = max(seen, key=seen.get)
	return best, round(seen[best] / sum(seen.values()), 3)


def modal_pattern(person_level: pd.DataFrame, as_of=None, excused=None) -> pd.DataFrame:
	"""The usual week, one row per (person, phase, weekday), with its attributes.

	`weekly_settledness` answers *how* regular someone is; this says *what* the
	regular thing is, in the shape `pipeline.schedules` needs to write an HRMS
	Shift Schedule. Only the weekdays in the modal pattern appear.

	Shift type and branch are decided here rather than folded into the score,
	because they are evidence of a different quality. Which *days* someone works
	is recorded; which *half* of the day is partly inferred by
	`presence.collapse_daily` from clock times and patient bookings, and where it
	has neither it falls back to the person's own AM/PM propensity. Branch is
	weaker still — `pipeline.location` only ever *asserts* a second site, so
	silence reads as the default one. Each therefore travels with the weighted
	share behind it: an attribute at 0.55 is a call the pipeline made, and
	nothing downstream should present it as a fact.
	"""
	columns = [
		"personnel_no",
		"phase",
		"cycle_weeks",
		"weekday",
		"window",
		"window_share",
		"branch",
		"branch_share",
	]
	if person_level is None or person_level.empty or "window" not in person_level:
		return pd.DataFrame(columns=columns)

	rows = []
	for personnel_no, fit in _fits(person_level, as_of, excused):
		period, weight, attributes = fit["period"], fit["weight"], fit["attributes"]
		for phase, days in enumerate(fit["modal"]):
			for weekday in sorted(days):
				window = _modal_attribute(attributes.get("window", {}), weight, period, phase, weekday)
				if window is None:
					continue
				branch = _modal_attribute(attributes.get("branch", {}), weight, period, phase, weekday)
				rows.append(
					{
						"personnel_no": personnel_no,
						"phase": phase,
						"cycle_weeks": period,
						"weekday": weekday,
						"window": window[0],
						"window_share": window[1],
						"branch": branch[0] if branch else None,
						"branch_share": branch[1] if branch else None,
					}
				)

	return pd.DataFrame(rows, columns=columns)


def resolve(spine: pd.DataFrame, person_level: pd.DataFrame | None, as_of=None, excused=None) -> pd.DataFrame:
	"""Per person: whether their job may bind, and what override to write.

	One row per schedulable person, whether or not they are eligible, so the
	build can report the whole picture rather than only the people it acted on.
	`binding_override` is the value for `Employee Scheduling Role`, and is blank
	for everyone the profile does not make eligible — their roles are not
	binding, so there is nothing to override.
	"""
	columns = [
		"personnel_no",
		"service_no",
		"eligible",
		"settledness",
		"cycle_weeks",
		"modal_week",
		"weeks",
		"binding_override",
		"reason",
	]
	eligible_services = binding_services()
	people = spine[spine["schedulable"]][["personnel_no", "service_no"]].copy()
	if people.empty:
		return pd.DataFrame(columns=columns)

	scores = weekly_settledness(person_level, as_of=as_of, excused=excused).set_index("personnel_no")
	settled_min = _threshold("binding_settled_min", DEFAULT_SETTLED_MIN)
	measurable = person_level is not None and not person_level.empty

	people["eligible"] = people["service_no"].isin(eligible_services)
	people["settledness"] = people["personnel_no"].map(scores["settledness"])
	people["cycle_weeks"] = people["personnel_no"].map(scores["cycle_weeks"])
	people["modal_week"] = people["personnel_no"].map(scores["modal_week"])
	people["weeks"] = people["personnel_no"].map(scores["weeks"])

	def decide(row) -> tuple[str, str]:
		if not row["eligible"]:
			return OVERRIDE_INHERIT, "not an eligible job"
		if not measurable:
			# Roles still carry the profile's flag; only the individual check is
			# skipped, and saying so beats silently binding everyone.
			return OVERRIDE_INHERIT, "no agenda history in this build; not checked"
		if pd.isna(row["settledness"]):
			return OVERRIDE_NOT_BINDING, "too little history to establish a week"
		if row["settledness"] < settled_min:
			return OVERRIDE_NOT_BINDING, f"week not settled ({row['settledness']:.2f})"
		return OVERRIDE_INHERIT, f"settled ({row['settledness']:.2f})"

	decided = [decide(row) for _, row in people.iterrows()]
	people["binding_override"] = [d[0] for d in decided]
	people["reason"] = [d[1] for d in decided]

	overrides = load_overrides()
	if not overrides.empty:
		by_person = overrides.set_index("personnel_no")["binding_override"]
		hit = people["personnel_no"].isin(by_person.index)
		people.loc[hit, "binding_override"] = people.loc[hit, "personnel_no"].map(by_person)
		people.loc[hit, "reason"] = "curated override"
		log.info("applied %d curated binding overrides", int(hit.sum()))

	out = people[columns].sort_values(["eligible", "settledness"], ascending=[False, False])
	_report(out, eligible_services, settled_min, measurable)
	return out.reset_index(drop=True)


def _report(
	resolved: pd.DataFrame, eligible_services: set[str], settled_min: float, measurable: bool
) -> None:
	if not eligible_services:
		log.info("binding: no service is marked assignments_binding; nothing is bound")
		return
	eligible = resolved[resolved["eligible"]]
	frozen = eligible[eligible["binding_override"] != OVERRIDE_NOT_BINDING]
	log.info(
		"binding: %d people in %d binding service(s); %d settled at >=%.2f, %d held back%s",
		len(eligible),
		len(eligible_services),
		len(frozen),
		settled_min,
		len(eligible) - len(frozen),
		"" if measurable else " (no history in this build — nobody checked)",
	)
	held = eligible[eligible["binding_override"] == OVERRIDE_NOT_BINDING]
	if not held.empty:
		log.info(
			"binding: not binding -> %s",
			", ".join(f"{r.personnel_no} ({r.reason})" for r in held.itertuples()),
		)
