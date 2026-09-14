"""Flag every Shift Schedule Assignment this import already wrote as unconfirmed.

autoshift turned its rota flag round: `custom_manually_edited` marked a hand edit as
gold, and `custom_unconfirmed` now marks an *inferred* pattern as silver instead, with
unflagged meaning gold. The new field defaults to 0, so without this every assignment
already imported would read as confirmed, and `loaders.frappe_sink` would never update
it again.

A row carries `custom_zawin_key` exactly when this import wrote it. A Rota Editor edit
replaces the row with a fresh one that has no key, so no hand edit is caught here.

Runs after model sync, but custom fields only arrive with fixture sync, which comes
later still, so the field is synced here first if it is missing.
"""

import frappe

DOCTYPE = "Shift Schedule Assignment"


def execute():
	if not frappe.db.has_column(DOCTYPE, "custom_zawin_key"):
		return  # nothing was ever imported
	if not frappe.db.has_column(DOCTYPE, "custom_unconfirmed"):
		from frappe.utils.fixtures import sync_fixtures

		sync_fixtures("autoshift")
		frappe.clear_cache(doctype=DOCTYPE)
	if not frappe.db.has_column(DOCTYPE, "custom_unconfirmed"):
		return  # autoshift too old to carry the flag; its own migrate will need a re-import

	table = frappe.qb.DocType(DOCTYPE)
	(
		frappe.qb.update(table)
		.set(table.custom_unconfirmed, 1)
		.where(table.custom_zawin_key.isnotnull() & (table.custom_zawin_key != ""))
		.run()
	)
