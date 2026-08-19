"""Who the optimizer should schedule, and who is only recorded.

Two groups are kept in Frappe for completeness but never scheduled:

  Reception   works a rolling schedule that is not derivable from the agenda
              and is not something the optimizer should try to reproduce.
  Administration  not tied to shifts at all.

Neither needs a flag on Employee: autoshift only schedules departments that
have a Discipline-Designation-Branch Config row, so putting these people in
their own department removes them from scope and nothing else changes. Adding a
config row later brings them back in.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from .. import settings

log = logging.getLogger(__name__)

DEPT_RECEPTION = "Reception"
DEPT_ADMIN = "Administration"
DEPT_PROPHYLAXIS = "Prophylaxis"

#: Departments the optimizer does not schedule.
#:
#: Prophylaxis is here because the two people who do it work a second,
#: assistant-side column as well, and committing them to one discipline would
#: over-state that discipline's capacity. Recorded now, scheduled once the
#: dual-role case is modelled properly.
UNSCHEDULED_DEPARTMENTS = frozenset({DEPT_RECEPTION, DEPT_ADMIN, DEPT_PROPHYLAXIS})

#: Share of worked rows labelled reception/admin above which someone is treated
#: as reception. The observed distribution is bimodal with nothing in between:
#: 79-100% for the seven reception staff, 0-1% for all 30 other assistants.
#: threshold("reception")

#: Below this many worked rows the share is too noisy to act on.
#: threshold("min_worked_rows")

#: Accounting service that is administrative by definition.
#: profile admin_service

#: Curated scope overrides live with the profile, not in this package.

WORKED_KINDS = ("present", "reception", "admin", "training", "on_call", "remote")
NON_CLINICAL_KINDS = ("reception", "admin")


def load_overrides(path: Path | None = None) -> pd.DataFrame:
	"""Departments a human has corrected by hand.

	Empty unless the active profile ships one. Reception is detected from the
	agenda labels; this file exists for the cases labels cannot reach.
	"""
	path = path or settings.get().override_path("staff_scope")
	if path is None or not Path(path).is_file():
		return pd.DataFrame(columns=["personnel_no", "department", "reason"])
	return pd.read_csv(path, dtype={"personnel_no": str})


def reception_share(annotated: pd.DataFrame, columns: pd.DataFrame) -> pd.DataFrame:
	"""Per person, the share of worked rows labelled reception or admin."""
	link = columns.dropna(subset=["personnel_no"])[["behandler_id", "personnel_no"]]
	a = annotated.merge(link, left_on="FK_Behandler", right_on="behandler_id", how="inner")
	worked = a[a["shift_kind"].isin(WORKED_KINDS)]
	if worked.empty:
		return pd.DataFrame(columns=["personnel_no", "worked_rows", "reception_rows", "share"])

	out = (
		worked.assign(is_rec=worked["shift_kind"].isin(NON_CLINICAL_KINDS))
		.groupby("personnel_no")
		.agg(worked_rows=("is_rec", "size"), reception_rows=("is_rec", "sum"))
		.reset_index()
	)
	out["share"] = out["reception_rows"] / out["worked_rows"]
	return out


def apply(spine: pd.DataFrame, annotated: pd.DataFrame, columns: pd.DataFrame) -> pd.DataFrame:
	"""Set `department` on the spine, splitting out reception and admin.

	Precedence: curated override, then the reception label signal, then the
	accounting service, then the clinical discipline already resolved.
	"""
	prof = settings.get()
	df = spine.copy()
	share = reception_share(annotated, columns).set_index("personnel_no")

	df["reception_share"] = df["personnel_no"].map(share["share"])
	df["worked_rows"] = df["personnel_no"].map(share["worked_rows"]).fillna(0)

	df["department"] = df["discipline_resolved"]
	df["scope_source"] = "discipline"

	# Payroll-only leavers never went through the accounting service map, so
	# their discipline is null even when payroll records a service number.
	# Their service code is recoverable from payroll, and without this every
	# leaver imports with no department at all.
	missing = df["department"].isna() & df["service_no"].notna()
	if missing.any():
		filled = df.loc[missing, "service_no"].map(lambda c: prof.service(c).discipline)
		df.loc[missing, "department"] = filled
		df.loc[missing & df["department"].notna(), "scope_source"] = "payroll_service"

	is_admin_service = df["service_no"].eq(prof.admin_service)
	df.loc[is_admin_service, ["department", "scope_source"]] = [DEPT_ADMIN, "service"]

	# Reception is inferred from what staff type into the agenda, which is a
	# weaker source than an explicit payroll designation. A clinician who
	# covers the desk often enough — or whose early years were spent on it —
	# can cross the threshold on labels alone: over all history the practice's
	# senior dentist did exactly that. So the label signal may fill in a
	# service that has no clinical discipline of its own (the assistant and
	# admin pools), but it never overrules one that does.
	clinically_placed = df["service_no"].map(
		lambda c: bool(prof.service(c).clinical and prof.service(c).discipline)
	)
	is_reception = (
		df["reception_share"].ge(prof.threshold("reception", 0.40))
		& df["worked_rows"].ge(prof.threshold("min_worked_rows", 30))
		& ~clinically_placed
	)
	df.loc[is_reception, ["department", "scope_source"]] = [DEPT_RECEPTION, "agenda_labels"]

	overruled = (
		df["reception_share"].ge(prof.threshold("reception", 0.40))
		& df["worked_rows"].ge(prof.threshold("min_worked_rows", 30))
		& clinically_placed
	)
	if overruled.any():
		log.info(
			"%d clinically-placed staff look like reception on labels but keep their payroll discipline: %s",
			int(overruled.sum()),
			list(df.loc[overruled, "personnel_no"]),
		)

	# Prophylaxis, from ZaWin rather than accounting. Payroll tends to be
	# inconsistent for this role, because prophylaxis counts contractually as
	# assistant work: two people doing the same job can sit under different
	# service codes while their ZaWin data is near-identical — a prophylaxis
	# column carrying patient bookings plus a presence column carrying none.
	from ..roster import funktion_prophylaxis

	fp = funktion_prophylaxis()
	prophy = (
		set() if fp is None else set(columns.loc[columns["funktion_code"].eq(fp), "personnel_no"].dropna())
	)
	if prophy:
		hit = df["personnel_no"].isin(prophy)
		df.loc[hit, ["department", "scope_source"]] = [DEPT_PROPHYLAXIS, "funktion"]

	overrides = load_overrides()
	if not overrides.empty:
		by_person = overrides.set_index("personnel_no")["department"]
		hit = df["personnel_no"].isin(by_person.index)
		df.loc[hit, "department"] = df.loc[hit, "personnel_no"].map(by_person)
		df.loc[hit, "scope_source"] = "override"
		log.info("applied %d curated scope overrides", int(hit.sum()))

	df["schedulable"] = ~df["department"].isin(UNSCHEDULED_DEPARTMENTS)
	log.info(
		"scope: %d schedulable, %d recorded only (%s)",
		int(df["schedulable"].sum()),
		int((~df["schedulable"]).sum()),
		", ".join(f"{k}={v}" for k, v in df.loc[~df["schedulable"], "department"].value_counts().items()),
	)
	return df
