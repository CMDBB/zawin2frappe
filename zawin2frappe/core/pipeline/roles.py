"""Scheduling Role and Employee Scheduling Role: the multi-role capability model.

autoshift schedules against `Employee Scheduling Role` rows now, not
`Employee.department`/`Employee.designation` (see
`autoshift/patches/migrate_designations_to_scheduling_roles.py`). A person's
*primary* role still comes from their accounting designation and resolved
discipline, exactly as that migration derived it from the old per-designation
config: one `Scheduling Role` per `(discipline, designation)` pair, named
after the designation unless the same designation spans more than one
discipline.

On top of that, a person can hold *secondary* roles the payroll designation
never records. Two signals produce those, both practice-specific and both
read from the active profile rather than hardcoded here:

  colour     BEHANDLER.FarbeTermin (default appointment colour) mapped via
             `profile.role_color_rules` — e.g. a pink default marks someone
             as also working sterilization. A rule may instead (or as well)
             be `primary`, which places the person in that discipline rather
             than adding a role; that half is `pipeline.discipline`'s, and a
             rule naming no `role` adds nothing here.
  funktion   BEHANDLER.Funktion == `profile.zawin['funktion_prophylaxis']`
             marks someone as also working prophylaxis. This used to reroute
             their `department` to a parked, unscheduled "Prophylaxis" bucket
             (see the removed block in `scope.py`); now it just adds a role,
             and they stay scheduled under their real discipline.

Secondary roles are always emitted as `Scheduling Role`s when configured, even
before anyone qualifies for them — same as Branch or Shift Type, they are
config, not data.

A role also carries whether its holders set their own working week
(`assignments_binding`), which comes from the service the role was built from
or from the colour rule that granted it. `pipeline.binding` then decides, per
holder, whether that person's week has actually settled enough to be frozen;
see its docstring for the split between the two.
"""

from __future__ import annotations

import pandas as pd

from .. import extract, settings
from ..roster import funktion_prophylaxis
from .binding import OVERRIDE_INHERIT
from .employees import department_link

#: Rooms one holder covers, absent an explicit override in the rule.
DEFAULT_MAX_ROOMS = 1

PROPHYLAXIS_ROLE = "Prophylaxis"


def _designation(spine: pd.DataFrame) -> pd.Series:
	"""Payroll designation per person — not a spine column; derived from
	`service_no` the same way `pipeline.employees.build_employees` does."""
	prof = settings.get()
	return spine["service_no"].map(lambda c: prof.service(c).designation)


def _role_namer(spine: pd.DataFrame) -> tuple[pd.Series, dict]:
	"""Designation series plus the ambiguous-naming lookup, shared by both
	builders so they agree on exactly the same role names."""
	designation = _designation(spine)
	pairs = pd.DataFrame({"discipline": spine["discipline_resolved"], "designation": designation}).dropna()
	pairs = pairs.drop_duplicates()
	disciplines_per_designation = pairs.groupby("designation")["discipline"].nunique()

	def name(discipline, designation_) -> str | None:
		if pd.isna(discipline) or pd.isna(designation_):
			return None
		ambiguous = disciplines_per_designation.get(designation_, 1) > 1
		return f"{designation_} ({discipline})" if ambiguous else designation_

	return designation, {"pairs": pairs, "name": name}


def _binding_roles(spine: pd.DataFrame) -> set[str]:
	"""Role names whose holders set their own week, from the profile.

	A role is binding if any service that produces it is marked
	`assignments_binding`. In practice a role is built from exactly one service
	— the designation comes from it — so "any" only matters for the pathological
	case of two services sharing a designation *and* a discipline, where erring
	towards binding leaves the decision to `pipeline.binding` per person rather
	than dropping it silently.
	"""
	prof = settings.get()
	designation = _designation(spine)
	_col, ctx = _role_namer(spine)
	names = pd.Series(
		[ctx["name"](d, r) for d, r in zip(spine["discipline_resolved"], designation, strict=False)],
		index=spine.index,
	)
	binding = spine["service_no"].map(lambda c: prof.service(c).assignments_binding)
	out = set(names[binding.fillna(False).astype(bool)].dropna())
	out |= {
		rule["role"]
		for rule in prof.role_color_rules.values()
		if rule.get("role") and rule.get("assignments_binding")
	}
	return out


def build_scheduling_roles(spine: pd.DataFrame) -> pd.DataFrame:
	"""One Scheduling Role per (discipline, designation) among schedulable
	employees, plus every role a configured secondary signal can grant."""
	prof = settings.get()
	schedulable = spine[spine["schedulable"]]
	_designation_col, ctx = _role_namer(schedulable)
	binding = _binding_roles(schedulable)

	rows = [
		{
			"role_name": ctx["name"](discipline, designation),
			"discipline": department_link(discipline),
			"max_rooms": DEFAULT_MAX_ROOMS,
			"active": 1,
		}
		for discipline, designation in ctx["pairs"].itertuples(index=False)
	]

	for rule in prof.role_color_rules.values():
		# A rule with no `role` only places people in a discipline
		# (`pipeline.discipline.colour_placements`); the role their designation
		# already gives them staffs it, so there is nothing extra to create.
		if not rule.get("role"):
			continue
		rows.append(
			{
				"role_name": rule["role"],
				"discipline": department_link(rule["discipline"]),
				"max_rooms": int(rule.get("max_rooms", DEFAULT_MAX_ROOMS)),
				"active": 1,
			}
		)

	if funktion_prophylaxis() is not None:
		discipline = prof.zawin.get("prophylaxis_discipline")
		if not discipline:
			raise ValueError(
				"zawin.funktion_prophylaxis is set but zawin.prophylaxis_discipline is not — "
				"the Prophylaxis Scheduling Role needs a Department to belong to."
			)
		rows.append(
			{
				"role_name": PROPHYLAXIS_ROLE,
				"discipline": department_link(discipline),
				"max_rooms": DEFAULT_MAX_ROOMS,
				"active": 1,
			}
		)

	out = pd.DataFrame(rows, columns=["role_name", "discipline", "max_rooms", "active"])
	out = out.drop_duplicates(subset="role_name").sort_values("role_name").reset_index(drop=True)
	out["assignments_binding"] = out["role_name"].isin(binding).astype(int)
	return out


def build_employee_scheduling_roles(
	spine: pd.DataFrame,
	columns: pd.DataFrame | None,
	binding: pd.DataFrame | None = None,
) -> pd.DataFrame:
	"""One row per (employee, role) an employee actually holds.

	`employee` is the payroll personnel number directly: HR Settings is
	provisioned with `emp_created_by = "Employee Number"`
	(`loaders/bootstrap.py`), so that number *is* the Employee docname. No
	link resolution is needed, only write order — Employee must already exist
	(see `core/build.py`).

	`binding` is `pipeline.binding.resolve`'s verdict per person. It is written
	only onto rows whose role is actually binding: elsewhere the role carries no
	flag to override, and a stray "Not Binding" would read as a decision about
	someone it was never made about.
	"""
	prof = settings.get()
	schedulable = spine[spine["schedulable"]].copy()
	schedulable["designation"], ctx = _role_namer(schedulable)
	schedulable["scheduling_role"] = [
		ctx["name"](d, r)
		for d, r in zip(schedulable["discipline_resolved"], schedulable["designation"], strict=False)
	]

	frames = [
		schedulable.dropna(subset=["scheduling_role"])[["personnel_no", "scheduling_role"]].rename(
			columns={"personnel_no": "employee"}
		)
	]

	if columns is not None and not columns.empty:
		linked = columns.dropna(subset=["personnel_no"])

		if prof.role_color_rules:
			beh_colors = extract.employees()[["behandler_id", "default_color"]]
			by_color = linked.merge(beh_colors, on="behandler_id", how="left").dropna(
				subset=["default_color"]
			)
			by_color["scheduling_role"] = by_color["default_color"].map(
				lambda c: (prof.role_color_rules.get(int(c)) or {}).get("role")
			)
			hits = by_color.dropna(subset=["scheduling_role"])
			frames.append(
				hits[["personnel_no", "scheduling_role"]].rename(columns={"personnel_no": "employee"})
			)

		fp = funktion_prophylaxis()
		if fp is not None:
			hits = linked[linked["funktion_code"].eq(fp)][["personnel_no"]].copy()
			hits["scheduling_role"] = PROPHYLAXIS_ROLE
			frames.append(hits.rename(columns={"personnel_no": "employee"}))

	out = (
		pd.concat(frames, ignore_index=True)
		if frames
		else pd.DataFrame(columns=["employee", "scheduling_role"])
	)
	out = out.dropna(subset=["employee", "scheduling_role"]).drop_duplicates()
	out["name"] = out["employee"].astype(str) + "-" + out["scheduling_role"]
	out["active"] = 1

	binding_roles = _binding_roles(schedulable)
	decision = (
		binding.set_index("personnel_no")["binding_override"]
		if binding is not None and not binding.empty
		else pd.Series(dtype=object)
	)
	out["binding_override"] = [
		decision.get(employee, OVERRIDE_INHERIT) if role in binding_roles else OVERRIDE_INHERIT
		for employee, role in zip(out["employee"], out["scheduling_role"], strict=False)
	]
	return out.sort_values(["employee", "scheduling_role"]).reset_index(drop=True)
