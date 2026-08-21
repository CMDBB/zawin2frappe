"""Frappe records the import needs before it can run.

A site that has not been through the ERPNext setup wizard has no Company and no
Gender records, and Department is a nested set with no root — so the first
import fails on link validation rather than on anything to do with ZaWin.

Everything here is derived from the practice profile, so pointing a site at a
different profile provisions it for that practice instead.
"""

from __future__ import annotations

import logging

import frappe

from ..core import settings

log = logging.getLogger(__name__)

#: Frappe ships these as stock Gender records, but only once setup has run.
#: "Prefer not to say" is needed for staff whose payroll row carries no sex.
GENDERS = ("Male", "Female", "Prefer not to say")

ROOT_DEPARTMENT = "All Departments"


def ensure_prerequisites(*, verbose: bool = True) -> dict:
	"""Create what the import links to. Idempotent."""
	prof = settings.get()
	done: dict[str, str] = {}

	if not frappe.db.exists("Company", prof.company):
		frappe.flags.in_setup_wizard = True
		try:
			frappe.get_doc(
				{
					"doctype": "Company",
					"company_name": prof.company,
					"abbr": prof.company_abbr,
					"default_currency": prof.zawin.get("currency", "CHF"),
					"country": prof.zawin.get("country", "Switzerland"),
				}
			).insert(ignore_permissions=True)
		except Exception as exc:
			# On a site whose ERPNext stock fixtures never ran, Company insert
			# raises in a downstream stock hook *after* the row lands. The
			# Company survives and is usable for HR, so this is not fatal.
			log.warning("company insert raised %s (usually harmless)", type(exc).__name__)
		frappe.db.commit()
		done["company"] = "created" if frappe.db.exists("Company", prof.company) else "FAILED"
	else:
		done["company"] = "exists"

	made = [g for g in GENDERS if not frappe.db.exists("Gender", g)]
	for gender in made:
		frappe.get_doc({"doctype": "Gender", "gender": gender}).insert(ignore_permissions=True)
	done["genders"] = f"created {len(made)}" if made else "exist"

	root = f"{ROOT_DEPARTMENT} - {prof.company_abbr}"
	if not frappe.db.exists("Department", root):
		frappe.get_doc(
			{
				"doctype": "Department",
				"department_name": ROOT_DEPARTMENT,
				"company": prof.company,
				"is_group": 1,
			}
		).insert(ignore_permissions=True)
		done["root_department"] = "created"
	else:
		done["root_department"] = "exists"

	# Employee must be named by employee number: shift assignments reference
	# people by their payroll number, and under the default naming series the
	# docname would be HR-EMP-00001 and every link would fail.
	hr = frappe.get_single("HR Settings")
	if hr.emp_created_by != "Employee Number":
		hr.emp_created_by = "Employee Number"
		hr.save(ignore_permissions=True)
		done["emp_naming"] = "set to Employee Number"
	else:
		done["emp_naming"] = "already Employee Number"

	for shift in prof.shift_types:
		name = shift.get("name")
		if name and not frappe.db.exists("Shift Type", name):
			frappe.get_doc(
				{
					"doctype": "Shift Type",
					"__newname": name,
					"name": name,
					"start_time": shift["start_time"],
					"end_time": shift["end_time"],
					"company": prof.company,
				}
			).insert(ignore_permissions=True)
	done["shift_types"] = f"{frappe.db.count('Shift Type')} present"

	for branch in prof.branches:
		if not frappe.db.exists("Branch", branch):
			frappe.get_doc({"doctype": "Branch", "branch": branch}).insert(ignore_permissions=True)
	done["branches"] = f"{len(prof.branches)} present"

	# Shift Locations are deliberately NOT created here. autoshift reads a shift's
	# branch *and* discipline back off the location link, so a location is a
	# (branch, discipline) pair — which needs the resolved disciplines the build
	# produces. A branch-only location would validate fine and then throw inside
	# the optimizer for having no discipline. `pipeline.employees` emits them.

	frappe.db.commit()
	if verbose:
		for k, v in done.items():
			log.info("  %-16s %s", k, v)
	return done
