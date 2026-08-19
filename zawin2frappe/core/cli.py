"""Command-line entry point: `uv run zawin <command>`.

Uses argparse rather than typer/click deliberately — this project's whole
value is reproducibility against a database that is awkward to restore, so the
dependency surface is kept to what pandas already needs.
"""

from __future__ import annotations

import argparse
import logging
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
