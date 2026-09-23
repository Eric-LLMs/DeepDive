"""Intent Registry (QIR P1): the versioned, DB-backed source of routable capabilities.

Layering (docs/temp.md §8.2/§8.3):

* ``capabilities`` rows are the editable Draft (this package's ``store`` CRUD);
* publishing freezes the whole draft set into an immutable ``registry_versions``
  payload; runtime reads ONLY the active version (never the draft table);
* Validate/Preview/Build-Then-Swap around these primitives is step 2; the Matcher
  starts reading this Registry (shadow) in step 3.
"""
from .types import (
    STATE_ACTIVE,
    STATE_FAILED,
    STATE_STAGED,
    STATE_SUPERSEDED,
    STATUS_ACTIVE,
    STATUS_DEPRECATED,
    STATUS_DISABLED,
    CapabilityEntry,
    RegistryVersionView,
)
from .store import (
    RegistryConflictError,
    RegistryError,
    RegistryNotFoundError,
    RegistryStateError,
    activate_version,
    active_view,
    content_fingerprint,
    create_draft,
    get_draft,
    get_version,
    invalidate_cache,
    list_drafts,
    list_versions,
    mark_failed,
    rollback,
    stage_version,
    update_draft,
)
