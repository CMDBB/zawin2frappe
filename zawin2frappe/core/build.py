"""The build: extract, transform, emit.

One orchestration shared by every entry point — the standalone CLI, the bench
command, and eventually a background job. Only the sink differs, which is the
whole point of the sink protocol: the same records go to CSV files for review
or straight into Frappe documents.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from . import extract, roster
from .db import DERIVED_DIR, write_derived
from .pipeline import (
	assignments,
	calendar,
	discipline,
	employees,
	presence,
	quality,
	scope,
)

log = logging.getLogger(__name__)

TARGETS = ("employees", "assignments", "all")
FULL_DAY_POLICIES = ("single", "split")


@dataclass
class BuildResult:
	"""What a build produced, for the caller to report on."""

	records: dict[str, Any] = field(default_factory=dict)
	unlinked_people: int = 0
	fte_outside_tolerance: int = 0
	notes: dict[str, Any] = field(default_factory=dict)

	def counts(self) -> dict[str, int]:
		return {k: len(v) for k, v in self.records.items()}


def run(
	sink,
	*,
	target: str = "all",
	date_from: str | None = None,
	date_to: str | None = None,
	signal_from: str | None = None,
	signal_to: str | None = None,
	full_day_policy: str = "single",
	write_reports: bool = True,
) -> BuildResult:
	"""Build the records and hand them to `sink`.

	`signal_*` bounds the window used to classify people (which assistants work
	ortho, who is reception); `date_*` bounds the assignments actually emitted.
	They are separate because classification wants as much history as it can
	get, while assignments usually want a recent, complete period.
	"""
	if target not in TARGETS:
		raise ValueError(f"target must be one of {TARGETS}, not {target!r}")
	if full_day_policy not in FULL_DAY_POLICIES:
		raise ValueError(f"full_day_policy must be one of {FULL_DAY_POLICIES}")

	result = BuildResult()

	spine = roster.build_spine()
	columns = roster.resolve_columns(spine)

	review = roster.identity_review(spine, columns)
	result.unlinked_people = len(review)
	if not review.empty and write_reports:
		write_derived(review, "needs_review_identity.csv")
		log.warning(
			"%d roster people have no ZaWin agenda column -> %s",
			len(review),
			DERIVED_DIR / "needs_review_identity.csv",
		)

	annotated = extract.agenda_with_labels(date_from=signal_from, date_to=signal_to)
	spine = discipline.refine(spine, columns, annotated)
	spine = scope.apply(spine, annotated, columns)

	person_level = fte = agenda = None
	if target in ("assignments", "all"):
		from .db import query as _query

		practice = calendar.practice_days(_query)
		agenda = extract.agenda_with_labels(date_from=date_from, date_to=date_to)
		patient = extract.patient_activity(date_from, date_to)
		raw, styles = presence.build(agenda, patient, practice)
		person_level = presence.to_person_level(raw, columns)
		if full_day_policy == "single":
			person_level = presence.collapse_daily(person_level, patient, columns)

		fte = assignments.fte_reconciliation(person_level, spine, styles)
		result.fte_outside_tolerance = int((~fte["within_tolerance"]).sum())
		if write_reports:
			write_derived(fte, "fte_reconciliation.csv")
		if result.fte_outside_tolerance:
			# Reported, never gating: the FTE figure is a current snapshot and
			# staff change their contracted rate, so a deviation is as likely
			# to be real drift as an extraction fault.
			log.info(
				"%d/%d people outside the FTE band (report only)",
				result.fte_outside_tolerance,
				len(fte),
			)

	if target in ("employees", "all"):
		result.records.update(employees.build_all(spine, columns, as_of=date_to))

	if target in ("assignments", "all"):
		shift_rows = assignments.build(person_level, spine)
		result.records["Shift Assignment"] = shift_rows
		if write_reports:
			# Derivation is audit metadata, not a Frappe field — kept alongside
			# so attested and reconstructed rows stay separable without asking
			# for a second custom field.
			write_derived(
				person_level.assign(custom_zawin_key=shift_rows["custom_zawin_key"].values)[
					["custom_zawin_key", "personnel_no", "date", "window", "derivation"]
				],
				"assignment_derivation.csv",
			)
		result.notes["data_quality"] = quality.report(agenda, fte)

	result.notes["signal_window"] = [signal_from, signal_to]
	result.notes["assignment_window"] = [date_from, date_to]
	result.notes["unlinked_people"] = result.unlinked_people
	result.notes["fte_outside_tolerance"] = result.fte_outside_tolerance

	for doctype, rows in result.records.items():
		sink.write(doctype, rows)
	for key, value in result.notes.items():
		if hasattr(sink, "note"):
			sink.note(key, value)
	sink.finalise()
	return result
