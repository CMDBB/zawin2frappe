"""Employee and the config scaffolding it links to.

Accounting is authoritative for identity, FTE, service and dates. ZaWin
contributes only what accounting does not record: which discipline the pooled
staff work in, and the agenda link used later for shift assignments.

Emits in dependency order: Department, Designation, Branch, Shift Type,
Shift Location, then Employee.
"""

from __future__ import annotations

import logging
import os

import pandas as pd

from .. import settings

log = logging.getLogger(__name__)

#: Company, its abbreviation, designations, branches and shift types are all
#: practice configuration; see core.settings.


def department_link(name):
	"""Department name as Frappe will have named it.

	ERPNext's Department.autoname appends the company abbreviation, so an
	Employee.department of "Omnipractice" never resolves — it must be
	"Omnipractice - CMDB" (or whatever the site's abbreviation is).
	"""
	if name is None or (isinstance(name, float) and pd.isna(name)):
		return None
	return f"{name} - {settings.get().company_abbr}"


_SEX_TO_GENDER = {"M": "Male", "F": "Female"}

#: ZaWin's own "unknown birth date" sentinel, used on 6 of the 9 leavers that
#: carry a Geburtsdatum at all. Frappe makes date_of_birth mandatory, so the
#: sentinel is carried forward rather than invented: it is transparently not a
#: real date, it is what the source system already holds, and Data Import can
#: correct it later. Affected employees are listed in the build log.
UNKNOWN_DOB = "1900-01-01"

#: Frappe ships this as a stock `Gender` record. Used for people who exist only
#: in the payroll export, which carries no sex — guessing would be worse than
#: declining to state, and Frappe requires the field.
GENDER_UNSTATED = "Prefer not to say"


def _department_names(spine: pd.DataFrame) -> list[str]:
	"""Every department the import can link to.

	The departments people are actually filed under, plus every discipline the
	profile names. The second half matters because a Scheduling Role, a Shift
	Location or an autoshift Discipline Branch Config may point at a discipline
	nobody happens to be filed under this month, and a link with no target
	fails validation. An empty department costs one row.
	"""
	col = "department" if "department" in spine else "discipline_resolved"
	return sorted(set(spine[col].dropna()) | set(settings.get().all_disciplines))


def build_departments(spine: pd.DataFrame) -> pd.DataFrame:
	return pd.DataFrame({"department_name": _department_names(spine), "company": settings.get().company})


def build_designations(spine: pd.DataFrame) -> pd.DataFrame:
	prof = settings.get()
	names = sorted({d for s in spine["service_no"].dropna() if (d := prof.service(s).designation)})
	return pd.DataFrame({"designation_name": names})


def build_branches() -> pd.DataFrame:
	return pd.DataFrame({"branch": settings.get().branches})


def build_shift_types() -> pd.DataFrame:
	return pd.DataFrame(settings.get().shift_types)


def build_shift_locations(spine: pd.DataFrame) -> pd.DataFrame:
	"""One Shift Location per (branch, discipline).

	Not one per site, even though a site is what the agenda records: autoshift
	reads a shift's branch *and* its discipline back off this single link
	(`optimizer/data_loader.py`), so the pair has to be encoded in the name.

	Room-level occupancy stays out of it. Only 808 of ~954k agenda rows name an
	actual chair, the rest carry the site or the default, so autoshift's
	`rooms_num` is configured by hand per Discipline Branch Config.

	Every department present is emitted, including the ones the optimizer never
	schedules: their staff still have Shift Assignments, and a link with no
	target would fail validation.
	"""
	from .location import location_name

	disciplines = _department_names(spine)
	rows = [
		{
			"location_name": location_name(branch, discipline),
			"custom_branch": branch,
			"custom_discipline": department_link(discipline),
		}
		for branch in settings.get().branches
		for discipline in disciplines
	]
	# Shift Location autonames from `location_name` (autoname: field:location_name).
	return pd.DataFrame(rows, columns=["location_name", "custom_branch", "custom_discipline"])


def build_employees(
	spine: pd.DataFrame,
	columns: pd.DataFrame | None = None,
	as_of: str | None = None,
) -> pd.DataFrame:
	"""One row per person, keyed on the accounting personnel number.

	Everyone in the roster union is emitted, leavers included: dropping them
	would drop their shift assignments with them, losing both the history and
	the volume. Frappe needs the Employee to exist before those import.

	Leavers appear only in the payroll export, which carries neither sex nor a
	hire date. Rather than exclude them, both are filled: gender from the stock
	"Prefer not to say" record, and `date_of_joining` from their earliest
	recorded shift, which is a lower bound on when they started. Data Import can
	correct either later.
	"""
	df = spine.copy()

	# Payroll-only rows are people who left between the June payroll export and
	# the August accounting export, plus non-person placeholders.
	df["status"] = "Active"
	df.loc[df["source"] == "payroll_only", "status"] = "Left"

	# Agenda activity bounds, used as fallback hire and leaving dates.
	df["_first_shift"] = pd.NaT
	df["_last_shift"] = pd.NaT
	df["_left_on"] = pd.NaT
	df["_zawin_dob"] = pd.NaT
	if columns is not None and "first_seen" in columns:
		linked = columns.dropna(subset=["personnel_no"])
		if "date_of_birth" in linked:
			zdob = linked.groupby("personnel_no")["date_of_birth"].max()
			df["_zawin_dob"] = pd.to_datetime(df["personnel_no"].map(zdob))
		df["_first_shift"] = pd.to_datetime(
			df["personnel_no"].map(linked.groupby("personnel_no")["first_seen"].min())
		)
		df["_last_shift"] = pd.to_datetime(
			df["personnel_no"].map(linked.groupby("personnel_no")["last_seen"].max())
		)
		if "left_on" in linked:
			df["_left_on"] = pd.to_datetime(
				df["personnel_no"].map(linked.groupby("personnel_no")["left_on"].max())
			)

	forename = df["forename"].replace("", pd.NA)
	surname = df["surname"].replace("", pd.NA)

	out = pd.DataFrame(
		{
			"employee_number": df["personnel_no"],
			"first_name": forename.fillna(surname).fillna("?"),
			"last_name": surname,
			"employee_name": (forename.fillna("") + " " + surname.fillna("")).str.strip(),
			"gender": df["sex"].map(_SEX_TO_GENDER).fillna(GENDER_UNSTATED),
			# Accounting first, then ZaWin's own record, then its sentinel.
			"date_of_birth": df["date_of_birth"].fillna(df["_zawin_dob"]).fillna(pd.Timestamp(UNKNOWN_DOB)),
			"date_of_joining": df["joined_on"].fillna(df["_first_shift"]),
			"status": df["status"],
			# Frappe refuses to save an Employee with status "Left" and no
			# relieving date. ZaWin's Austritts_Datum is used where it exists,
			# otherwise the last recorded shift — a lower bound, symmetric with
			# how date_of_joining falls back to the first.
			# Capped at the end of the extract window, because the agenda
			# carries forward bookings years ahead and a leaver whose relieving
			# date sits in the future is plainly wrong. Their real leaving date
			# is unknown; all we can say is they were gone by the end of the
			# period we looked at. Anchoring to the window rather than to
			# "today" keeps the value stable across runs — otherwise every
			# leaver reports as changed once a day.
			"relieving_date": df["_left_on"]
			.fillna(df["_last_shift"])
			.clip(upper=pd.Timestamp(as_of) if as_of else pd.Timestamp.today().normalize())
			.where(df["status"].eq("Left")),
			"company": settings.get().company,
			"department": (df["department"] if "department" in df else df["discipline_resolved"]).map(
				department_link
			),
			"designation": df["service_no"].map(lambda c: settings.get().service(c).designation),
			# autoshift already defines this custom field on Employee.
			"custom_fte": df["fte_pct"],
			# The practice's own short code for a person, and the only name that
			# appears on their paper planning. Blank for anyone the crosswalk
			# could not tie to an agenda column — recent hires, mostly, who have
			# no ZaWin column yet but do appear on the printed roster.
			"custom_initials": df["behandler_initials"].str.strip(),
		}
	)

	# Rows with neither a hire date nor any agenda history cannot be represented
	# as an Employee and reference nothing — dropping them loses no assignments.
	unrepresentable = out["date_of_joining"].isna() & df["_last_shift"].isna()
	if unrepresentable.any():
		log.warning(
			"excluding %d rows with no hire date and no shift history: %s",
			int(unrepresentable.sum()),
			", ".join(out.loc[unrepresentable, "employee_number"].astype(str)),
		)
		out = out[~unrepresentable]
		df = df[~unrepresentable]

	filled = df["joined_on"].isna() & out["date_of_joining"].notna()
	if filled.any():
		log.info(
			"%d employees have no recorded hire date; used earliest shift instead",
			int(filled.sum()),
		)

	sentinel = out["date_of_birth"].eq(pd.Timestamp(UNKNOWN_DOB))
	if sentinel.any():
		log.warning(
			"%d employees carry the %s unknown-birth-date sentinel: %s",
			int(sentinel.sum()),
			UNKNOWN_DOB,
			", ".join(out.loc[sentinel, "employee_number"].astype(str)),
		)

	missing_relieving = out["status"].eq("Left") & out["relieving_date"].isna()
	if missing_relieving.any():
		log.warning(
			"%d leavers have no relieving date and will be rejected: %s",
			int(missing_relieving.sum()),
			", ".join(out.loc[missing_relieving, "employee_number"].astype(str)),
		)

	return out.sort_values("employee_number", key=lambda s: pd.to_numeric(s, errors="coerce")).reset_index(
		drop=True
	)


def build_all(
	spine: pd.DataFrame,
	columns: pd.DataFrame | None = None,
	as_of: str | None = None,
) -> dict[str, pd.DataFrame]:
	"""Every config doctype plus Employee, in import order."""
	return {
		"Department": build_departments(spine),
		"Designation": build_designations(spine),
		"Branch": build_branches(),
		"Shift Type": build_shift_types(),
		"Shift Location": build_shift_locations(spine),
		"Employee": build_employees(spine, columns, as_of),
	}
