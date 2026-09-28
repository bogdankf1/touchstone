"""The disposable platform smoke must never clean another Compose project."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run(tmp_path, project):
    executable = tmp_path / "bin"
    executable.mkdir()
    docker = executable / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$DOCKER_CALLS"\n'
        'case "$*" in *"volume ls"*) printf "sentinel-volume\\n";; esac\n'
        'case "$*" in *"port collector"*) printf "127.0.0.1:4318\\n";; esac\n'
    )
    docker.chmod(0o755)
    uv = executable / "uv"
    uv.write_text("#!/bin/sh\nexit 1\n")
    uv.chmod(0o755)
    calls = tmp_path / "docker-calls"
    environment = {
        **os.environ,
        "PATH": f"{executable}:{os.environ['PATH']}",
        "DOCKER_CALLS": str(calls),
        "TOUCHSTONE_COMPOSE_PROJECT": project,
    }
    result = subprocess.run(
        ["bash", str(ROOT / "infra/smoke-platform.sh")],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, calls.read_text() if calls.exists() else ""


def test_existing_measured_project_name_is_refused_before_docker(tmp_path):
    result, calls = _run(tmp_path, "touchstone-phase2-measured")
    assert result.returncode != 0
    assert calls == ""


def test_existing_smoke_volume_is_refused_and_preserved(tmp_path):
    result, calls = _run(tmp_path, "touchstone-phase2-smoke-sentinel")
    assert result.returncode != 0
    assert "volume ls" in calls
    assert not any(line.startswith("compose ") for line in calls.splitlines())
    assert not any(" down " in line for line in calls.splitlines())
