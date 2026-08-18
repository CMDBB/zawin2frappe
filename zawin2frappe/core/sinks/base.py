"""The sink boundary."""
from __future__ import annotations

from typing import Protocol

import pandas as pd


class Sink(Protocol):
    """Where built records go.

    Deliberately minimal: everything a sink needs is the target doctype and a
    frame whose columns are already Frappe field names. Keeping it this small
    is what lets a Frappe-native loader drop in later without stage 2 changing.
    """

    def write(self, doctype: str, rows: pd.DataFrame) -> None: ...

    def finalise(self) -> None: ...
