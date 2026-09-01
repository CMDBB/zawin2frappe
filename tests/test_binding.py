"""Unit tests for settled-schedule detection. No database, no Frappe, no
practice data — every schedule here is written by hand, and people are `P1`,
`P2`, ... as in autoshift's own suite.

`pipeline.binding` reads only the collapsed person-level frame and the profile,
so the whole measure can be exercised without restoring ZaWin. The profile is
`profiles/example.json`, which doubles as a check that the shipped template
still parses and still carries the binding thresholds it documents.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zawin2frappe.core import settings
from zawin2frappe.core.pipeline import binding

#: A Monday, so week starts line up with `to_period("W")`.
AS_OF = pd.Timestamp("2026-06-29")
EXAMPLE_PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "example.json"

#: example.json marks these binding; 120 (hygienist) and 200 (assistant) not.
BINDING_SERVICE = "100"
PLAIN_SERVICE = "200"


@pytest.fixture(autouse=True)
def profile():
	settings.use(EXAMPLE_PROFILE)
	yield
	settings.use(EXAMPLE_PROFILE)


def shifts(personnel_no: str, weekdays_by_week, weeks: int = 40, end=AS_OF) -> pd.DataFrame:
	"""A person-level frame: `weekdays_by_week(i)` gives week i's weekdays.

	Week 0 is the most recent; weeks run backwards from `end`, so a schedule
	built here decays exactly as a real one would.
	"""
	rows = []
	for i in range(weeks):
		monday = end - pd.Timedelta(weeks=i)
		for weekday in weekdays_by_week(i):
			rows.append({"personnel_no": personnel_no, "date": monday + pd.Timedelta(days=weekday)})
	return pd.DataFrame(rows, columns=["personnel_no", "date"])


def score_of(frame: pd.DataFrame, personnel_no: str = "P1") -> pd.Series:
	out = binding.weekly_settledness(frame, as_of=AS_OF).set_index("personnel_no")
	return out.loc[personnel_no]


def spine(**services: str) -> pd.DataFrame:
	"""A minimal schedulable spine: {personnel_no: service_no}."""
	return pd.DataFrame(
		{
			"personnel_no": list(services),
			"service_no": list(services.values()),
			"schedulable": [True] * len(services),
		}
	).astype({"personnel_no": "object", "service_no": "object", "schedulable": "bool"})


# --- the measure -----------------------------------------------------------


def test_an_identical_week_every_week_is_perfectly_settled():
	frame = shifts("P1", lambda i: [0, 1, 3])
	row = score_of(frame)
	assert row["settledness"] == 1.0
	assert row["cycle_weeks"] == 1
	assert row["modal_week"] == "MT.T..."


def test_a_week_that_never_repeats_is_not_settled():
	# Three days a week, but never the same three: a rotation long enough that no
	# cycle up to binding_max_cycle_weeks can describe it.
	frame = shifts("P1", lambda i: [(i) % 5, (i + 1) % 5, (i + 2) % 5])
	assert score_of(frame)["settledness"] < settings.get().threshold("binding_settled_min", 0.75)


def test_one_day_off_now_and_then_barely_moves_the_score():
	frame = shifts("P1", lambda i: [0, 1, 2, 3] if i % 5 else [0, 1, 2])
	assert score_of(frame)["settledness"] > 0.9


def test_holiday_weeks_are_leave_not_drift():
	"""A fortnight away must not read as a fortnight of a different schedule."""
	steady = shifts("P1", lambda i: [0, 1, 2, 3])
	with_leave = shifts("P1", lambda i: [] if i in (4, 5) else [0, 1, 2, 3])
	assert score_of(with_leave)["settledness"] == score_of(steady)["settledness"] == 1.0
	assert score_of(with_leave)["weeks"] == score_of(steady)["weeks"] - 2


# --- rotas longer than a week ----------------------------------------------


def test_a_two_on_two_off_rota_is_settled_not_erratic():
	"""The orthodontists' case: two weeks on, two off, on a four-week cycle.

	Scored weekly this is one of the *least* regular schedules in the practice;
	it is in fact one of the most regular, and reading it as erratic would take
	binding away from exactly the people it exists for.

	It is reported as a **two**-week cycle, not a four-week one, and that is
	right: the off weeks are dropped as leave, so the weeks that remain simply
	alternate, and two phases describe them completely. The shortest sufficient
	period wins because a longer one has to pay for its extra phases.
	"""
	pattern = {0: [0, 1, 2], 1: [3, 4], 2: [], 3: []}
	frame = shifts("P1", lambda i: pattern[i % 4], weeks=48)
	row = score_of(frame)
	assert row["cycle_weeks"] == 2
	assert row["settledness"] > 0.95
	# Read weekly instead, the same person would be held back as unsettled —
	# which is the outcome the cycle fit exists to prevent.
	settings.get().thresholds["binding_max_cycle_weeks"] = 1
	assert score_of(frame)["settledness"] < settings.get().threshold("binding_settled_min", 0.75)


def test_a_rota_that_needs_four_phases_gets_four():
	"""Four worked weeks, all different: no shorter period can describe them."""
	pattern = {0: [0, 1], 1: [2, 3], 2: [0, 4], 3: [1, 3]}
	frame = shifts("P1", lambda i: pattern[i % 4], weeks=48)
	row = score_of(frame)
	assert row["cycle_weeks"] == 4
	assert row["settledness"] == 1.0
	# Phase 0 is counted off a fixed Monday, not off the start of the data, so
	# which phase is reported first is a property of the calendar.
	assert set(row["modal_week"].split("/")) == {"MT.....", "..WT...", "M...F..", ".T.T..."}


def test_an_alternating_fortnight_reads_as_a_two_week_cycle():
	frame = shifts("P1", lambda i: [0, 1, 2, 4] if i % 2 == 0 else [0, 1, 2], weeks=40)
	row = score_of(frame)
	assert row["cycle_weeks"] == 2
	assert row["settledness"] > 0.95
	assert set(row["modal_week"].split("/")) == {"MTW.F..", "MTW...."}


def test_a_weekly_schedule_is_not_reported_as_a_rota():
	"""A longer period can always fit at least as well, so without the penalty
	every schedule would come back as a four-week rota."""
	frame = shifts("P1", lambda i: [0, 1, 2, 3])
	assert score_of(frame)["cycle_weeks"] == 1


def test_a_rota_needs_enough_weeks_per_phase_to_be_believed():
	pattern = {0: [0, 1, 2, 4], 1: [0, 1, 2], 2: [], 3: []}
	frame = shifts("P1", lambda i: pattern[i % 4], weeks=48)
	settings.get().thresholds["binding_min_phase_weeks"] = 40
	assert score_of(frame)["cycle_weeks"] == 1


# --- the window ------------------------------------------------------------


def test_a_schedule_changed_long_ago_is_judged_on_the_one_in_force_now():
	"""Four half-lives back, the old regime is gone from the reading entirely."""
	frame = shifts("P1", lambda i: [0, 1, 2] if i < 40 else [2, 3, 4], weeks=52)
	row = score_of(frame)
	assert row["modal_week"] == "MTW...."
	assert row["settledness"] > 0.9


def test_a_schedule_changed_recently_reads_as_not_yet_settled():
	"""One half-life back, the old regime still has half a vote. The new week is
	already the modal one, but the person is held back — which is correct on the
	evidence and is exactly the case the curated `binding_scope` file exists for,
	when everyone in the building knows the new week is final."""
	frame = shifts("P1", lambda i: [0, 1, 2] if i < 13 else [2, 3, 4], weeks=45)
	row = score_of(frame)
	assert row["modal_week"] == "MTW...."
	assert row["settledness"] < settings.get().threshold("binding_settled_min", 0.75)


def test_too_little_history_is_not_a_low_score():
	"""Absent, not zero: a recent hire has no established week *yet*, which is a
	different finding from an irregular one and gets a different reason."""
	frame = shifts("P1", lambda i: [0, 1, 2], weeks=4)
	assert binding.weekly_settledness(frame, as_of=AS_OF).empty


def test_the_booked_ahead_agenda_is_not_evidence_for_itself():
	"""The agenda is booked a year out. Anchoring on a future date would score
	people on the very pattern being judged."""
	future = shifts("P1", lambda i: [0, 1, 2], weeks=40, end=AS_OF + pd.Timedelta(weeks=60))
	assert binding.weekly_settledness(future, as_of=AS_OF + pd.Timedelta(weeks=60)).empty


# --- turning a score into an override --------------------------------------


def test_an_ineligible_job_is_never_touched():
	frame = shifts("P1", lambda i: [(i) % 5, (i + 1) % 5])
	out = binding.resolve(spine(P1=PLAIN_SERVICE), frame, as_of=AS_OF).set_index("personnel_no")
	assert out.loc["P1", "binding_override"] == binding.OVERRIDE_INHERIT
	assert not out.loc["P1", "eligible"]


def test_a_settled_holder_inherits_the_roles_flag():
	frame = shifts("P1", lambda i: [0, 1, 3])
	out = binding.resolve(spine(P1=BINDING_SERVICE), frame, as_of=AS_OF).set_index("personnel_no")
	assert out.loc["P1", "binding_override"] == binding.OVERRIDE_INHERIT
	assert out.loc["P1", "eligible"]


def test_an_unsettled_holder_is_held_back():
	frame = shifts("P1", lambda i: [(i) % 5, (i + 1) % 5, (i + 2) % 5])
	out = binding.resolve(spine(P1=BINDING_SERVICE), frame, as_of=AS_OF).set_index("personnel_no")
	assert out.loc["P1", "binding_override"] == binding.OVERRIDE_NOT_BINDING


def test_a_holder_with_no_history_is_held_back():
	frame = shifts("P2", lambda i: [0, 1, 2])
	out = binding.resolve(spine(P1=BINDING_SERVICE, P2=BINDING_SERVICE), frame, as_of=AS_OF)
	out = out.set_index("personnel_no")
	assert out.loc["P1", "binding_override"] == binding.OVERRIDE_NOT_BINDING
	assert "too little history" in out.loc["P1", "reason"]


def test_a_build_with_no_history_at_all_binds_nobody_unchecked():
	"""`target="employees"` has no person-level frame. The roles still carry the
	profile's flag, so silently leaving every override blank would bind everyone
	on no evidence."""
	out = binding.resolve(spine(P1=BINDING_SERVICE), None, as_of=AS_OF).set_index("personnel_no")
	assert out.loc["P1", "binding_override"] == binding.OVERRIDE_INHERIT
	assert "not checked" in out.loc["P1", "reason"]


def test_the_measure_can_only_ever_take_binding_away():
	"""Nothing here may promote someone the profile did not make eligible."""
	frame = pd.concat(
		[shifts("P1", lambda i: [0, 1, 2]), shifts("P2", lambda i: [0, 1, 2])], ignore_index=True
	)
	out = binding.resolve(spine(P1=BINDING_SERVICE, P2=PLAIN_SERVICE), frame, as_of=AS_OF)
	assert binding.OVERRIDE_BINDING not in set(out["binding_override"])


def test_no_binding_service_means_the_feature_is_off():
	for service in settings.get().services.values():
		object.__setattr__(service, "assignments_binding", False)
	frame = shifts("P1", lambda i: [(i) % 5, (i + 1) % 5])
	out = binding.resolve(spine(P1=BINDING_SERVICE), frame, as_of=AS_OF).set_index("personnel_no")
	assert binding.binding_services() == set()
	assert out.loc["P1", "binding_override"] == binding.OVERRIDE_INHERIT


# --- curated overrides -----------------------------------------------------


def test_a_curated_decision_beats_the_measurement(tmp_path):
	csv = tmp_path / "binding_scope.csv"
	csv.write_text("personnel_no,binding_override,reason\nP1,Binding,new week is final\n")
	settings.get().overrides["binding_scope"] = str(csv)
	frame = shifts("P1", lambda i: [(i) % 5, (i + 1) % 5, (i + 2) % 5])
	out = binding.resolve(spine(P1=BINDING_SERVICE), frame, as_of=AS_OF).set_index("personnel_no")
	assert out.loc["P1", "binding_override"] == binding.OVERRIDE_BINDING
	assert out.loc["P1", "reason"] == "curated override"


def test_an_unreadable_override_value_is_refused(tmp_path):
	csv = tmp_path / "binding_scope.csv"
	csv.write_text("personnel_no,binding_override,reason\nP1,yes,typo\n")
	with pytest.raises(ValueError, match="binding_override must be one of"):
		binding.load_overrides(csv)


def test_the_shipped_example_profile_documents_every_threshold_used():
	thresholds = settings.get().thresholds
	for key in (
		"binding_settled_min",
		"binding_half_life_weeks",
		"binding_lookback_weeks",
		"binding_min_weeks",
		"binding_max_cycle_weeks",
		"binding_cycle_penalty",
		"binding_min_phase_weeks",
	):
		assert key in thresholds, key


def test_the_example_profile_marks_at_least_one_service_binding():
	assert binding.binding_services()


def test_dates_are_read_whether_they_arrive_as_dates_or_timestamps():
	frame = shifts("P1", lambda i: [0, 1, 2])
	frame["date"] = frame["date"].dt.date
	assert score_of(frame)["settledness"] == 1.0


def test_an_empty_frame_yields_no_scores_rather_than_failing():
	empty = pd.DataFrame(columns=["personnel_no", "date"])
	assert binding.weekly_settledness(empty, as_of=AS_OF).empty
	assert binding.resolve(spine(), empty, as_of=AS_OF).empty


def test_a_date_type_that_is_not_a_datetime_is_still_grouped_by_week():
	frame = pd.DataFrame(
		[
			{"personnel_no": "P1", "date": datetime.date(2026, 6, 29) - datetime.timedelta(weeks=i, days=-d)}
			for i in range(30)
			for d in (0, 1, 2)
		]
	)
	assert score_of(frame)["settledness"] == 1.0


# --- leave recorded in the slot the shift would have taken ------------------


def agenda(personnel_no: str, days, category: str = "Holiday") -> pd.DataFrame:
	"""An `annotated`-shaped frame of absence rows, as `excused_days` reads it."""
	return pd.DataFrame(
		[{"FK_Behandler": 1, "Datum": day, "category": category} for day in days],
		columns=["FK_Behandler", "Datum", "category"],
	)


COLUMNS = pd.DataFrame([{"behandler_id": 1, "personnel_no": "P1"}])


def leave_on(weeks, weekday: int, personnel_no: str = "P1", category: str = "Holiday"):
	days = [AS_OF - pd.Timedelta(weeks=i) + pd.Timedelta(days=weekday) for i in weeks]
	return binding.excused_days(agenda(personnel_no, days, category), COLUMNS)


def test_leave_neither_creates_a_pattern_nor_breaks_one():
	"""The practice records leave where the shift would have been, so a day off
	sick says nothing about the week rather than saying they did not work it."""
	away_weeks = range(1, 9)
	frame = shifts("P1", lambda i: [0, 1, 2] if i in away_weeks else [0, 1, 2, 3])
	assert score_of(frame)["settledness"] < 0.95
	excused = leave_on(away_weeks, weekday=3)
	scored = binding.weekly_settledness(frame, as_of=AS_OF, excused=excused).set_index("personnel_no")
	assert scored.loc["P1", "settledness"] == 1.0
	assert scored.loc["P1", "modal_week"] == "MTWT..."


def test_a_weekday_only_ever_excused_still_counts_as_worked():
	"""Someone away every Friday of the measured period has not stopped working
	Fridays; the weeks that were excused simply do not get a vote."""
	frame = shifts("P1", lambda i: [0, 1, 4] if i % 2 else [0, 1])
	excused = leave_on([i for i in range(40) if i % 2 == 0], weekday=4)
	scored = binding.weekly_settledness(frame, as_of=AS_OF, excused=excused).set_index("personnel_no")
	assert scored.loc["P1", "modal_week"] == "MT..F.."
	assert scored.loc["P1", "settledness"] == 1.0


def test_a_day_both_worked_and_marked_absent_counts_as_worked():
	"""A shift is the stronger evidence; an excused day is one with nothing on it."""
	frame = shifts("P1", lambda i: [0, 1, 2])
	excused = leave_on(range(40), weekday=2)
	scored = binding.weekly_settledness(frame, as_of=AS_OF, excused=excused).set_index("personnel_no")
	assert scored.loc["P1", "modal_week"] == "MTW...."


def test_only_the_profiles_own_categories_are_excused():
	"""Keyed on the agenda category, never on the label: the generic "away"
	banner covers whole stretches of calendar and excusing it would blank out
	most of the week."""
	frame = shifts("P1", lambda i: [0, 1, 2] if i < 8 else [0, 1, 2, 3])
	assert binding.excused_days(agenda("P1", [AS_OF], "Absent"), COLUMNS).empty
	assert not binding.excused_days(agenda("P1", [AS_OF], "Holiday"), COLUMNS).empty
	assert (
		score_of(frame)["settledness"]
		== binding.weekly_settledness(
			frame, as_of=AS_OF, excused=binding.excused_days(agenda("P1", [AS_OF], "Absent"), COLUMNS)
		)
		.set_index("personnel_no")
		.loc["P1", "settledness"]
	)


def test_a_profile_that_excuses_nothing_behaves_as_before():
	object.__setattr__(settings.get(), "excused_categories", ())
	assert binding.excused_days(agenda("P1", [AS_OF], "Holiday"), COLUMNS).empty


def test_an_alternating_friday_is_found_as_a_fortnightly_rota():
	"""The practice's own chart has VENDREDI SEM. PAIRE / IMPAIRE — a Friday
	worked on alternate weeks is a settled fortnight, not an unreliable week."""
	frame = shifts("P1", lambda i: [0, 1, 2, 3, 4] if i % 2 else [0, 1, 2, 3], weeks=48)
	row = score_of(frame)
	assert row["cycle_weeks"] == 2
	assert row["settledness"] == 1.0
	assert set(row["modal_week"].split("/")) == {"MTWTF..", "MTWT..."}
