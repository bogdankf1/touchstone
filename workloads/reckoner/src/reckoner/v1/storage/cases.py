"""Safe SQL views and keyset pagination; identifiers never become SQL syntax."""

import base64
import json

from pydantic import TypeAdapter

from reckoner.v1.api.schemas import CaseStatus, OpaqueID


def list_cases(
    repo,
    *,
    tenant_id: str,
    run_id: str | None = None,
    status: str = "open",
    limit: int = 50,
    cursor: str | None = None,
) -> dict:
    TypeAdapter(OpaqueID).validate_python(tenant_id)
    TypeAdapter(CaseStatus).validate_python(status)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("invalid limit")
    scope = [tenant_id, run_id, status]
    after = ""
    if cursor is not None:
        try:
            if len(cursor) > 4096:
                raise ValueError("cursor too long")
            token = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if set(token) != {"scope", "after"} or token["scope"] != scope:
                raise ValueError("cursor scope mismatch")
            after = TypeAdapter(OpaqueID).validate_python(token["after"])
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise ValueError("invalid cursor") from exc
    filters = {
        "all": "true",
        "open": "status='open'",
        "resolved": "status='resolved'",
        "note_pending": "note_status='pending'",
        "note_failed": "note_status NOT IN ('pending','succeeded')",
        "degraded": "degraded",
    }
    rows = repo._connection.execute(
        "SELECT tenant_id,case_id,decision_id,run_id,transaction_id,status,version,created_at,"
        "note_status,degraded,amount_minor,currency FROM reckoner.api_v1_case_details "
        "WHERE tenant_id=%s AND (%s::text IS NULL OR run_id=%s) AND case_id>%s AND "
        + filters[status]
        + " ORDER BY case_id LIMIT %s",
        (tenant_id, run_id, run_id, after, limit + 1),
    ).fetchall()
    next_cursor = None
    if len(rows) > limit:
        next_cursor = base64.urlsafe_b64encode(
            json.dumps({"scope": scope, "after": rows[limit - 1]["case_id"]}).encode()
        ).decode()
    return {"items": rows[:limit], "next_cursor": next_cursor, "limit": limit}


def get_case(repo, *, tenant_id: str, case_id: str) -> dict:
    row = repo._connection.execute(
        "SELECT * FROM reckoner.api_v1_case_details WHERE tenant_id=%s AND case_id=%s",
        (tenant_id, case_id),
    ).fetchone()
    if row is None:
        raise LookupError("case not found")
    return row
