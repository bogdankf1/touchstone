"""Tenant-scoped reads from one immutable governed warehouse generation."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, localcontext

from touchstone_platform.refresh import open_published_snapshot
from touchstone_platform.settings import Settings
from touchstone_platform.staging import exact_cpst

RUN_COLUMNS = (
    "tenant_id, workflow_id, run_id, workflow_version, experiment_version, "
    "cohort_version, config_version, measurement_mode, dataset_simulated, "
    "declared_at, completeness_known, expected_tasks, received_tasks, "
    "completed_tasks, failed_tasks, missing_tasks, correct_tasks, missing_outcomes, "
    "unexpected_tasks, "
    "model_cost, review_cost, error_cost, currency, latency_population, "
    "latency_p99_ms, expected_cases, passed_cases, missing_checks, error_checks, "
    "conflicting_checks, metrics_complete, code_revision, dataset_version, "
    "price_table_version, declaration_versions"
)
COMPATIBILITY = (
    "workflow_version",
    "experiment_version",
    "cohort_version",
    "config_version",
    "measurement_mode",
    "dataset_simulated",
    "currency",
    "code_revision",
    "dataset_version",
    "price_table_version",
)
MONEY = ("model_cost", "review_cost", "error_cost")
COUNTS = (
    "expected_tasks",
    "received_tasks",
    "completed_tasks",
    "failed_tasks",
    "missing_tasks",
    "unexpected_tasks",
    "correct_tasks",
    "missing_outcomes",
    "latency_population",
    "expected_cases",
    "passed_cases",
    "missing_checks",
    "error_checks",
    "conflicting_checks",
)


def _text(value):
    return value.astimezone(UTC).isoformat() if isinstance(value, datetime) else value


def _money(value):
    return format(value, "f") if value is not None else None


def _rate(numerator, denominator):
    return numerator / denominator if numerator is not None and denominator else None


class SnapshotReader:
    def __init__(self, connection, manifest: dict, status: dict):
        self.connection = connection
        self.manifest = manifest
        self.status = status

    def metadata(self):
        state = self.status.get("state")
        return {
            "generation": self.manifest["generation"],
            "warehouse": self.manifest.get("warehouse", "DuckDB local preview"),
            "published_at": self.manifest.get("published_at"),
            "cutoff": self.manifest.get("cutoff"),
            "accepted_measurements": self.manifest.get("accepted_measurements"),
            "accepted_declarations": self.manifest.get("accepted_declarations"),
            "rejected_count": self.manifest.get("rejected_count"),
            "latest_refresh": {
                "state": state
                if state in {"succeeded", "failed", "published_with_warning"}
                else "unknown",
                "attempted_at": self.status.get("attempted_at"),
                "reason": "latest refresh failed"
                if state == "failed"
                else ("published with warning" if state == "published_with_warning" else None),
            },
        }

    def _rows(self, sql, params):
        result = self.connection.execute(sql, params)
        names = [column[0] for column in result.description]
        return [dict(zip(names, row, strict=True)) for row in result.fetchall()]

    def _runs(self, workflow_id=None, run_id=None, tenant_id=None):
        clauses, params = [], []
        for field, value in (
            ("workflow_id", workflow_id),
            ("run_id", run_id),
            ("tenant_id", tenant_id),
        ):
            if value is not None:
                clauses.append(f"{field} = ?")
                params.append(value)
        where = " where " + " and ".join(clauses) if clauses else ""
        return self._rows(
            f"select {RUN_COLUMNS} from mart_runs r "
            "left join (select tenant_id, workflow_id, run_id, "
            "count(distinct content_sha256) as declaration_versions, "
            "case when count(distinct content_sha256) = 1 then "
            "min(json_extract_string(document_json, '$.code_revision')) end as code_revision, "
            "case when count(distinct content_sha256) = 1 then "
            "min(json_extract_string(document_json, '$.dataset_version')) end as dataset_version "
            "from raw_declarations group by tenant_id, workflow_id, run_id) d "
            "using (tenant_id, workflow_id, run_id) "
            "left join (select tenant_id, workflow_id, run_id, "
            "case when count(distinct price_table_version) = 1 then "
            "min(price_table_version) end as price_table_version "
            "from int_calls group by tenant_id, workflow_id, run_id) p "
            f"using (tenant_id, workflow_id, run_id){where} "
            "order by workflow_id, run_id, tenant_id",
            params,
        )

    def workflows(self):
        rows = self._rows(
            "select workflow_id, count(distinct tenant_id) as tenant_count, "
            "count(*) as run_count, "
            "array_agg(distinct tenant_id order by tenant_id) as tenant_ids "
            "from mart_runs group by workflow_id order by workflow_id",
            [],
        )
        return rows

    def runs(self, workflow_id: str, tenant_id: str):
        return [self._present_run(row) for row in self._runs(workflow_id, tenant_id=tenant_id)]

    def _present_run(self, row):
        result = {key: _text(value) for key, value in row.items()}
        for key in MONEY:
            result[key] = _money(row[key])
        result["cpst"] = _money(
            exact_cpst(
                row["model_cost"],
                row["review_cost"],
                row["error_cost"],
                row["correct_tasks"],
                row["metrics_complete"],
            )
        )
        result["correctness"] = _rate(row["correct_tasks"], row["expected_tasks"])
        result["completion"] = _rate(row["completed_tasks"], row["expected_tasks"])
        result["evaluation_pass_rate"] = _rate(row["passed_cases"], row["expected_cases"])
        result["metric_definitions"] = {
            "cpst": "v1",
            "correctness": "v1",
            "completion": "v1",
            "latency_p99": "v1",
            "evaluation_pass_rate": "v1",
        }
        return result

    def _compatible(self, rows):
        declared = [
            row for row in rows if row["completeness_known"] and row["declaration_versions"] == 1
        ]
        if not declared:
            return [], sorted(row["tenant_id"] for row in rows)
        groups = {}
        for row in declared:
            key = tuple(row[field] for field in COMPATIBILITY)
            groups.setdefault(key, []).append(row)
        included = sorted(groups.values(), key=lambda group: (-len(group), group[0]["tenant_id"]))[
            0
        ]
        selected = {row["tenant_id"] for row in included}
        return included, sorted(
            row["tenant_id"] for row in rows if row["tenant_id"] not in selected
        )

    def summary(
        self,
        workflow_id: str,
        run_id: str,
        *,
        tenant_id: str | None = None,
        aggregate: bool = False,
    ):
        rows = self._runs(workflow_id, run_id, None if aggregate else tenant_id)
        if not rows:
            return None
        if aggregate:
            rows, excluded = self._compatible(rows)
            if not rows:
                return None
            result = self._aggregate(workflow_id, run_id, rows)
            result["excluded_tenants"] = excluded
            result["tenants"] = [self._present_run(row) for row in rows]
            result["tenant_ids"] = [row["tenant_id"] for row in rows]
        else:
            result = self._present_run(rows[0])
            result["tenant_ids"] = [tenant_id]
            result["excluded_tenants"] = []
            result["tenants"] = []
        result["quality"] = self.quality(workflow_id, run_id, result["tenant_ids"])
        result["contribution_rates"] = self.contribution_rates(
            workflow_id, run_id, result["tenant_ids"]
        )
        return result

    def quality(self, workflow_id, run_id, tenant_ids):
        placeholders = ", ".join("?" for _ in tenant_ids)
        where = f"tenant_id in ({placeholders}) and workflow_id = ? and run_id = ?"
        params = [*tenant_ids, workflow_id, run_id]
        identities = self.connection.execute(
            f"select count(*) from stg_events where {where} and identity_conflict", params
        ).fetchone()[0]
        incomplete, unavailable = self.connection.execute(
            "select count(*) filter (where incomplete), "
            f"count(*) filter (where unavailable > 0) from int_calls where {where}",
            params,
        ).fetchone()
        rejections = self.connection.execute(
            f"select count(*) from raw_rejections where {where}", params
        ).fetchone()[0]
        return {
            "identity_conflicts": identities,
            "incomplete_calls": incomplete,
            "unavailable_prices": unavailable,
            "rejected_events": rejections,
        }

    def _aggregate(self, workflow_id, run_id, rows):
        common = rows[0]
        result = {key: common[key] for key in COMPATIBILITY}
        result.update(
            {
                "workflow_id": workflow_id,
                "run_id": run_id,
                "tenant_id": None,
                "declared_at": min(_text(row["declared_at"]) for row in rows),
                "completeness_known": all(row["completeness_known"] for row in rows),
                "metrics_complete": all(row["metrics_complete"] for row in rows),
                "declaration_versions": 1,
            }
        )
        for key in COUNTS:
            result[key] = (
                sum(row[key] for row in rows) if all(row[key] is not None for row in rows) else None
            )
        with localcontext() as context:
            # A component can use all 38 warehouse digits; summing tenants needs headroom.
            context.prec = 38 + len(str(len(rows)))
            for key in MONEY:
                result[key] = (
                    sum((row[key] for row in rows), Decimal(0))
                    if all(row[key] is not None for row in rows)
                    else None
                )
        result["latency_p99_ms"] = self._pooled_p99(
            workflow_id, run_id, [row["tenant_id"] for row in rows]
        )
        return self._present_run(result)

    def _pooled_p99(self, workflow_id, run_id, tenant_ids):
        placeholders = ", ".join("?" for _ in tenant_ids)
        sql = (
            "select quantile_disc(latency_ms, 0.99) from int_tasks "
            f"where tenant_id in ({placeholders}) and workflow_id = ? and run_id = ? "
            "and terminal_status = 'completed' and latency_ms is not null"
        )
        return self.connection.execute(sql, [*tenant_ids, workflow_id, run_id]).fetchone()[0]

    def contribution_rates(self, workflow_id, run_id, tenant_ids):
        placeholders = ", ".join("?" for _ in tenant_ids)
        rows = self._rows(
            "select metric_id, definition_version, unit, expected_tasks, "
            "task_contributions, incomplete_contributions, unexpected_tasks, missing_tasks, "
            "numerator_sum, denominator_sum, eligible_numerator, eligible_denominator "
            f"from mart_contribution_rates where tenant_id in ({placeholders}) "
            "and workflow_id = ? and run_id = ? order by metric_id, definition_version, unit",
            [*tenant_ids, workflow_id, run_id],
        )
        grouped = {}
        for row in rows:
            key = (row["metric_id"], row["definition_version"], row["unit"])
            grouped.setdefault(key, []).append(row)
        output = []
        for (metric_id, version, unit), members in sorted(grouped.items()):
            expected = (
                sum(row["expected_tasks"] for row in members)
                if len(members) == len(tenant_ids)
                and all(row["expected_tasks"] is not None for row in members)
                else None
            )
            observed_numerators = [
                row["numerator_sum"] for row in members if row["numerator_sum"] is not None
            ]
            observed_denominators = [
                row["denominator_sum"] for row in members if row["denominator_sum"] is not None
            ]
            numerator = sum(observed_numerators) if observed_numerators else None
            denominator = sum(observed_denominators) if observed_denominators else None
            eligible = expected is not None and all(
                row["eligible_numerator"] is not None and row["eligible_denominator"] is not None
                for row in members
            )
            output.append(
                {
                    "metric_id": metric_id,
                    "definition_version": version,
                    "unit": unit,
                    "expected_tasks": expected,
                    "task_contributions": sum(row["task_contributions"] for row in members),
                    "incomplete_contributions": sum(
                        row["incomplete_contributions"] for row in members
                    ),
                    "unexpected_tasks": sum(row["unexpected_tasks"] or 0 for row in members),
                    "missing_tasks": sum(row["missing_tasks"] or 0 for row in members)
                    if expected is not None
                    else None,
                    "numerator": numerator,
                    "denominator": denominator,
                    "rate": _rate(
                        sum(row["eligible_numerator"] for row in members),
                        sum(row["eligible_denominator"] for row in members),
                    )
                    if eligible
                    else None,
                }
            )
        return output

    def tasks(self, workflow_id, run_id, tenant_ids, page, page_size):
        placeholders = ", ".join("?" for _ in tenant_ids)
        where = f"tenant_id in ({placeholders}) and workflow_id = ? and run_id = ?"
        params = [*tenant_ids, workflow_id, run_id]
        total = self.connection.execute(
            f"select count(*) from int_tasks where {where}", params
        ).fetchone()[0]
        rows = self._rows(
            "select tenant_id, workflow_id, run_id, task_id, terminal_status, latency_ms, "
            f"incomplete from int_tasks where {where} order by tenant_id, task_id "
            "limit ? offset ?",
            [*params, page_size, (page - 1) * page_size],
        )
        for row in rows:
            node_rows = self._rows(
                "select node_name, currency, model_cost, call_count, incomplete, trace_id, "
                "evidence_event_id from mart_nodes where tenant_id = ? and workflow_id = ? "
                "and run_id = ? and task_id = ? order by node_name",
                [row["tenant_id"], workflow_id, run_id, row["task_id"]],
            )
            for node in node_rows:
                node["model_cost"] = _money(node["model_cost"])
            row["nodes"] = node_rows
        return {"items": rows, "page": page, "page_size": page_size, "total": total}

    def trace(self, trace_id, workflow_id, run_id, tenant_id, page=1, page_size=100):
        params = [tenant_id, workflow_id, run_id, trace_id]
        where = "tenant_id = ? and workflow_id = ? and run_id = ? and trace_id = ?"
        total = self.connection.execute(
            f"select count(*) from stg_events where {where}", params
        ).fetchone()[0]
        if not total:
            return None
        rows = self._rows(
            "select tenant_id, workflow_id, run_id, task_id, event_id, event_kind, "
            "node_name, trace_id, span_id, received_at, identity_conflict "
            f"from stg_events where {where} order by received_at, event_id "
            "limit ? offset ?",
            [*params, page_size, (page - 1) * page_size],
        )
        return {
            "trace_id": trace_id,
            "events": [{key: _text(value) for key, value in row.items()} for row in rows],
            "page": page,
            "page_size": page_size,
            "total": total,
        }


@contextmanager
def open_snapshot(settings: Settings):
    """Resolve metadata once, then open that exact generation read-only."""
    try:
        manifest = json.loads((settings.warehouse_dir / "current.json").read_text())
    except json.JSONDecodeError as error:
        raise ValueError("invalid published manifest") from error
    if not isinstance(manifest, dict) or not isinstance(manifest.get("generation"), str):
        raise ValueError("invalid published manifest")
    status_path = settings.warehouse_dir / "refresh-status.json"
    try:
        status = json.loads(status_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        status = {}
    with open_published_snapshot(settings, manifest=manifest) as connection:
        yield SnapshotReader(connection, manifest, status)
