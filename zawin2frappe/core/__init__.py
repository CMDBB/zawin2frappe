"""ZaWin extraction core — deliberately free of Frappe.

Nothing in this package imports `frappe`, so it runs standalone against a
restored ZaWin backup with no site, no bench and no database beyond MSSQL. That
is what keeps schema forensics (`introspect`, `profile`, `fkscan`) usable for
ad-hoc digging, and what lets the transform layer be tested without a site.

`zawin2frappe.loaders` holds the pieces that do talk to Frappe.
"""

__all__ = [
    "db", "introspect", "profile", "fkscan", "extract", "shifts",
    "crosswalk", "roster", "settings", "pipeline", "sinks",
]
