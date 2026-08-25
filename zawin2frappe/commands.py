"""Bench commands.

    bench --site <site> zawin-build --target all --out data/import
    bench --site <site> zawin-profile

Connection details and the profile path come from the site config, so the same
command works across sites without arguments:

    "zawin_profile": "/path/to/apps/cmdb_frappe/profiles/cmdb.json",
    "zawin_mssql": {"host": "...", "port": 1433, "user": "...", "password": "..."}
"""

from __future__ import annotations
import logging

import json

import click
import frappe
from frappe.commands import get_site, pass_context


@click.command("zawin-build")
@click.option(
	"--target", type=click.Choice(["employees", "assignments", "all"]), default="all", show_default=True
)
@click.option("--out", default=None, help="Write Data Import CSVs here. Omit to load straight into Frappe.")
@click.option("--date-from", default=None, help="Assignment period start (YYYY-MM-DD).")
@click.option("--date-to", default=None, help="Assignment period end (YYYY-MM-DD).")
@click.option(
	"--signal-from", default=None, help="Start of the window used to classify staff. Defaults to all history."
)
@click.option("--signal-to", default=None)
@click.option(
	"--full-day-policy",
	type=click.Choice(["single", "split"]),
	default="single",
	show_default=True,
	help="'single' emits at most one shift per person per day.",
)
@click.option("--dry-run", is_flag=True, help="Build and report, writing nothing.")
@pass_context
def zawin_build(context, target, out, date_from, date_to, signal_from, signal_to, full_day_policy, dry_run):
	"""Extract ZaWin agenda data and load it into Frappe HR."""
	logging.basicConfig(level=logging.DEBUG)
	site = get_site(context)
	frappe.init(site=site)
	frappe.connect()
	try:
		from zawin2frappe.core import build, settings
		from zawin2frappe.core.sinks import CsvSink

		if not date_from or not date_to:
			default_from, default_to = _default_window()
			date_from = date_from or default_from
			date_to = date_to or default_to

		click.echo(f"profile : {settings.get().name}")
		click.echo(f"window  : {date_from} .. {date_to}")

		if dry_run:
			sink = _NullSink()
		elif out:
			sink = CsvSink(out)
		else:
			from zawin2frappe.loaders import FrappeDocSink
			from zawin2frappe.loaders.bootstrap import ensure_prerequisites

			# Writing straight into Frappe depends on Employee being keyed by
			# employee number, not the HRMS naming series — Scheduling Role
			# and Employee Scheduling Role link to it by that number directly
			# (see pipeline.roles). A site that never ran `zawin-bootstrap`
			# still has HR Settings on its setup-wizard default of "Naming
			# Series", which silently breaks that link. Run unconditionally
			# rather than trust the operator remembered the separate command
			# — it is idempotent (checks before writing).
			ensure_prerequisites(verbose=False)
			sink = FrappeDocSink()

		result = build.run(
			sink,
			target=target,
			date_from=date_from,
			date_to=date_to,
			signal_from=signal_from,
			signal_to=signal_to,
			full_day_policy=full_day_policy,
			write_reports=not dry_run,
		)
		if hasattr(sink, "summary"):
			# What actually landed, which is not the same as what was built.
			for doctype, counts in sink.summary()["counts"].items():
				detail = "  ".join(f"{k}={v:,}" for k, v in sorted(counts.items()) if v)
				click.echo(f"  {doctype:<20} {detail or 'nothing to do'}")
			if sink.errors:
				click.echo(f"! {len(sink.errors)} rows failed:")
				for e in sink.errors[:5]:
					click.echo(f"    {e['doctype']}/{e['key']}: {e['error']}")
		else:
			for doctype, n in result.counts().items():
				click.echo(f"  {doctype:<20} {n:>7,}")
		if result.unlinked_people:
			click.echo(f"! {result.unlinked_people} roster people have no ZaWin agenda column")
		if result.fte_outside_tolerance:
			click.echo(f"  {result.fte_outside_tolerance} people outside the FTE band (report only)")
		frappe.db.commit()
	finally:
		frappe.destroy()


@click.command("zawin-profile")
@pass_context
def zawin_profile(context):
	"""Show the practice profile this site resolves to."""
	site = get_site(context)
	frappe.init(site=site)
	frappe.connect()
	try:
		from zawin2frappe.core import settings

		p = settings.get()
		click.echo(
			json.dumps(
				{
					"name": p.name,
					"source": str(p.source_path),
					"company": f"{p.company} ({p.company_abbr})",
					"branches": p.branches,
					"services": len(p.services),
					"label_rules": len(p.label_rules),
					"disciplines": list(p.all_disciplines),
					"default_discipline": p.default_discipline,
					"overrides": {k: str(p.override_path(k)) for k in p.overrides},
				},
				indent=2,
				ensure_ascii=False,
			)
		)
	finally:
		frappe.destroy()


def _default_window() -> tuple[str, str]:
	"""Trailing 12 complete months."""
	from zawin2frappe.core.cli import _default_window as w

	return w()


class _NullSink:
	"""Counts records without writing them, for --dry-run."""

	def write(self, doctype, rows) -> None:
		pass

	def note(self, key, value) -> None:
		pass

	def finalise(self) -> None:
		pass


@click.command("zawin-bootstrap")
@pass_context
def zawin_bootstrap(context):
	"""Create the Company, Genders, Departments and Shift Types the import links to."""
	site = get_site(context)
	frappe.init(site=site)
	frappe.connect()
	try:
		from zawin2frappe.core import settings
		from zawin2frappe.loaders.bootstrap import ensure_prerequisites

		click.echo(f"profile : {settings.get().name}")
		for key, value in ensure_prerequisites(verbose=False).items():
			click.echo(f"  {key:<16} {value}")
		frappe.db.commit()
	finally:
		frappe.destroy()


commands = [zawin_build, zawin_profile, zawin_bootstrap]
