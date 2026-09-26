"""Resource paths for an installed wheel or an editable workspace checkout."""

from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
PACKAGED = PACKAGE / "_resources"
ROOT = PACKAGE.parents[3]
SCHEMAS = PACKAGED / "schemas" if PACKAGED.is_dir() else ROOT / "contracts/schemas"
CONFIG = PACKAGED / "config" if PACKAGED.is_dir() else ROOT / "workloads/reckoner/config"
PROMPTS = PACKAGED / "prompts" if PACKAGED.is_dir() else ROOT / "workloads/reckoner/prompts"
