"""Phase-C extractors: practice-side per-extension data pull.

Each module reads from a per-PMS source (default: Open Dental MySQL/MariaDB)
using a hand-curated mapping config from `mappings/<pms>/<extension>.json`,
applies the existing locked Safe Harbor de-id pipeline (`praxis_deid.deidentify`,
`praxis_deid.safe_harbor`, `praxis_deid.hashing`), and emits a canonical CSV
file that Praxis cloud's L1->L2 aggregator already accepts.

Extractors NEVER call out to Claude or any other network service. They are
pure deterministic transformations:

    raw row dicts -> column mapping -> Safe Harbor -> canonical CSV row

Public re-exports:
"""

from __future__ import annotations

from .base import (
    BaseExtractor,
    ExtractorError,
    Filter,
    MappingConfig,
    load_mapping_config,
)

__all__ = [
    "BaseExtractor",
    "ExtractorError",
    "Filter",
    "MappingConfig",
    "load_mapping_config",
]
