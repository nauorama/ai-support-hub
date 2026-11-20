"""Privacy functions (security §6, P8-05): the retention job and the erasure of one customer.

Both work on our own tables only and return the ticket ids whose LangGraph checkpoints must go
too: the checkpoints belong to the graph's store (`hub.graph.checkpoint`), which this module does
not import (import-linter); the API deletes them after the commit. The audit log is never
changed: it holds only internal ids, and the app role cannot change it (security §4, §5).
"""

from hub.privacy.erasure import ErasureReport, erase_customer
from hub.privacy.retention import RetentionReport, run_retention

__all__ = ["ErasureReport", "RetentionReport", "erase_customer", "run_retention"]
