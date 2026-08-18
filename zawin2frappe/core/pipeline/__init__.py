"""Stage 2: transform.

Imports neither `pymssql` nor `frappe` — DataFrames in, DataFrames out. This is
the layer intended to be vendored into a Frappe app later, so keep it free of
both the source driver and the target ORM.
"""
