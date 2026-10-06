#!/usr/bin/env bash
# Download only the checksum-pinned GPLv3 community GDS plugin for disposable tests.
set -euo pipefail
repository_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
plugin_dir="$repository_dir/artifacts/phase3/task3/plugins"
plugin_file="$plugin_dir/neo4j-graph-data-science-2026.09.0.jar"
mkdir -p "$plugin_dir"
if [ ! -f "$plugin_file" ]; then
  curl --fail --silent --show-error --location \
    https://graphdatascience.ninja/neo4j-graph-data-science-2026.09.0.jar \
    --output "$plugin_file"
fi
printf '%s  %s\n' \
  74e7026ed7bad144c67473a0e4d47276907b781ee5e283ecab11245498ad2a8e \
  "$plugin_file" | shasum -a 256 --check
