"""Unit tests for turning a settled week into an HRMS Shift Schedule.

No database and no practice data: hand-written schedules for `P1`, `P2`, ...,
against the shipped `profiles/example.json`.
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zawin2frappe.core import settings
from zawin2frappe.core.pipeline import binding, schedules

AS_OF = pd.Timestamp("2026-06-29")  # a Monday
EXAMPLE_PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "example.json"

BINDING_SERVICE = "100"
PLAIN_SERVICE = "200"


@pytest.fixture(autouse=True)
def profile():
	settings.use(EXAMPLE_PROFILE)
	yield
	settings.use(EXAMPLE_PROFILE)


def worked(personnel_no, by_week, weeks: int = 40, branch: str = "Main") -> pd.DataFrame:
	"""Person-level half-days. `by_week(i)` -> [(weekday, window), ...]."""
	rows = []
	for i in range(weeks):
		monday = AS_OF - pd.Timedelta(weeks=i)
		for weekday, window in by_week(i):
			rows.append(
				{
					"personnel_no": personnel_no,
					"date": monday + pd.Timedelta(days=weekday),
					"window": window,
					"branch": branch,
				}
			)
	return pd.DataFrame(rows, columns=["personnel_no", "date", "window", "branch"])


def spine(**people) -> pd.DataFrame:
	"""{personnel_no: (service_no, department)}."""
	return pd.DataFrame(
		{
			"personnel_no": list(people),
			"service_no": [v[0] for v in people.values()],
			"department": [v[1] for v in people.values()],
			"schedulable": [True] * len(people),
		}
	).astype({"schedulable": "bool"})


def emit(frame, people, **kwargs):
	resolved = binding.resolve(spine(**people), frame, as_of=AS_OF)
	return schedules.build(
		frame, resolved, spine(**people), as_of=AS_OF, create_shifts_after="2026-07-31", **kwargs
	)


# --- the weekly case, which is the one HRMS can actually run ---------------


def test_a_settled_week_becomes_one_schedule_per_shift_type():
	"""A Shift Schedule names one shift type, so mornings and afternoons are two
	schedules — which is a fair description, not a workaround."""
	frame = worked("P1", lambda i: [(0, "am"), (1, "am"), (3, "pm")])
	out = emit(frame, {"P1": (BINDING_SERVICE, "Omnipractice")})

	assert set(out["Shift Schedule"]["shift_type"]) == {"AM", "PM"}
	assert set(out["Shift Schedule"]["frequency"]) == {"Every Week"}
	by_type = out["Shift Schedule"].set_index("shift_type")["repeat_on_days"]
	assert by_type["AM"] == ("Monday", "Tuesday")
	assert by_type["PM"] == ("Thursday",)
	assert len(out["Shift Schedule Assignment"]) == 2


def test_everything_is_emitted_disabled_for_review():
	frame = worked("P1", lambda i: [(0, "am"), (1, "am")])
	assignments = emit(frame, {"P1": (BINDING_SERVICE, "Omnipractice")})["Shift Schedule Assignment"]
	assert (assignments["enabled"] == 0).all()


def test_the_schedule_takes_over_where_the_import_stops():
	"""Generated and imported assignments throw on overlap, so the handover date
	is the end of the import window exactly."""
	frame = worked("P1", lambda i: [(0, "am")])
	assignments = emit(frame, {"P1": (BINDING_SERVICE, "Omnipractice")})["Shift Schedule Assignment"]
	assert set(assignments["create_shifts_after"]) == {"2026-07-31"}


def test_a_shift_location_is_named_so_the_optimiser_can_read_it_back():
	"""autoshift reads branch and discipline off `shift_location`; without one a
	generated assignment is invisible to it."""
	frame = worked("P1", lambda i: [(0, "am")], branch="Annexe")
	assignments = emit(frame, {"P1": (BINDING_SERVICE, "Omnipractice")})["Shift Schedule Assignment"]
	assert list(assignments["shift_location"]) == ["Annexe - Omnipractice"]


def test_two_sites_are_two_assignments():
	"""One assignment carries one location, and someone working both sites really
	does work both sites."""
	frame = pd.concat(
		[
			worked("P1", lambda i: [(0, "am")], branch="Main"),
			worked("P1", lambda i: [(2, "am")], branch="Annexe"),
		],
		ignore_index=True,
	)
	assignments = emit(frame, {"P1": (BINDING_SERVICE, "Omnipractice")})["Shift Schedule Assignment"]
	assert set(assignments["shift_location"]) == {"Main - Omnipractice", "Annexe - Omnipractice"}


def test_two_people_on_the_same_pattern_share_one_schedule():
	"""The schedule is the rule; the assignment is who follows it."""
	frame = pd.concat(
		[
			worked("P1", lambda i: [(0, "am"), (1, "am")]),
			worked("P2", lambda i: [(0, "am"), (1, "am")]),
		],
		ignore_index=True,
	)
	out = emit(frame, {"P1": (BINDING_SERVICE, "Omnipractice"), "P2": (BINDING_SERVICE, "Omnipractice")})
	assert len(out["Shift Schedule"]) == 1
	assert len(out["Shift Schedule Assignment"]) == 2


def test_tuesday_and_thursday_do_not_collide():
	"""Regression: naming a schedule by weekday initials merged Tuesday with
	Thursday and Saturday with Sunday, silently putting people on the wrong one."""
	frame = pd.concat(
		[
			worked("P1", lambda i: [(1, "am")]),  # Tuesday
			worked("P2", lambda i: [(3, "am")]),  # Thursday
			worked("P3", lambda i: [(5, "am")]),  # Saturday
			worked("P4", lambda i: [(6, "am")]),  # Sunday
		],
		ignore_index=True,
	)
	out = emit(frame, {p: (BINDING_SERVICE, "Omnipractice") for p in ("P1", "P2", "P3", "P4")})
	assert len(out["Shift Schedule"]) == 4
	assert out["Shift Schedule"]["name"].is_unique
	days = {r["name"]: r["repeat_on_days"] for _, r in out["Shift Schedule"].iterrows()}
	assert len(set(days.values())) == 4


# --- the rota case, which it cannot ---------------------------------------


def test_a_rota_is_emitted_but_marked_unusable():
	"""HRMS's "Every N Weeks" repeats one weekday set every N weeks rather than
	varying it per week, and above Every Week its nightly job re-anchors the
	cycle and collapses it. The rota is still emitted, because it is real and
	someone should see it, but it must not be switched on."""
	pattern = {0: [(0, "am"), (1, "am")], 1: [(3, "am")], 2: [], 3: []}
	frame = worked("P1", lambda i: pattern[i % 4], weeks=48)
	out = emit(frame, {"P1": (BINDING_SERVICE, "Omnipractice")})

	assignments = out["Shift Schedule Assignment"]
	assert len(assignments) >= 2
	assert (assignments["shift_status"] == "Inactive").all()
	assert (assignments["enabled"] == 0).all()
	assert (assignments["zawin_tag"] == schedules.TAG_DO_NOT_ENABLE).all()
	assert all("do not enable" in c.lower() for c in assignments["zawin_comment"])
	assert set(out["Shift Schedule"]["frequency"]) == {"Every 2 Weeks"}


def test_rota_phases_are_anchored_a_week_apart():
	"""Each phase fires in its own week, which is the faithful shape and what
	would work the day HRMS anchors its weeks properly."""
	pattern = {0: [(0, "am")], 1: [(1, "am")], 2: [(2, "am")], 3: [(3, "am")]}
	frame = worked("P1", lambda i: pattern[i % 4], weeks=48)
	assignments = emit(frame, {"P1": (BINDING_SERVICE, "Omnipractice")})["Shift Schedule Assignment"]

	anchors = sorted(pd.Timestamp(d) for d in set(assignments["create_shifts_after"]))
	assert len(anchors) == 4
	gaps = {(b - a).days for a, b in itertools.pairwise(anchors)}
	assert gaps == {7}


def test_a_weekly_schedule_keeps_the_handover_date_rather_than_the_next_monday():
	"""Moving a weekly anchor forward would leave the days between the end of the
	import and that Monday with no assignments at all."""
	frame = worked("P1", lambda i: [(0, "am")])
	assignments = emit(frame, {"P1": (BINDING_SERVICE, "Omnipractice")})["Shift Schedule Assignment"]
	assert list(assignments["create_shifts_after"]) == ["2026-07-31"]


# --- who gets one at all ---------------------------------------------------


def test_an_unsettled_holder_gets_no_schedule():
	"""`binding` held them back, so there is no settled week to express."""
	frame = worked("P1", lambda i: [((i) % 5, "am"), ((i + 1) % 5, "am"), ((i + 2) % 5, "am")])
	out = emit(frame, {"P1": (BINDING_SERVICE, "Omnipractice")})
	assert out["Shift Schedule Assignment"].empty


def test_an_ineligible_job_gets_no_schedule():
	"""Everyone the profile does not make binding is the optimiser's to schedule."""
	frame = worked("P1", lambda i: [(0, "am"), (1, "am")])
	out = emit(frame, {"P1": (PLAIN_SERVICE, "Omnipractice")})
	assert out["Shift Schedule Assignment"].empty
	assert out["Shift Schedule"].empty


def test_a_build_with_no_history_emits_nothing():
	assert schedules.build(None, pd.DataFrame(), spine(), as_of=AS_OF)["Shift Schedule"].empty


def test_a_frame_without_shift_types_emits_nothing():
	"""Nothing can be said about which shift type a schedule is for."""
	frame = worked("P1", lambda i: [(0, "am")]).drop(columns=["window"])
	out = emit(frame, {"P1": (BINDING_SERVICE, "Omnipractice")})
	assert out["Shift Schedule"].empty


def test_every_assignment_points_at_a_schedule_that_was_emitted():
	frame = pd.concat(
		[
			worked("P1", lambda i: [(0, "am"), (3, "pm")]),
			worked("P2", lambda i: [(1, "am"), (2, "am")], branch="Annexe"),
		],
		ignore_index=True,
	)
	out = emit(frame, {"P1": (BINDING_SERVICE, "Omnipractice"), "P2": (BINDING_SERVICE, "Orthodontics")})
	assert set(out["Shift Schedule Assignment"]["shift_schedule"]) <= set(out["Shift Schedule"]["name"])
	assert out["Shift Schedule Assignment"]["custom_zawin_key"].is_unique
