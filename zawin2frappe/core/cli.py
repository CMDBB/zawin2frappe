"""Command-line entry point: `uv run zawin <command>`.

Uses argparse rather than typer/click deliberately — this project's whole
value is reproducibility against a database that is awkward to restore, so the
dependency surface is kept to what pandas already needs.
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys

import pandas as pd

from . import extract, fkscan, introspect, profile, roster
from .db import DERIVED_DIR, write_derived, write_raw

#: Commands whose output carries names or free text and must never default to
#: the committed data/derived/ directory. TAGPLANTERMIN.Beschreibung contains
#: clinical detail even on patient-less rows ("Couronne tombé sur une dent de
#: devant, pat. va venir 10min plus tôt"), and the crosswalk carries employee
#: names from the payroll export.
SENSITIVE_COMMANDS = {"agenda", "employees"}


def _save(args, df: pd.DataFrame) -> None:
	"""Route output to data/raw/ (gitignored) when it may contain PII."""
	if not args.save:
		return
	if args.command in SENSITIVE_COMMANDS:
		path = write_raw(df, args.save)
		print(f"-> {path} (data/raw is gitignored: output contains names/free text)")
	else:
		write_derived(df, args.save)


def _show(df: pd.DataFrame, limit: int | None = 40) -> None:
	with pd.option_context(
		"display.max_rows",
		limit or 10_000,
		"display.max_columns",
		None,
		"display.width",
		200,
	):
		print(df if limit is None or len(df) <= limit else df.head(limit))
	if limit is not None and len(df) > limit:
		print(f"... {len(df) - limit} more rows ({len(df)} total)")


def cmd_tables(args) -> None:
	df = introspect.candidate_tables(args.min_rows) if args.candidates else introspect.tables()
	if args.min_rows and not args.candidates:
		df = df[df["rows"] >= args.min_rows]
	_show(df, None if args.all else 40)
	_save(args, df)


def cmd_columns(args) -> None:
	df = introspect.columns(args.table)
	_show(df, None)
	_save(args, df)


def cmd_profile(args) -> None:
	df = profile.profile_table(args.table)
	_show(df, None)
	_save(args, df)


def cmd_values(args) -> None:
	df = profile.value_counts(args.table, args.column, args.top)
	_show(df, None)


def cmd_fkscan(args) -> None:
	df = fkscan.scan(
		args.tables,
		parent_tables=args.parents or None,
		min_containment=args.min_containment,
	)
	if df.empty:
		print("no relationships found above threshold")
		return
	_show(df, None)
	_save(args, df)


def cmd_categories(args) -> None:
	df = extract.categories()
	_show(df, None)
	_save(args, df)


def cmd_employees(args) -> None:
	df = extract.employees(active_only=args.active)
	_show(df, None if args.all else 40)
	_save(args, df)


def cmd_agenda(args) -> None:
	df = extract.agenda_with_labels(
		date_from=args.date_from,
		date_to=args.date_to,
		include_archive=args.archive,
	)
	_show(df, None if args.all else 40)
	_save(args, df)


def _default_window() -> tuple[str, str]:
	"""Trailing 12 complete months.

	The accounting FTE column is a *today* snapshot ("taux d'occupation actuel
	... fin de mois"), so the assignments it is reconciled against should be
	recent too. Reconciling a 2026 snapshot against 2024 measures staff turnover
	as much as extraction error. Forward bookings exist out to 2028 but thin
	rapidly after the current month, so the window ends at the last complete one.
	"""
	import datetime as _dt

	today = _dt.datetime.now(_dt.UTC).date()
	end = today.replace(day=1) - _dt.timedelta(days=1)  # last day of last month
	# 11 months back from the end month gives 12 complete months inclusive.
	# Subtracting 364 days and snapping to the 1st overshoots into a 13th month.
	year, month = end.year, end.month - 11
	while month <= 0:
		month += 12
		year -= 1
	return _dt.date(year, month, 1).isoformat(), end.isoformat()


def cmd_build(args) -> None:
	"""Extract, transform, and write Frappe Data Import CSVs."""
	from .build import run
	from .sinks import CsvSink

	result = run(
		CsvSink(args.out),
		target=args.target,
		date_from=args.date_from,
		date_to=args.date_to,
		signal_from=args.signal_from,
		signal_to=args.signal_to,
		full_day_policy=args.full_day_policy,
	)
	if result.unlinked_people:
		print(f"!! {result.unlinked_people} roster people have no ZaWin agenda column")
	if result.fte_outside_tolerance:
		print(f"   {result.fte_outside_tolerance} people outside the FTE band (report only)")


def cmd_binding(args) -> None:
	"""Rank staff by how closely their working week repeats.

	The calibration surface for `binding_settled_min`. The score distribution is
	continuous — there is no natural break to read a threshold off — so the
	threshold is a judgement about this practice and has to be made against real
	numbers. Prints everyone by default rather than only the eligible services,
	because the useful question is usually whether the people the profile marks
	eligible score any differently from the people it does not.
	"""
	from .db import query
	from .pipeline import apprenticeship, binding, calendar, discipline, location, presence, scope

	spine = roster.build_spine()
	columns = roster.resolve_columns(spine)
	annotated = extract.agenda_with_labels(date_from=args.signal_from, date_to=args.signal_to)
	spine = apprenticeship.apply(spine, annotated, columns)
	spine = discipline.refine(spine, columns, annotated)
	spine = scope.apply(spine, annotated, columns)

	agenda = extract.agenda_with_labels(date_from=args.date_from, date_to=args.date_to)
	patient = extract.patient_activity(args.date_from, args.date_to)
	raw, _styles = presence.build(agenda, patient, calendar.practice_days(query))
	person_level = presence.to_person_level(raw, columns)
	person_level = location.apply(person_level, columns, query)
	person_level = presence.collapse_daily(person_level, patient, columns)

	excused = binding.excused_days(agenda, columns)
	resolved = binding.resolve(spine, person_level, as_of=args.date_to, excused=excused)
	if resolved.empty:
		print("no schedulable people")
		return
	if args.eligible_only:
		resolved = resolved[resolved["eligible"]]
	_show(resolved, None if args.all else 60)

	scored = resolved["settledness"].dropna().sort_values(ascending=False).reset_index(drop=True)
	if len(scored) > 1:
		print(f"\nscored {len(scored)} people, {scored.min():.3f} .. {scored.max():.3f}")
		print("largest gaps in the ranking — candidate thresholds:")
		gaps = (-scored.diff()).dropna()
		for position in gaps.sort_values(ascending=False).head(5).index:
			above, below = scored[position - 1], scored[position]
			print(f"  cut at {below:.3f}..{above:.3f}  (gap {above - below:.3f}, {position} people above)")
	_save(args, resolved)


def cmd_restore(args) -> None:
	"""Refresh the local SQL Server from a nightly backup on the share."""
	from . import restore as R

	backups = R.available()
	if not backups:
		print(f"no backups in {R.backup_dir()}")
		return

	live = R.current()
	if live:
		print(f"live now : {live['physical_device_name']}")
		print(f"           backup taken {live['backup_finish_date']}, restored {live['restore_date']}")

	if args.backup:
		chosen = R.Backup(
			path=pathlib.Path(args.backup),
			size=pathlib.Path(args.backup).stat().st_size,
			taken_at=R._taken_at(pathlib.Path(args.backup)),
		)
	elif args.latest:
		chosen = backups[0]
	else:
		print(f"\n{len(backups)} backups in {R.backup_dir()}, newest first:")
		for index, backup in enumerate(backups[: args.limit], start=1):
			print(f"  {index:>3}  {backup.label}")
		if args.list:
			return
		answer = input(f"\nrestore which? [1-{min(len(backups), args.limit)}, blank = 1] ").strip()
		chosen = backups[(int(answer) - 1) if answer else 0]

	if args.list:
		return

	print(f"\nchosen: {chosen.label}")
	if not args.yes:
		if input("this replaces the local ZaWin database. continue? [y/N] ").strip().lower() != "y":
			print("aborted")
			return

	host_dir, server_dir = R.staging_dirs()
	bak = R.unpack(chosen, host_dir, reuse=not args.no_reuse)
	R.restore(f"{server_dir.rstrip('/')}/{bak.name}")

	state = R.horizon()
	print(
		f"\nagenda: {state['rows_total']:,} rows, {state['first_date']:%Y-%m-%d} "
		f"to {state['last_date']:%Y-%m-%d} ({state['rows_ahead']:,} of them still ahead)"
	)

	if not args.keep_bak and bak.parent == host_dir:
		try:
			bak.unlink()
			print(f"removed {bak.name}")
		except OSError as exc:
			print(f"! could not remove {bak.name}: {exc}")

	if args.then_build:
		code = R.rebuild()
		if code:
			print(f"! build exited {code}")


def build_parser() -> argparse.ArgumentParser:
	p = argparse.ArgumentParser(prog="zawin", description=__doc__)
	p.add_argument("-v", "--verbose", action="store_true")
	sub = p.add_subparsers(dest="command", required=True)

	t = sub.add_parser("tables", help="list tables with row counts")
	t.add_argument("--candidates", action="store_true", help="restrict to the agenda/HR domain vocabulary")
	t.add_argument("--min-rows", type=int, default=0)
	t.add_argument("--all", action="store_true", help="print every row")
	t.add_argument("--save", metavar="NAME.csv")
	t.set_defaults(func=cmd_tables)

	c = sub.add_parser("columns", help="column metadata for a table")
	c.add_argument("table")
	c.add_argument("--save", metavar="NAME.csv")
	c.set_defaults(func=cmd_columns)

	pr = sub.add_parser("profile", help="null/distinct/range per column")
	pr.add_argument("table")
	pr.add_argument("--save", metavar="NAME.csv")
	pr.set_defaults(func=cmd_profile)

	v = sub.add_parser("values", help="value frequency for one column")
	v.add_argument("table")
	v.add_argument("column")
	v.add_argument("--top", type=int, default=30)
	v.set_defaults(func=cmd_values)

	f = sub.add_parser("fkscan", help="infer foreign keys by value containment")
	f.add_argument("tables", nargs="+", help="child tables to scan")
	f.add_argument("--parents", nargs="*", help="restrict candidate parent tables")
	f.add_argument("--min-containment", type=float, default=0.95)
	f.add_argument("--save", metavar="NAME.csv")
	f.set_defaults(func=cmd_fkscan)

	k = sub.add_parser("categories", help="TAGPLANTERMINKATEGORIE lookup")
	k.add_argument("--save", metavar="NAME.csv")
	k.set_defaults(func=cmd_categories)

	e = sub.add_parser("employees", help="BEHANDLER as an HR record")
	e.add_argument("--active", action="store_true", help="exclude leavers")
	e.add_argument("--all", action="store_true")
	e.add_argument("--save", metavar="NAME.csv")
	e.set_defaults(func=cmd_employees)

	a = sub.add_parser("agenda", help="patient-less agenda entries with labels")
	a.add_argument("--date-from")
	a.add_argument("--date-to")
	a.add_argument("--archive", action="store_true", help="include TAGPLANTERMINARCHIV (2009-2017)")
	a.add_argument("--all", action="store_true")
	a.add_argument("--save", metavar="NAME.csv")
	a.set_defaults(func=cmd_agenda)

	bl = sub.add_parser("build", help="emit Frappe Data Import files")
	bl.add_argument("target", choices=["employees", "assignments", "all"], default="all", nargs="?")
	dfrom, dto = _default_window()
	bl.add_argument(
		"--date-from", default=dfrom, help=f"assignment period start (default {dfrom}: trailing 12 months)"
	)
	bl.add_argument("--date-to", default=dto, help=f"assignment period end (default {dto})")
	bl.add_argument(
		"--full-day-policy",
		choices=["single", "split"],
		default="single",
		help="'single' (default) emits at most one shift per person per day, "
		"matching the practice's 5-shifts-per-week contract and autoshift's "
		"model; 'split' emits AM and PM separately and requires "
		"HR Settings.allow_multiple_shift_assignments",
	)
	bl.add_argument("--out", default="data/import", help="output directory (gitignored; contains names)")
	# Default to all history and let the classifier narrow where it must:
	# a wide window is the steadier estimator, and pipeline.discipline
	# re-measures only the columns a wide window cannot decide.
	bl.add_argument(
		"--signal-from", default=None, help="start of the classification window (default: all history)"
	)
	bl.add_argument("--signal-to", default=None)
	bl.set_defaults(func=cmd_build)

	bd = sub.add_parser("binding", help="rank staff by how closely their working week repeats")
	bd.add_argument("--date-from", default=dfrom, help=f"history the score reads (default {dfrom})")
	bd.add_argument("--date-to", default=dto, help=f"end of that history (default {dto})")
	bd.add_argument("--signal-from", default=None)
	bd.add_argument("--signal-to", default=None)
	bd.add_argument(
		"--eligible-only", action="store_true", help="only services the profile marks assignments_binding"
	)
	bd.add_argument("--all", action="store_true", help="print every row")
	bd.add_argument("--save", metavar="NAME.csv")
	bd.set_defaults(func=cmd_binding)

	r = sub.add_parser("restore", help="refresh the local SQL Server from a nightly backup")
	r.add_argument("--list", action="store_true", help="show what is on the share and stop")
	r.add_argument("--backup", default=None, help="restore this file instead of choosing one")
	r.add_argument("--latest", action="store_true", help="take the newest without asking")
	r.add_argument("--limit", type=int, default=15, help="how many backups to list")
	r.add_argument("--yes", action="store_true", help="do not confirm before replacing the database")
	r.add_argument("--keep-bak", action="store_true", help="keep the unpacked .bak (it is large)")
	r.add_argument("--no-reuse", action="store_true", help="unpack again even if the .bak is there")
	r.add_argument(
		"--then-build", action="store_true", help="run $ZAWIN_BUILD_COMMAND once the restore lands"
	)
	r.set_defaults(func=cmd_restore)

	return p


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	logging.basicConfig(
		level=logging.DEBUG if args.verbose else logging.INFO,
		format="%(levelname)s %(name)s: %(message)s",
	)
	args.func(args)
	return 0


if __name__ == "__main__":
	sys.exit(main())
