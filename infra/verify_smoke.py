"""Assert fixed fabricated deployment results and tenant-scoped API reads."""

import hashlib
import json
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

root = Path(sys.argv[1])
base = sys.argv[2]
report = json.loads((root / "report/report.json").read_text())
assert report["run"]["provider_call_mode"] == "fake"
assert report["run"]["execution_mode"] == "test"
assert report["counts"]["expected"] == report["counts"]["completed"] == 4
assert report["aggregate"]["metrics"]["cpst"]["value"] == "4.000066"
for directory in ("runner-otlp", "evaluator-otlp"):
    manifest = json.loads((root / directory / "manifest.json").read_text())
    assert manifest["request_count"] == len(manifest["requests"]) == 4
    for entry in manifest["requests"]:
        payload = (root / directory / entry["filename"]).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == entry["sha256"]
for endpoint in ("/health/live", "/health/ready"):
    with urlopen(base + endpoint, timeout=5) as response:
        assert response.status == 200
for tenant in ("tenant-a", "tenant-b"):
    with urlopen(
        base + f"/tenants/{tenant}/runs/fabricated-smoke-v1/results", timeout=5
    ) as response:
        document = json.load(response)
        assert len(document["items"]) == 2
        expected = (
            {"smoke-transaction-0", "smoke-transaction-1"}
            if tenant == "tenant-a"
            else {"smoke-transaction-2", "smoke-transaction-3"}
        )
        assert {row["transaction_id"] for row in document["items"]} == expected
try:
    urlopen(base + "/tenants/not-a-tenant/runs/fabricated-smoke-v1", timeout=5)
except HTTPError as error:
    assert error.code == 404
else:
    raise AssertionError("unknown tenant must return 404")
print("Verified four fake cases, exact CPST, both exports, readiness, and tenant isolation.")
