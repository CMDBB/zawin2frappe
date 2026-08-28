"""The practice profile: everything this code must not assume.

ZaWin is used by many practices, and none of them share a service numbering,
a set of disciplines, a branch list, or — least of all — the free-text
vocabulary their staff type into the agenda. All of that lives here as **data**,
loaded from a JSON profile, so the code carries no site-specific knowledge.

Resolution order, first hit wins:

  1. an explicit path passed to `load()`
  2. ``ZAWIN_PROFILE`` in the environment
  3. ``zawin_profile`` in the Frappe site config, when running inside a site
  4. ``profiles/default.json`` in this app

A site-specific app can therefore ship its own profile and point at it, without
this package changing. Nothing here validates *meaning* — an unknown service
code simply produces an unmapped employee, which the build reports rather than
guesses at.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

APP_ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIR = APP_ROOT / "profiles"
DEFAULT_PROFILE = PROFILE_DIR / "default.json"


@dataclass(frozen=True)
class Service:
	"""One accounting service code."""

	designation: str | None = None
	discipline: str | None = None
	clinical: bool = False
	#: For an apprenticeship service: the service its apprentices become once
	#: they qualify. Setting it is what tells `pipeline.apprenticeship` this
	#: code is an apprenticeship at all, and accounting is not always prompt
	#: about retiring the filing itself. Null everywhere else.
	graduates_to: str | None = None


@dataclass(frozen=True)
class Profile:
	name: str
	#: Where this profile was loaded from. Companion data (curated overrides)
	#: is resolved relative to it, so a profile shipped by a separate,
	#: site-specific app can bring its own overrides with it.
	source_path: Path
	company: str
	company_abbr: str
	branches: list[str]
	shift_types: list[dict[str, str]]
	#: minutes since midnight
	day_start: int
	day_end: int
	midday: int
	services: dict[str, Service]
	admin_service: str | None
	absence_categories: tuple[str, ...]
	non_clinical_categories: tuple[str, ...]
	#: (regex, kind, counts_as_worked), first match wins
	label_rules: list[tuple[str, str, bool]]
	#: (regex, discipline), first match wins. Applied to a presence label, this
	#: is how the practice's own vocabulary — "PRES ORTHO", "PRES HD" — names a
	#: discipline. See `pipeline.discipline`.
	discipline_labels: list[tuple[str, str]]
	#: Discipline for presence rows that name none. A bare "PRES" means the main
	#: floor at most practices; null leaves those rows out of the count instead.
	default_discipline: str | None
	#: Disciplines that must exist whether or not anyone is currently filed
	#: under them. Only needed for ones no service, label rule or colour rule
	#: mentions — `all_disciplines` already covers those.
	declared_disciplines: list[str]
	#: TAGPLAN ids that are rooms/purposes rather than disciplines
	non_discipline_agendas: set[int]
	#: BEHORT id -> branch name
	site_behort: dict[int, str]
	#: FarbeTermin (OLE_COLOR int) -> {"discipline": ..., "role": ..., "max_rooms": ...,
	#: "primary": bool}. `role` grants that Scheduling Role on top of the
	#: designation-derived one; `primary` makes the colour place the person in
	#: the discipline outright (`pipeline.discipline`). Either, or both.
	role_color_rules: dict[int, dict[str, Any]]
	thresholds: dict[str, float]
	zawin: dict[str, Any] = field(default_factory=dict)
	#: name -> path, relative to source_path's directory unless absolute
	overrides: dict[str, str] = field(default_factory=dict)
	#: name -> dotted path of a zero-argument factory returning a resolver object.
	#: Where `overrides` supply practice-specific *data*, these supply practice-specific
	#: *rules* — logic this package must not carry. See `pipeline.location`.
	resolvers: dict[str, str] = field(default_factory=dict)

	def resolver(self, name: str):
		"""Instantiate the named resolver, or None if the profile declares none.

		The factory is imported by dotted path, so a site-specific app can ship
		the rules without this package importing it. Absent is normal, not an
		error: a practice with nothing to resolve simply gets the default.
		"""
		path = self.resolvers.get(name)
		if not path:
			return None
		module_path, _, attr = path.rpartition(".")
		if not module_path:
			raise ValueError(f"resolver {name!r} must be a dotted path, got {path!r}")
		return getattr(importlib.import_module(module_path), attr)()

	def override_path(self, name: str) -> Path | None:
		"""Path to a curated override file, or None if the profile has none.

		Overrides are judgements about identifiable people — a rename, or that
		someone's real job differs from their payroll filing. They live beside
		the profile, never in the public package.
		"""
		rel = self.overrides.get(name)
		if not rel:
			return None
		p = Path(rel)
		return p if p.is_absolute() else self.source_path.parent / p

	def threshold(self, key: str, default: float) -> float:
		return float(self.thresholds.get(key, default))

	def service(self, code: str | None) -> Service:
		return self.services.get(str(code), Service()) if code is not None else Service()

	@property
	def clinical_services(self) -> set[str]:
		return {code for code, s in self.services.items() if s.clinical}

	@property
	def apprenticeship_services(self) -> dict[str, str]:
		"""Apprenticeship service code -> the service it graduates into."""
		return {code: s.graduates_to for code, s in self.services.items() if s.graduates_to}

	@property
	def all_disciplines(self) -> tuple[str, ...]:
		"""Every discipline this profile can place someone in.

		The union of what accounting can say, what the agenda vocabulary can
		name, what a colour rule can grant, and anything declared outright.
		A Department is emitted for each (`pipeline.employees`), because a
		Scheduling Role may name a discipline nobody happens to be filed under
		this month and a link with no target fails validation.

		Order is stable — declaration order, then first appearance — so ties in
		`pipeline.discipline` break the same way on every run.
		"""
		seen: dict[str, None] = dict.fromkeys(self.declared_disciplines)
		groups = (
			[s.discipline for s in self.services.values()],
			[name for _, name in self.discipline_labels],
			[r.get("discipline") for r in self.role_color_rules.values()],
			[self.default_discipline, self.zawin.get("prophylaxis_discipline")],
		)
		for group in groups:
			for name in group:
				if name:
					seen.setdefault(name, None)
		return tuple(seen)


def _resolve(path: str | Path | None) -> Path:
	if path:
		return Path(path)
	if os.environ.get("ZAWIN_PROFILE"):
		return Path(os.environ["ZAWIN_PROFILE"])
	# Only consult Frappe if the caller is already inside a site; never import
	# it just because it is installed in the same environment.
	frappe = sys.modules.get("frappe")
	if frappe is not None:
		try:
			configured = (frappe.conf or {}).get("zawin_profile")
		except Exception:
			configured = None
		if configured:
			p = Path(configured)
			return p if p.is_absolute() else PROFILE_DIR / p
	return DEFAULT_PROFILE


def _strip_comments(value):
	"""Drop `_`-prefixed keys anywhere in the profile.

	Profiles are meant to be read and edited by hand, so they carry `_comment`
	keys explaining each section — including inside `services` and
	`site_behort`, where an unexpected string would otherwise be parsed as
	configuration.
	"""
	if isinstance(value, dict):
		return {k: _strip_comments(v) for k, v in value.items() if not k.startswith("_")}
	if isinstance(value, list):
		return [_strip_comments(v) for v in value]
	return value


def load(path: str | Path | None = None) -> Profile:
	p = _resolve(path)
	if not p.is_file():
		raise FileNotFoundError(
			f"no practice profile at {p}. Copy profiles/example.json and point "
			f"ZAWIN_PROFILE (or site config 'zawin_profile') at it."
		)
	raw = _strip_comments(json.loads(p.read_text(encoding="utf-8")))
	log.info("practice profile: %s (%s)", raw.get("name", p.stem), p)

	services = {k: Service(**v) for k, v in raw.get("services", {}).items()}
	# A graduate service that does not exist would silently un-designate every
	# apprentice who finished, which is worse than the stale filing it fixes.
	for code, service in services.items():
		if service.graduates_to and service.graduates_to not in services:
			raise ValueError(
				f"{p.name}: service {code} graduates_to {service.graduates_to!r}, "
				f"which is not a service in this profile"
			)

	return Profile(
		name=raw.get("name", p.stem),
		source_path=p.resolve(),
		company=raw["company"]["name"],
		company_abbr=raw["company"]["abbr"],
		branches=list(raw.get("branches", [])),
		shift_types=list(raw.get("shift_types", [])),
		day_start=int(raw["practice_day"]["start"]),
		day_end=int(raw["practice_day"]["end"]),
		midday=int(raw["practice_day"]["midday"]),
		services=services,
		admin_service=raw.get("admin_service"),
		absence_categories=tuple(raw.get("categories", {}).get("absence", [])),
		non_clinical_categories=tuple(raw.get("categories", {}).get("non_clinical", [])),
		label_rules=[tuple(r) for r in raw.get("label_rules", [])],
		discipline_labels=[tuple(r) for r in raw.get("discipline_labels", [])],
		default_discipline=raw.get("default_discipline"),
		declared_disciplines=list(raw.get("disciplines", [])),
		non_discipline_agendas=set(raw.get("agendas", {}).get("non_discipline", [])),
		site_behort={int(k): v for k, v in raw.get("site_behort", {}).items()},
		role_color_rules={int(k): v for k, v in raw.get("role_color_rules", {}).items()},
		thresholds=raw.get("thresholds", {}),
		zawin=raw.get("zawin", {}),
		overrides={k: v for k, v in (raw.get("overrides") or {}).items() if v},
		resolvers={k: v for k, v in (raw.get("resolvers") or {}).items() if v},
	)


_current: Profile | None = None


def get() -> Profile:
	"""The active profile, loaded once."""
	global _current
	if _current is None:
		_current = load()
	return _current


def use(profile: Profile | str | Path | None) -> Profile:
	"""Set the active profile — by object, or by path to load."""
	global _current
	_current = profile if isinstance(profile, Profile) else load(profile)
	return _current
