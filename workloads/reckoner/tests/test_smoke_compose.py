"""Exercise orchestration without starting Docker or accessing provider credentials."""

import json
import os
import subprocess
import sys
from pathlib import Path


def test_smoke_verifies_private_exports_in_container(tmp_path):
    """Host verification cannot read files owned privately by the image UID."""
    root = Path(__file__).resolve().parents[3]
    commands = tmp_path / "commands.jsonl"
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    docker = executable_dir / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['SMOKE_COMMANDS'], 'a') as output:\n"
        "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if 'psql' in sys.argv:\n"
        "    print('4')\n"
    )
    docker.chmod(0o755)
    host_python = executable_dir / "python3"
    host_python.write_text(
        "#!/bin/sh\n"
        "echo 'PermissionError: host cannot read image-owned manifest.json' >&2\n"
        "exit 1\n"
    )
    host_python.chmod(0o755)
    result = subprocess.run(
        ["bash", "infra/smoke-compose.sh"],
        cwd=root,
        env={
            **os.environ,
            "PATH": f"{executable_dir}:{os.environ['PATH']}",
            "SMOKE_COMMANDS": str(commands),
            "RECKONER_SECRET_DIR": str(tmp_path / "secrets"),
            "RECKONER_EXPORT_DIR": str(tmp_path / "evidence"),
            "RECKONER_BUILD_REVISION": "a" * 40,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    dispatched = [json.loads(line) for line in commands.read_text().splitlines()]
    assert any(command[-4:] == ["run", "--rm", "--no-deps", "verifier"] for command in dispatched)
