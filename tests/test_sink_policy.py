"""What a re-import is allowed to overwrite — `core.sinks.policy`.

No Frappe and no database: the rules are decisions about reconciliation, which is
why they live apart from the sink that applies them.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zawin2frappe.core.sinks.policy import is_blank, needs_replacement, preserve_existing

# --- what counts as saying nothing ----------------------------------------


def test_blank_is_absence_not_emptiness():
	assert is_blank(None)
	assert is_blank("")
	assert is_blank("   ")
	assert not is_blank("AB")


def test_zero_is_a_decision_not_a_blank():
	"""A Check of 0 is "not binding"; swallowing it would make that unwritable."""
	assert not is_blank(0)
	assert not is_blank(0.0)
	assert not is_blank(False)


# --- preserving what the extract cannot see -------------------------------


def test_a_blank_never_erases_a_curated_value():
	current = {"employee_number": "P1", "custom_initials": "AB"}
	payload = {"employee_number": "P1", "custom_initials": None}
	assert preserve_existing(current, payload)["custom_initials"] == "AB"


def test_a_value_the_extract_does_have_still_wins():
	current = {"custom_initials": "AB"}
	assert preserve_existing(current, {"custom_initials": "CD"})["custom_initials"] == "CD"


def test_a_blank_over_a_blank_stays_blank():
	assert preserve_existing({"custom_initials": ""}, {"custom_initials": None}) == {"custom_initials": None}


def test_zero_overwrites_because_it_is_not_blank():
	assert preserve_existing({"custom_unconfirmed": 1}, {"custom_unconfirmed": 0}) == {
		"custom_unconfirmed": 0
	}


def test_a_new_record_keeps_its_payload_untouched():
	payload = {"employee_number": "P1", "custom_initials": None}
	assert preserve_existing(None, payload) == payload
	assert preserve_existing({}, payload) == payload


def test_a_field_the_stored_row_never_had_is_left_alone():
	"""`_existing` reads back only the comparison fields, so absence means unknown."""
	assert preserve_existing({"employee_number": "P1"}, {"custom_initials": None}) == {
		"custom_initials": None
	}


# --- when a submitted record has to be rebuilt ----------------------------


def test_a_change_frappe_freezes_at_submit_needs_a_new_record():
	assert needs_replacement({"start_date"}, {"custom_scheduling_role"})


def test_a_change_confined_to_editable_fields_is_written_in_place():
	"""A corrected role must not cost a Shift Assignment its name, links and comments."""
	assert not needs_replacement({"custom_scheduling_role"}, {"custom_scheduling_role"})


def test_nothing_changed_needs_nothing_rebuilt():
	assert not needs_replacement(set(), set())
