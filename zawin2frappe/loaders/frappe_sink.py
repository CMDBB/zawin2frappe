"""Write records straight into Frappe documents.

The alternative — emitting CSVs for Data Import — works, but fights the tool:
Data Import refuses doctypes with `allow_import = 0` (Shift Type and Shift
Location among them), matches updates only on an `ID` column the extract cannot
know in advance, and surfaces failures one file at a time. Creating documents
directly avoids all of it, resolves links in-process, and gives a per-row error.

Re-run semantics, chosen deliberately:

  upsert per record   Each row is found by its natural key. Absent -> insert.
                      Present and identical -> skip. Present and changed ->
                      update, or for a submitted document, cancel and recreate,
                      because `shift_type` and `start_date` cannot be edited
                      after submit.
  vanished records    A record in Frappe that the extract no longer produces is
                      left alone. The loader never cancels on absence: an
                      extraction fault must not be able to wipe real
                      assignments.
"""

from __future__ import annotations

import contextlib
import datetime
import logging
import re
from collections import Counter

import frappe
import pandas as pd

log = logging.getLogger(__name__)

#: Field that identifies an existing record, per doctype. `Shift Assignment`
#: autonames as a series and so carries the custom key; the rest are named
#: after a field they already have.
KEY_FIELD = {
	"Department": "department_name",
	"Designation": "designation_name",
	"Branch": "branch",
	"Shift Type": "name",
	"Shift Location": "location_name",
	"Employee": "employee_number",
	"Shift Assignment": "custom_zawin_key",
	"Scheduling Role": "role_name",
	# Deterministic autoname (format:{employee}-{scheduling_role}), precomputed
	# by pipeline.roles to match exactly, so it can double as the upsert key.
	"Employee Scheduling Role": "name",
	# Named after its own content by pipeline.schedules, so the name IS the key.
	"Shift Schedule": "name",
	"Shift Schedule Assignment": "custom_zawin_key",
}

#: Child tables written from a plain tuple of values: doctype -> {table
#: fieldname: the child field each value fills}. They are never *compared*,
#: because the only one here belongs to a Shift Schedule whose docname already
#: encodes its content — a name match is a content match.
TABLE_FIELDS = {"Shift Schedule": {"repeat_on_days": "day"}}

#: Columns that are instructions to this loader rather than document fields.
#: Applied after the document exists, since both need its name.
ANNOTATIONS = ("zawin_tag", "zawin_comment")

#: Doctype -> the field that names a Department, needing the same "ERPNext
#: appends the company abbreviation" resolution as Employee.department.
DEPARTMENT_FIELD = {
	"Employee": "department",
	"Scheduling Role": "discipline",
	"Shift Location": "custom_discipline",
}

#: Doctypes to submit after insert.
SUBMIT = {"Shift Assignment", "Shift Schedule"}

#: Fieldtypes Frappe stores as a number and cannot leave NULL: writing None
#: lands as 0, so None and 0 must compare equal or the record reports as
#: changed on every single run.
NUMERIC_FIELDTYPES = {"Float", "Int", "Currency", "Percent", "Check"}

#: Fields never compared when deciding whether a record changed — either
#: Frappe's own bookkeeping or values the extract does not own.
IGNORE_ON_COMPARE = {"docstatus", "name", "owner", "creation", "modified"}

CHUNK = 500


@contextlib.contextmanager
def bulk_load_flags():
	"""Quieten Frappe's per-document side effects for the duration of a load.

	Document events across hrms, erpnext and any custom app enqueue background
	work per save. At ~13k documents that floods the queue — a single run left
	757 jobs pending on a bench with no workers running — and none of it is
	wanted for a migration. `in_import` is the flag Frappe's own importer uses.
	"""
	flags = frappe.flags
	previous = {k: flags.get(k) for k in ("in_import", "mute_emails", "mute_messages")}
	flags.in_import = True
	flags.mute_emails = True
	flags.mute_messages = True
	try:
		yield
	finally:
		for key, value in previous.items():
			flags[key] = value


def _clean(value):
	"""DataFrame value -> something Frappe will accept."""
	if value is None or (not isinstance(value, str | bool) and pd.isna(value)):
		return None
	if isinstance(value, pd.Timestamp):
		return value.date()
	if hasattr(value, "item"):  # numpy scalar
		return value.item()
	return value


def _has_comment(doc, text: str) -> bool:
	"""Whether this exact comment is already on the document.

	Every build re-runs, and a comment is append-only: without this the same
	explanation accumulates one copy per import.
	"""
	return bool(
		frappe.db.exists(
			"Comment",
			{
				"reference_doctype": doc.doctype,
				"reference_name": doc.name,
				"comment_type": "Comment",
				"content": text,
			},
		)
	)


def _comparable(value):
	"""Normalise a value for change detection.

	Frappe returns Time fields as `timedelta`, whose str() drops the leading
	zero — "7:00:00" against the "07:00:00" we wrote. Comparing those naively
	reports a change on every run, which buries the changes that are real.

	Unset is one value, not two. Frappe stores an empty Select as NULL or as ""
	depending on the column, and the extract emits "" for "no override"; without
	folding them together every blank optional field reports as changed on every
	run, which is the same noise in a different place.
	"""
	value = _clean(value)
	if value is None:
		return ""
	if isinstance(value, datetime.timedelta):
		total = int(value.total_seconds())
		return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"
	if isinstance(value, datetime.time):
		return value.strftime("%H:%M:%S")
	if isinstance(value, str) and re.fullmatch(r"\d:\d{2}:\d{2}", value):
		return "0" + value
	return str(value)


class FrappeDocSink:
	"""Sink that creates and reconciles Frappe documents."""

	def __init__(self, *, submit: bool = True, dry_run: bool = False):
		self.submit = submit
		self.dry_run = dry_run
		self.stats: dict[str, Counter] = {}
		self.errors: list[dict] = []
		self.notes: dict = {}
		#: department_name -> actual docname, learned while writing Departments
		self._departments: dict[str, str] = {}
		self._numeric_cache: dict[str, set[str]] = {}

	# -- lookup ------------------------------------------------------------

	def _existing(self, doctype: str, key_field: str, keys: list, fields: list) -> dict:
		"""Map key -> stored row, for keys already in Frappe.

		Fetched in chunks, and carrying the comparison fields with it. Doing the
		comparison from this one result rather than re-reading each document is
		what keeps a 13k-row assignment run to seconds: the first version issued
		one `get_value` per row and took six minutes.
		"""
		wanted = ["name", "docstatus", key_field]
		wanted += [f for f in fields if f not in wanted and f not in IGNORE_ON_COMPARE]
		found: dict = {}
		keys = [k for k in keys if k is not None]
		for i in range(0, len(keys), CHUNK):
			batch = keys[i : i + CHUNK]
			for row in frappe.get_all(
				doctype,
				filters={key_field: ["in", batch]},
				fields=wanted,
				limit_page_length=0,
			):
				found[row[key_field]] = row
		return found

	def _numeric_fields(self, doctype: str) -> set[str]:
		if doctype not in self._numeric_cache:
			meta = frappe.get_meta(doctype)
			self._numeric_cache[doctype] = {
				f.fieldname for f in meta.fields if f.fieldtype in NUMERIC_FIELDTYPES
			}
		return self._numeric_cache[doctype]

	def _differs(self, doctype: str, current: dict, payload: dict) -> bool:
		"""Whether the stored row disagrees with what we would write.

		Only over the fields `_existing` was able to read back: a child table is
		not a column and an annotation is not a field, so both are absent from
		`current` and comparing them would report a change on every single run.
		"""
		if current is None:
			return True
		numeric = self._numeric_fields(doctype)
		unreadable = set(TABLE_FIELDS.get(doctype, {})) | set(ANNOTATIONS)
		for field, wanted in payload.items():
			if field in IGNORE_ON_COMPARE or field in unreadable:
				continue
			have = current.get(field)
			if field in numeric:
				if float(have or 0) != float(wanted or 0):
					return True
			elif _comparable(have).strip() != _comparable(wanted).strip():
				return True
		return False

	# -- link resolution ---------------------------------------------------

	def _load_departments(self) -> None:
		"""Map plain department names to whatever Frappe actually named them.

		ERPNext appends the company abbreviation, so "Omnipractice" becomes
		"Omnipractice - ABBR". The extract has to guess that string for the CSV
		path, but guessing is fragile: if the site's Company abbreviation ever
		differs from the profile's — or a department predates a company rename —
		every Employee link silently fails validation. Here we can simply look
		it up.
		"""
		self._departments = {
			row["department_name"]: row["name"]
			for row in frappe.get_all("Department", fields=["name", "department_name"], limit_page_length=0)
		}

	def _resolve_department(self, value):
		"""Turn an emitted department value into a real docname."""
		if not value:
			return value
		if value in self._departments.values():
			return value
		plain = str(value).rsplit(" - ", 1)[0]
		return self._departments.get(plain, value)

	# -- gold standard (hand-edited rotas) ----------------------------------

	def _drop_gold_standard(self, payloads: list[dict], stats: Counter) -> list[dict]:
		"""Filter out `Shift Schedule Assignment` rows that would land on a pattern a
		planner already fixed by hand in autoshift's Rota Editor.

		A hand edit is gold standard — it is the planner correcting the record, not a
		guess — and it is never expressed as a patch on the detected schedule: the
		editor always replaces the assignment wholesale with a fresh one tagged
		`custom_manually_edited` (see `Shift Schedule Assignment.custom_manually_edited`).
		That fresh record carries no `custom_zawin_key`, so the usual upsert-by-key
		lookup in `write` can never see it, and a re-run that recomputed the same
		employee/shift_type/branch would just insert a second, competing assignment
		alongside the hand edit instead of updating anything. Matched on
		(employee, shift_type, shift_location) rather than key, since that triple is
		the only vocabulary both sides share.
		"""
		manual = frappe.get_all(
			"Shift Schedule Assignment",
			filters={"custom_manually_edited": 1},
			fields=["employee", "shift_schedule", "shift_location"],
		)
		if not manual:
			return payloads

		schedule_names = {r["shift_schedule"] for r in manual if r["shift_schedule"]}
		schedule_names |= {p.get("shift_schedule") for p in payloads if p.get("shift_schedule")}
		shift_type_of = (
			{
				s["name"]: s["shift_type"]
				for s in frappe.get_all(
					"Shift Schedule",
					filters={"name": ["in", list(schedule_names)]},
					fields=["name", "shift_type"],
				)
			}
			if schedule_names
			else {}
		)
		gold = {(r["employee"], shift_type_of.get(r["shift_schedule"]), r["shift_location"]) for r in manual}

		kept = []
		for payload in payloads:
			pattern = (
				payload.get("employee"),
				shift_type_of.get(payload.get("shift_schedule")),
				payload.get("shift_location"),
			)
			if pattern in gold:
				stats["skipped_manual"] += 1
				log.info(
					"Shift Schedule Assignment: skipping %s / %s / %s — manually edited in the Rota "
					"Editor, gold standard, import must not overwrite it",
					*pattern,
				)
				continue
			kept.append(payload)
		return kept

	# -- writing -----------------------------------------------------------

	def write(self, doctype: str, rows: pd.DataFrame) -> None:
		stats = self.stats.setdefault(doctype, Counter())
		if rows is None or rows.empty:
			return

		key_field = KEY_FIELD.get(doctype)
		if key_field is None:
			raise ValueError(f"no key field configured for {doctype}")

		payloads = [{c: _clean(v) for c, v in row.items()} for _, row in rows.iterrows()]
		if doctype == "Shift Schedule Assignment":
			payloads = self._drop_gold_standard(payloads, stats)
			if not payloads:
				log.info("%-20s %s", doctype, dict(stats))
				return
		dept_field = DEPARTMENT_FIELD.get(doctype)
		if dept_field is not None:
			self._load_departments()
			for payload in payloads:
				if dept_field in payload:
					payload[dept_field] = self._resolve_department(payload[dept_field])
		keys = [p.get(key_field) for p in payloads]
		missing_key = sum(1 for k in keys if k is None)
		if missing_key:
			raise ValueError(f"{missing_key} {doctype} rows have no {key_field}")

		log.debug(f'trying {doctype} write with {key_field=}')
		# Child tables are not columns and annotations are not fields, so neither
		# can be read back for comparison.
		skip = set(TABLE_FIELDS.get(doctype, {})) | set(ANNOTATIONS)
		comparable = [f for f in payloads[0] if f not in skip]
		existing = self._existing(doctype, key_field, keys, comparable)

		with bulk_load_flags():
			self._write_rows(doctype, key_field, payloads, existing, stats)

		if not self.dry_run:
			frappe.db.commit()
		log.info("%-20s %s", doctype, dict(stats))

	def _write_rows(self, doctype, key_field, payloads, existing, stats) -> None:
		for i, payload in enumerate(payloads):
			key = payload[key_field]
			try:
				hit = existing.get(key)
				if hit is None:
					self._insert(doctype, payload, stats)
				elif not self._differs(doctype, hit, payload):
					stats["unchanged"] += 1
				elif hit["docstatus"] == 1:
					# Submitted: shift_type and start_date are not editable, so
					# the only way to correct it is to cancel and replace.
					self._cancel(doctype, hit["name"])
					self._insert(doctype, payload, stats)
					stats["recreated"] += 1
					stats["inserted"] -= 1
				else:
					self._update(doctype, hit["name"], payload, stats)
			except Exception as exc:  # one bad row must not sink the run
				stats["failed"] += 1
				self.errors.append({"doctype": doctype, "key": key, "error": f"{type(exc).__name__}: {exc}"})
				frappe.db.rollback()
			if i % CHUNK == CHUNK - 1 and not self.dry_run:
				frappe.db.commit()

	def _apply(self, doc, doctype: str, payload: dict) -> dict:
		"""Set every real field on `doc`, and hand back the annotations.

		A child table arrives as a plain sequence of values — ("Monday",
		"Tuesday") — because that is what the pipeline has to say about it; the
		child fieldname it fills is this loader's business, not the pipeline's.
		"""
		tables = TABLE_FIELDS.get(doctype, {})
		annotations = {}
		for field, value in payload.items():
			if field == "docstatus":
				continue
			if field in ANNOTATIONS:
				annotations[field] = value
			elif field in tables:
				doc.set(field, [{tables[field]: v} for v in (value or ())])
			else:
				doc.set(field, value)
		return annotations

	def _annotate(self, doc, annotations: dict) -> None:
		"""Tag and comment a written document.

		Both are how a person is told something about a record that is not part
		of the record — here, that a schedule describes a real rota but must not
		be switched on. A failure to annotate must not fail the row: the document
		itself is correct and the note is commentary on it.
		"""
		tag = (annotations.get("zawin_tag") or "").strip()
		comment = (annotations.get("zawin_comment") or "").strip()
		try:
			if tag:
				from frappe.desk.doctype.tag.tag import add_tag

				add_tag(tag, doc.doctype, doc.name)
			if comment and not _has_comment(doc, comment):
				doc.add_comment("Comment", comment)
		except Exception as exc:
			log.warning("could not annotate %s %s: %s", doc.doctype, doc.name, exc)

	def _insert(self, doctype: str, payload: dict, stats: Counter) -> None:
		doc = frappe.new_doc(doctype)
		annotations = self._apply(doc, doctype, payload)
		doc.insert(ignore_permissions=True)
		if self.submit and doctype in SUBMIT:
			doc.submit()
		self._annotate(doc, annotations)
		stats["inserted"] += 1

	def _update(self, doctype: str, name: str, payload: dict, stats: Counter) -> None:
		doc = frappe.get_doc(doctype, name)
		annotations = self._apply(doc, doctype, payload)
		doc.save(ignore_permissions=True)
		self._annotate(doc, annotations)
		if self.submit and doctype in SUBMIT and doc.docstatus == 0:
			doc.submit()
		stats["updated"] += 1

	def _cancel(self, doctype: str, name: str) -> None:
		doc = frappe.get_doc(doctype, name)
		# A cancelled document keeps the unique key, which would collide with
		# its replacement, so the key is cleared as it is retired.
		doc.cancel()
		if "custom_zawin_key" in doc.as_dict():
			frappe.db.set_value(doctype, name, "custom_zawin_key", None, update_modified=False)

	# -- protocol ----------------------------------------------------------

	def note(self, key: str, value) -> None:
		self.notes[key] = value

	def finalise(self) -> None:
		if not self.dry_run:
			frappe.db.commit()
		if self.errors:
			log.warning("%d rows failed; first few: %s", len(self.errors), self.errors[:3])

	def summary(self) -> dict:
		return {
			"counts": {dt: dict(c) for dt, c in self.stats.items()},
			"errors": len(self.errors),
		}
