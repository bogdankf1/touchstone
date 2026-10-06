"""Static checks of the Phase 2 platform Compose stack (no Docker run)."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_dagster_instance_storage_is_initialized_once_before_its_processes_start():
    # Dagster's SQLite storage check-then-stamp is not atomic: concurrent first starts on an
    # empty DAGSTER_HOME crash or leave two alembic heads that break every later instance load.
    services = yaml.safe_load((ROOT / "infra/compose.platform.yaml").read_text())["services"]
    init = services["dagster-init"]
    assert "dagster instance migrate" in " ".join(init["command"])
    assert init["environment"]["DAGSTER_HOME"] == services["dagster"]["environment"]["DAGSTER_HOME"]
    assert init["volumes"] == services["dagster"]["volumes"]
    for name in ("dagster", "dagster-daemon"):
        assert services[name]["depends_on"]["dagster-init"] == {
            "condition": "service_completed_successfully"
        }
