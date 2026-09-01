"""A settled week, expressed as HRMS's own Shift Schedule rather than as rows.

Once `pipeline.binding` knows a practitioner's week repeats, that week is a
*rule*, and stock HR already has somewhere to put a rule: a `Shift Schedule`
(a shift type, a frequency, and the weekdays it falls on) plus a
`Shift Schedule Assignment` joining it to an employee. HRMS's nightly job then
creates the `Shift Assignment` records from it, and an administrator can review
the rule once instead of auditing several hundred generated rows.

Two shift types means two schedules. A `Shift Schedule` names exactly one
`shift_type`, so a practitioner working mornings on Monday and afternoons on
Thursday needs one schedule for each — which is a fair description of the
practice rather than a workaround.

## What HRMS's frequency actually means, and why only weekly is emitted

`Every N Weeks` is **not** an N-week rota. Reading `create_shifts`, and
confirmed by running it: it repeats *the same* weekday set, working one week in
every N. There is no way to say "week one is Monday to Wednesday, week two is
Thursday and Friday" — which is precisely what this practice's orthodontists
work.

It is also unsound for N > 1. `create_shifts` takes its week boundary from
`create_shifts_after`, and `create_individual_assignment` overwrites that with
the last *shift's* end date rather than the end of a week. One long call is
correct, but the nightly `process_auto_shift_creation` resumes mid-pattern, the
boundary re-anchors, and the cycle collapses. Measured over twelve weeks in
thirty-day chunks, `Every 4 Weeks` produced weeks 0, 4, 4, 5, 8, 9, 10, 11, 12
— by the third month it was firing weekly. `Every Week` is immune, because
`gap` is 0 and the branch that moves the boundary never runs.

So a rota is emitted, because it is real and someone should see it, but it is
emitted **disabled, Inactive and tagged**, with a comment saying why. One
assignment per phase, each anchored a week apart, is the faithful shape and
would work as-is the day `create_shifts` anchors its weeks properly. Until
then those people keep the imported `Shift Assignment` rows, which describe
them correctly.

## Where the import stops and the schedule starts

Generated assignments collide with imported ones — `validate_overlapping_shifts`
throws on any active same-day, same-time pair — so the two must meet exactly
rather than overlap. `create_shifts_after` is set to the build's own
`date_to`: the import owns everything up to and including it, the schedule owns
everything after. Nothing is generated at all while `enabled` is 0, so the
handover happens when an administrator approves the rule, not when the build
runs.
"""

from __future__ import annotations

import logging

import pandas as pd

from .. import settings
from . import binding, location

log = logging.getLogger(__name__)

#: HRMS's `Assignment Rule Day` values, indexed by `date.weekday()`.
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

#: `Shift Schedule.frequency`, indexed by cycle length in weeks.
FREQUENCY = {1: "Every Week", 2: "Every 2 Weeks", 3: "Every 3 Weeks", 4: "Every 4 Weeks"}

#: Tag put on anything that must not be switched on as it stands.
TAG_DO_NOT_ENABLE = "DO NOT ENABLE"

REASON_ROTA = (
	"Detected as a {cycle}-week rota (phase {phase} of {cycle}), which HRMS cannot express: "
	'"Every {cycle} Weeks" repeats one weekday set every {cycle} weeks rather than varying it '
	"per week, and for any frequency above Every Week the nightly job re-anchors the cycle "
	"mid-pattern and collapses it towards weekly. This person's Shift Assignments are imported "
	"directly instead and are correct. Left here as the faithful shape of the rota, for the day "
	"HRMS anchors its weeks properly — do not enable it before then."
)


def _schedule_name(shift_type: str, frequency: str, days: tuple[str, ...]) -> str:
	"""Deterministic docname for a Shift Schedule.

	`Shift Schedule` autonames by prompt, so the name is ours to choose, and a
	name derived from the content is what makes the schedule shareable: every
	practitioner working mornings on Monday, Tuesday and Wednesday lands on one
	record rather than a private copy each. HRMS's own
	`get_or_insert_shift_schedule` dedupes on exactly this triple, by search;
	naming it means the importer gets the same result by lookup.
	"""
	# Three letters, not one: Tuesday and Thursday share an initial, as do
	# Saturday and Sunday, so single letters name two different schedules the
	# same thing and silently merge them.
	initials = "+".join(day[:3] for day in days) if days else "none"
	slug = frequency.replace("Every ", "").replace(" ", "").lower()
	return f"{shift_type} {slug} {initials}"


def _anchor(create_shifts_after, cycle: int, phase: int):
	"""The `create_shifts_after` that puts this phase on the right week.

	`create_shifts` emits its weekday set in the week following
	`create_shifts_after` and then skips `cycle - 1` weeks, so a rota's phases
	are separated by anchoring each one a week further on. Phase is counted off
	`binding`'s fixed Monday, so the anchors agree with the pattern that was
	fitted rather than with whenever this build happened to run.

	A weekly schedule keeps the date it was given: every week matches, and
	moving it forward to the next Monday would leave the days between the end of
	the import and that Monday with no assignments at all.
	"""
	if cycle <= 1 or create_shifts_after is None:
		return create_shifts_after
	first = pd.Timestamp(create_shifts_after).normalize().to_period("W").start_time + pd.Timedelta(weeks=1)
	while ((first - binding._EPOCH).days // 7) % cycle != phase:
		first += pd.Timedelta(weeks=1)
	return str((first - pd.Timedelta(days=1)).date())


def build(
	person_level: pd.DataFrame | None,
	resolved: pd.DataFrame,
	spine: pd.DataFrame,
	*,
	as_of=None,
	create_shifts_after=None,
	excused=None,
) -> dict[str, pd.DataFrame]:
	"""Shift Schedule and Shift Schedule Assignment rows for bound practitioners.

	`resolved` is `pipeline.binding.resolve`'s verdict. Only people it leaves
	binding get a schedule: someone it held back has no settled week to express,
	and someone the profile never made eligible is the optimiser's to schedule.
	"""
	empty = {
		"Shift Schedule": pd.DataFrame(columns=["name", "shift_type", "frequency", "repeat_on_days"]),
		"Shift Schedule Assignment": pd.DataFrame(
			columns=[
				"custom_zawin_key",
				"employee",
				"company",
				"shift_schedule",
				"shift_location",
				"shift_status",
				"enabled",
				"create_shifts_after",
				"zawin_tag",
				"zawin_comment",
			]
		),
	}
	if person_level is None or person_level.empty or resolved.empty:
		return empty

	bound = set(
		resolved.loc[
			resolved["eligible"] & (resolved["binding_override"] != binding.OVERRIDE_NOT_BINDING),
			"personnel_no",
		]
	)
	if not bound:
		log.info("schedules: nobody is bound; no Shift Schedule emitted")
		return empty

	pattern = binding.modal_pattern(person_level, as_of=as_of, excused=excused)
	if pattern.empty:
		log.info("schedules: no shift type on the person-level frame; no Shift Schedule emitted")
		return empty
	pattern = pattern[pattern["personnel_no"].isin(bound)]

	company = settings.get().company
	shift_types = _shift_type_names()
	discipline = spine.set_index("personnel_no")["department"]
	schedules: dict[str, dict] = {}
	assignments: list[dict] = []

	# Branch is part of the grouping, not an afterthought: a Shift Schedule
	# Assignment carries one `shift_location`, and autoshift reads both the
	# branch and the discipline back off it. Somebody who genuinely works two
	# sites therefore gets one assignment per site, which is what they do.
	for (personnel_no, phase, window, branch), days in pattern.groupby(
		["personnel_no", "phase", "window", "branch"], dropna=False
	):
		cycle = int(days["cycle_weeks"].iat[0])
		frequency = FREQUENCY.get(cycle)
		if frequency is None:
			# Longer than binding_max_cycle_weeks can be fitted, so unreachable
			# unless the profile raises it past what HRMS offers.
			log.warning("schedules: %s has a %d-week cycle HRMS cannot name; skipped", personnel_no, cycle)
			continue

		shift_type = shift_types.get(str(window).lower())
		if shift_type is None:
			log.warning("schedules: no Shift Type for window %r; skipped", window)
			continue

		names = tuple(WEEKDAY_NAMES[int(d)] for d in sorted(days["weekday"]))
		name = _schedule_name(shift_type, frequency, names)
		schedules[name] = {
			"name": name,
			"shift_type": shift_type,
			"frequency": frequency,
			"repeat_on_days": names,
		}

		usable = cycle == 1
		assignments.append(
			{
				# Phase is in the key even for a weekly schedule: a person who
				# moves onto a rota must not silently overwrite the single
				# assignment they used to have.
				"custom_zawin_key": f"ssa:{personnel_no}:{cycle}:{phase}:{window}:{branch}",
				"employee": personnel_no,
				"company": company,
				"shift_schedule": name,
				"shift_location": location.location_name(branch, discipline.get(personnel_no)),
				"shift_status": "Active" if usable else "Inactive",
				"enabled": 0,
				"create_shifts_after": _anchor(create_shifts_after, cycle, phase),
				"zawin_tag": "" if usable else TAG_DO_NOT_ENABLE,
				"zawin_comment": "" if usable else REASON_ROTA.format(cycle=cycle, phase=phase + 1),
			}
		)

	out = {
		"Shift Schedule": pd.DataFrame(list(schedules.values()), columns=empty["Shift Schedule"].columns),
		"Shift Schedule Assignment": pd.DataFrame(
			assignments, columns=empty["Shift Schedule Assignment"].columns
		),
	}
	_report(out["Shift Schedule Assignment"], bound)
	return out


def _shift_type_names() -> dict[str, str]:
	"""Lowercased window ("am"/"pm") -> the Shift Type the profile names for it.

	The profile already declares the practice's shift types, and
	`pipeline.assignments` writes `window.upper()` as the Shift Type on every
	imported assignment, so this only has to agree with that.
	"""
	return {str(s["name"]).lower(): s["name"] for s in settings.get().shift_types}


def _report(assignments: pd.DataFrame, bound: set[str]) -> None:
	if assignments.empty:
		log.info("schedules: %d bound people, but no schedule could be built", len(bound))
		return
	blocked = assignments[assignments["zawin_tag"] == TAG_DO_NOT_ENABLE]
	log.info(
		"schedules: %d Shift Schedule Assignment(s) for %d of %d bound people, all disabled pending review",
		len(assignments),
		assignments["employee"].nunique(),
		len(bound),
	)
	if not blocked.empty:
		log.warning(
			"schedules: %d assignment(s) for %d person(s) are rotas HRMS cannot run and are tagged %r; "
			"their Shift Assignments are imported directly instead: %s",
			len(blocked),
			blocked["employee"].nunique(),
			TAG_DO_NOT_ENABLE,
			", ".join(sorted(set(blocked["employee"].astype(str)))),
		)
