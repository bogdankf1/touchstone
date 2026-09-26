"""Embed the source revision; runtime hashes the actual installed package bytes."""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        # Editable installs read current checkout provenance directly.
        if version == "editable":
            return
        revision = os.environ.get("RECKONER_BUILD_REVISION")
        if revision is None:
            revision = subprocess.check_output(
                ["git", "-C", self.root, "rev-parse", "HEAD"], text=True
            ).strip()
        if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
            raise ValueError("RECKONER_BUILD_REVISION must be a full Git commit ID")
        descriptor, filename = tempfile.mkstemp(prefix="reckoner-build-", suffix=".json")
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"revision": revision}, stream, sort_keys=True)
        self.metadata_path = filename
        build_data["force_include"][filename] = "reckoner/_resources/build.json"

    def finalize(self, version, build_data, artifact_path):
        if version != "editable":
            Path(self.metadata_path).unlink()
