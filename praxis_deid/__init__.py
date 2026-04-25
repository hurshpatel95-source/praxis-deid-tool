"""Praxis practice-side de-identification tool.

Runs at the practice on the practice's own infrastructure. Reads raw practice
management data, applies HIPAA Safe Harbor de-identification rules, and
produces output that's safe to send to Praxis cloud — patient data never
leaves the practice in identifiable form.

Open source by design: practice IT and compliance teams are expected to read
this code before running it.

Public re-exports:
"""

from .config import Config, load_config
from .deidentify import Deidentifier
from .hashing import stable_external_id

__all__ = ["Config", "Deidentifier", "load_config", "stable_external_id"]
__version__ = "0.1.0"
