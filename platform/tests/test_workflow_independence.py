"""The second workload crosses into Touchstone only through OTLP contracts."""

import ast
from pathlib import Path


def test_synthetic_runtime_has_no_reckoner_or_platform_implementation_imports():
    source = (
        Path(__file__).resolve().parents[2]
        / "workloads"
        / "synthetic"
        / "src"
        / "touchstone_synthetic"
    )
    assert source.exists()
    for path in source.glob("*.py"):
        tree = ast.parse(path.read_text())
        modules = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        modules += [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ]
        assert not any(
            module
            and (
                module == "reckoner"
                or module.startswith("reckoner.")
                or module == "touchstone_platform"
                or module.startswith("touchstone_platform.")
            )
            for module in modules
        ), path
