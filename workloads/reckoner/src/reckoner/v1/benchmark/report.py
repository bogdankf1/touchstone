"""Immutable JSON and readable Markdown; unavailable is never a fabricated zero."""

import json
from pathlib import Path

from reckoner.contracts import content_id


def write_report(report: dict, output: Path) -> dict:
    output = Path(output)
    json_path, markdown_path = output.with_suffix(".json"), output.with_suffix(".md")
    if json_path.exists() or markdown_path.exists():
        raise FileExistsError("benchmark report already exists")
    body = {**report, "report_id": content_id(report)}
    output.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
    markdown_path.write_text(
        "# Simulated-data retrieval benchmark\n\n"
        f"Report `{body['report_id']}`. "
        f"Query count: {body.get('query_count', 'unavailable')}.\n\n"
        "Exact neighbourhood matches: "
        f"{body.get('exact_neighbourhood_matches', 'unavailable')}; "
        "exact eligible-candidate matches: "
        f"{body.get('exact_candidate_matches', 'unavailable')}.\n\n"
        f"Decision impact: {body.get('decision_impact', {}).get('status', 'unavailable')}. "
        "No provider calls are performed by this report writer.\n\n"
        "## Evidence and limits\n\n```json\n"
        + json.dumps({k: v for k, v in body.items() if k != "queries"}, indent=2, sort_keys=True)
        + "\n```\n"
    )
    return body
