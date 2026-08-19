"""Frappe Data Import CSV output."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)


class CsvSink:
	"""One CSV per doctype, plus a manifest describing the run.

	Files are named after the doctype as Frappe expects on import
	("Shift Assignment" -> shift_assignment.csv). The manifest records row
	counts and any notes the builders attached, so an import can be traced back
	to what produced it.
	"""

	def __init__(self, out_dir: Path | str, *, notes: dict | None = None):
		self.out_dir = Path(out_dir)
		self.out_dir.mkdir(parents=True, exist_ok=True)
		self.manifest: dict = {"outputs": {}, "notes": dict(notes or {})}

	@staticmethod
	def filename(doctype: str) -> str:
		return doctype.lower().replace(" ", "_").replace("-", "_") + ".csv"

	def write(self, doctype: str, rows: pd.DataFrame) -> None:
		path = self.out_dir / self.filename(doctype)
		rows.to_csv(path, index=False, encoding="utf-8")
		self.manifest["outputs"][doctype] = {
			"file": path.name,
			"rows": len(rows),
			"columns": list(rows.columns),
		}
		log.info("%-22s %6d rows -> %s", doctype, len(rows), path.name)

	def note(self, key: str, value) -> None:
		self.manifest["notes"][key] = value

	def finalise(self) -> None:
		path = self.out_dir / "manifest.json"
		path.write_text(json.dumps(self.manifest, indent=2, default=str), encoding="utf-8")
		log.info("manifest -> %s", path)
