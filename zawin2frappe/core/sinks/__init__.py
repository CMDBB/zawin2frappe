"""Stage 3: emit.

`CsvSink` writes Frappe Data Import files today. A `FrappeDocSink` calling
`frappe.get_doc(...).insert()` is the intended successor and is the only piece
that would need a bench context — stage 2 does not change either way.
"""

from .base import Sink
from .csv_sink import CsvSink
from .policy import is_blank, needs_replacement, preserve_existing

__all__ = ["CsvSink", "Sink", "is_blank", "needs_replacement", "preserve_existing"]
