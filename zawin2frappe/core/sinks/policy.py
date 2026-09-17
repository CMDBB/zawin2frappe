"""What a re-import may and may not overwrite.

The extract is a *partial* view of a practice: it knows what ZaWin recorded and
nothing about what anybody has since entered, corrected or curated in Frappe.
Reconciling the two is `loaders.frappe_sink`'s job; the rules it reconciles *by*
are decisions rather than mechanics, so they live here — in `core`, which imports
no Frappe and can therefore be tested without a bench.
"""

from __future__ import annotations

from collections.abc import Iterable


def is_blank(value) -> bool:
	"""Whether a payload value says nothing at all.

	`None` and whitespace only. **Zero is not blank**: a `Check` of 0 and a count of
	0 are things the extract has decided, and a rule that swallowed them would make
	"not binding" and "not unconfirmed" unwritable.
	"""
	if value is None:
		return True
	return isinstance(value, str) and not value.strip()


def preserve_existing(current: dict | None, payload: dict) -> dict:
	"""`payload`, with every blank that would erase a stored value put back.

	An absent value means "the extract has nothing to say about this", never "make
	it empty" — the two are indistinguishable in a CSV and very different in a
	record. `Employee.custom_initials` is the case that named this: it is curated by
	hand for anybody the extract has no initials for, and every re-import used to
	wipe it.

	Restoring the stored value rather than dropping the key matters on the
	cancel-and-recreate path, where the payload becomes a *new* document: dropping
	it would lose the value just as thoroughly, only later.
	"""
	if not current:
		return payload
	return {
		field: current[field]
		if is_blank(value) and field in current and not is_blank(current[field])
		else value
		for field, value in payload.items()
	}


def needs_replacement(differing: Iterable[str], editable_after_submit: Iterable[str]) -> bool:
	"""Whether a *submitted* document has to be cancelled and rebuilt to be corrected.

	Frappe refuses edits to a submitted document except on fields marked
	`allow_on_submit`, so a changed `shift_type` or `start_date` leaves no choice.
	A change confined to those fields does, though — `Shift Assignment.
	custom_scheduling_role` is one — and taking it means a corrected role no longer
	costs the record its identity, its links and its comments.
	"""
	return not set(differing) <= set(editable_after_submit)
