"""Sinks that talk to Frappe.

Kept out of `core` so that package stays importable without Frappe.
"""

from .frappe_sink import FrappeDocSink

__all__ = ["FrappeDocSink"]
