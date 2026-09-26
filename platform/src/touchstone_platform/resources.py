from importlib.resources import files
from pathlib import Path

SCHEMAS = files("touchstone_platform").joinpath("_resources", "schemas")
if not SCHEMAS.is_dir():
    SCHEMAS = Path(__file__).resolve().parents[3] / "contracts" / "schemas"
