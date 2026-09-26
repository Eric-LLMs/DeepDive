"""Intent Registry (migration 0014, final ruling 2026-09-26): the LIVE-table
source of routable capabilities.

Layering:

* ``capabilities`` + ``capability_standard_queries`` /
  ``capability_similar_queries`` / ``capability_negatives`` ARE the runtime
  truth — no Draft, no Publish, no Projection;
* :mod:`.store` reads them (fingerprint-cached live view) and edits the
  intent rows; :mod:`.queries` edits the corpus with embed-then-write
  atomicity;
* :mod:`.snapshot` is the pure validation gate in front of every write;
* ``registry_versions`` keeps write-time history only.
"""
from .entry import (
    STATUS_ACTIVE,
    STATUS_DEPRECATED,
    STATUS_DISABLED,
    CapabilityEntry,
    QueryRecord,
    RegistryLiveView,
    derive_language,
)
from .snapshot import (
    VALID_POLICIES,
    VALID_SOURCES,
    RegistryValidationError,
    validate_entries,
)
from .store import (
    RegistryConflictError,
    RegistryError,
    RegistryNotFoundError,
    active_view,
    audit,
    content_fingerprint,
    create_capability,
    embedding_status,
    get_capability,
    get_version,
    invalidate_cache,
    list_audit,
    list_capabilities,
    list_versions,
    load_live_view,
    snapshot_history,
    update_capability,
    version_entries,
)

__all__ = [
    # entry
    "STATUS_ACTIVE",
    "STATUS_DEPRECATED",
    "STATUS_DISABLED",
    # snapshot (the write-time validation gate)
    "VALID_POLICIES",
    "VALID_SOURCES",
    "CapabilityEntry",
    "QueryRecord",
    # store (live-table read/edit + fingerprint cache + audit)
    "RegistryConflictError",
    "RegistryError",
    "RegistryLiveView",
    "RegistryNotFoundError",
    "RegistryValidationError",
    "active_view",
    "audit",
    "content_fingerprint",
    "create_capability",
    "derive_language",
    "embedding_status",
    "get_capability",
    "get_version",
    "invalidate_cache",
    "list_audit",
    "list_capabilities",
    "list_versions",
    "load_live_view",
    "snapshot_history",
    "update_capability",
    "validate_entries",
    "version_entries",
]
