"""Audit log with a hash chain (FR-15, NFR-12; docs/security.md §5)."""

from hub.audit.chain import GENESIS_HASH, AuditDetailsError
from hub.audit.log import ChainCheck, record, verify_chain

__all__ = ["GENESIS_HASH", "AuditDetailsError", "ChainCheck", "record", "verify_chain"]
