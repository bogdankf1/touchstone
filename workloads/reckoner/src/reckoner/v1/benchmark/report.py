"""Immutable JSON and readable Markdown; unavailable is never a fabricated zero."""

import json
import os
import re
from pathlib import Path
from uuid import uuid4

from reckoner.contracts import content_id

RETRIEVAL_SCHEMA = "reckoner-retrieval-benchmark-v1"
COMPARISON_SCHEMA = "reckoner-comparison-v1"
# Measured runs and synthetic fixtures never share a heading or label.
MEASUREMENT_MODES = {
    "measured-local-retrieval": (
        "Measured simulated-data retrieval benchmark",
        "Measured locally against stores prepared from the simulated dataset. "
        "Not production traffic.",
    ),
    "synthetic-fixture": (
        "Synthetic-fixture retrieval benchmark",
        "Synthetic fixture: demonstrates plumbing only and cannot satisfy a measured gate.",
    ),
}
COMPARISON_MODES = {
    "measured-comparison": (
        "Measured Reckoner v1 arm comparison",
        "Arms measured on the simulated dataset. Not production traffic.",
    ),
    "synthetic-fixture": (
        "Synthetic-fixture Reckoner v1 arm comparison",
        "Synthetic fixture: demonstrates plumbing only and cannot satisfy a measured gate.",
    ),
}


def write_report(report: dict, output: Path) -> dict:
    output = Path(output)
    if output.suffix.lower() in (".json", ".md"):
        # A file name here would otherwise publish report.json.json and report.json.md.
        raise ValueError("report output is a prefix; omit the .json/.md extension")
    # The whole prefix is kept: "report.v2" becomes report.v2.json, not report.json.
    json_path = output.with_name(output.name + ".json")
    markdown_path = output.with_name(output.name + ".md")
    modes = (
        COMPARISON_MODES if report.get("schema_version") == COMPARISON_SCHEMA else MEASUREMENT_MODES
    )
    if report.get("measurement_mode") not in modes:
        raise ValueError(
            "report requires an explicit measurement_mode: " + ", ".join(sorted(modes))
        )
    if json_path.exists() or markdown_path.exists():
        raise FileExistsError("benchmark report already exists")
    body = {**report, "report_id": content_id(report)}
    markdown = (
        _comparison_markdown(body)
        if body.get("schema_version") == COMPARISON_SCHEMA
        else _retrieval_markdown(body)
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    _publish_pair(
        [(json_path, json.dumps(body, indent=2, sort_keys=True) + "\n"), (markdown_path, markdown)]
    )
    return body


def _publish_pair(files):
    """Write temporaries beside the targets, then hard-link; link never replaces a file."""
    temporaries = []
    try:
        for path, text in files:
            temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            temporaries.append(temporary)
            with os.fdopen(descriptor, "w") as handle:
                handle.write(text)
        published = []
        try:
            for (path, _), temporary in zip(files, temporaries, strict=True):
                os.link(temporary, path)
                published.append(path)
        except BaseException:
            for path in published:
                path.unlink()
            raise
    finally:
        for temporary in temporaries:
            temporary.unlink(missing_ok=True)


def _retrieval_markdown(body):
    heading, label = MEASUREMENT_MODES[body["measurement_mode"]]
    return (
        f"# {heading}\n\n"
        f"Measurement mode: `{body['measurement_mode']}`. {label}\n\n"
        f"Report `{body['report_id']}`. "
        f"Query count: {body.get('query_count', 'unavailable')}.\n\n"
        "Exact neighbourhood matches: "
        f"{body.get('exact_neighbourhood_matches', 'unavailable')}; "
        "exact eligible-candidate matches: "
        f"{body.get('exact_candidate_matches', 'unavailable')}.\n\n"
        f"Decision impact: {body.get('decision_impact', {}).get('status', 'unavailable')}. "
        "No provider calls are performed by this report writer.\n\n"
        "## Evidence and limits\n\n"
        + _fenced(
            json.dumps({k: v for k, v in body.items() if k != "queries"}, indent=2, sort_keys=True)
        )
    )


def _fenced(text):
    """A fence longer than any backtick run in the content, so content cannot close it."""
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", text)), default=0))
    return f"{fence}json\n{text}\n{fence}\n"


def _cell(value):
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return re.sub(r"([\\`*_\[\]<>|#])", r"\\\1", " ".join(text.split()))


def _comparison_markdown(body):
    arms = [
        "| Arm | Execution mode | Configuration | Model | Prompt | Question | Calibration "
        "| Evidence | Retrieval window | Declared membership complete |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for arm in body.get("arms", []):
        arms.append(
            "| "
            + " | ".join(
                _cell(arm.get(key, "unavailable"))
                for key in (
                    "arm_id",
                    "execution_mode",
                    "config_version",
                    "model_version",
                    "prompt_version",
                    "question_version",
                    "calibration_id",
                    "evidence_version",
                    "retrieval_window",
                    "membership_complete",
                )
            )
            + " |"
        )
    comparisons = [
        "| Baseline | Current | Eligibility | Reasons | CPST delta |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in body.get("comparisons", []):
        comparisons.append(
            "| "
            + " | ".join(
                [
                    _cell(item["baseline"]),
                    _cell(item["current"]),
                    "eligible" if item["eligible"] else "ineligible",
                    _cell("; ".join(item["reasons"]) or "none"),
                    _cell(item["delta_cpst"]) if item["delta_cpst"] is not None else "unavailable",
                ]
            )
            + " |"
        )
    heading, label = COMPARISON_MODES[body["measurement_mode"]]
    return (
        f"# {heading}\n\n"
        f"Measurement mode: `{body['measurement_mode']}`. {label}\n\n"
        f"Report `{body['report_id']}`. "
        f"Declared cases: {len(body.get('expected_ids', []))}. "
        "An ineligible comparison never shows a CPST delta. "
        "No provider calls are performed by this report writer.\n\n"
        "## Arms\n\n" + "\n".join(arms) + "\n\n"
        "## Comparisons\n\n" + "\n".join(comparisons) + "\n\n"
        "## Full record\n\n" + _fenced(json.dumps(body, indent=2, sort_keys=True))
    )
