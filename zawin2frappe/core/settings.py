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
	#: TAGPLAN ids that are rooms/purposes rather than disciplines
	non_discipline_agendas: set[int]
	#: BEHORT id -> branch name
	site_behort: dict[int, str]
	#: FarbeTermin (OLE_COLOR int) -> {"role": ..., "discipline": ..., "max_rooms": ...}
	#: a person holds that Scheduling Role in addition to their designation-derived one.
	role_color_rules: dict[int, dict[str, Any]]
	thresholds: dict[str, float]
	zawin: dict[str, Any] = field(default_factory=dict)
	#: name -> path, relative to source_path's directory unless absolute
	overrides: dict[str, str] = field(default_factory=dict)

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
		services={k: Service(**v) for k, v in raw.get("services", {}).items()},
		admin_service=raw.get("admin_service"),
		absence_categories=tuple(raw.get("categories", {}).get("absence", [])),
		non_clinical_categories=tuple(raw.get("categories", {}).get("non_clinical", [])),
		label_rules=[tuple(r) for r in raw.get("label_rules", [])],
		non_discipline_agendas=set(raw.get("agendas", {}).get("non_discipline", [])),
		site_behort={int(k): v for k, v in raw.get("site_behort", {}).items()},
		role_color_rules={int(k): v for k, v in raw.get("role_color_rules", {}).items()},
		thresholds=raw.get("thresholds", {}),
		zawin=raw.get("zawin", {}),
		overrides={k: v for k, v in (raw.get("overrides") or {}).items() if v},
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
