"""Create new protected, disposable deployment credentials; never read provider keys."""

import argparse
import os
import secrets
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("directory", type=Path)
parser.add_argument(
    "--database", choices=("reckoner_smoke_compose", "reckoner_smoke_kind"), required=True
)
args = parser.parse_args()
os.umask(0o077)
args.directory.mkdir(parents=True, exist_ok=False)
owner_password = secrets.token_urlsafe(24)
values = {
    "RECKONER_OWNER_DSN": f"postgresql://postgres:{owner_password}@postgres:5432/{args.database}"
}
for kind in ("runner", "evaluator", "api"):
    password = secrets.token_urlsafe(24)
    values[f"RECKONER_{kind.upper()}_DSN"] = (
        f"postgresql://reckoner_{kind}_login:{password}@postgres:5432/{args.database}"
    )
    (args.directory / f"{kind}.env").write_text(
        f"RECKONER_{kind.upper()}_DSN={values[f'RECKONER_{kind.upper()}_DSN']}\n"
    )
(args.directory / "provision.env").write_text(
    "".join(f"{key}={value}\n" for key, value in values.items())
)
(args.directory / "smoke.env").write_text(
    "".join(f"{key}={values[key]}\n" for key in ("RECKONER_OWNER_DSN", "RECKONER_RUNNER_DSN"))
)
(args.directory / "postgres.env").write_text(
    f"POSTGRES_DB={args.database}\nPOSTGRES_USER=postgres\nPOSTGRES_PASSWORD={owner_password}\n"
)
print("Created protected disposable database credentials (no provider key).")
