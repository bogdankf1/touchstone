GRANT SELECT ON reckoner.evaluations TO reckoner_evaluator;
GRANT SELECT ON reckoner.runner_telemetry_outbox TO reckoner_evaluator;
GRANT SELECT ON public.reckoner_schema_migrations TO reckoner_api;

CREATE VIEW reckoner.api_run_summaries AS
SELECT
  r.tenant_id,
  r.run_id,
  r.purpose,
  r.config_id,
  r.bundle_id,
  r.status,
  r.created_at,
  count(t.task_id)::integer AS expected_count,
  count(t.task_id) FILTER (WHERE t.status = 'completed')::integer AS completed_count,
  count(t.task_id) FILTER (WHERE t.status = 'failed')::integer AS failed_count,
  count(t.task_id) FILTER (WHERE t.status = 'uncertain')::integer AS uncertain_count,
  count(t.task_id) FILTER (WHERE t.status IN ('pending', 'dispatched'))::integer AS pending_count,
  count(e.evaluation_id)::integer AS evaluated_count,
  count(e.evaluation_id) FILTER (WHERE e.correct IS TRUE)::integer AS correct_count
FROM reckoner.runs r
LEFT JOIN reckoner.tasks t
  ON t.tenant_id = r.tenant_id AND t.run_id = r.run_id
LEFT JOIN reckoner.evaluations e
  ON e.tenant_id = t.tenant_id AND e.run_id = t.run_id AND e.task_id = t.task_id
GROUP BY r.tenant_id, r.run_id, r.purpose, r.config_id, r.bundle_id, r.status, r.created_at;

CREATE VIEW reckoner.api_results AS
SELECT
  t.tenant_id,
  t.run_id,
  t.task_id,
  t.transaction_id,
  t.status,
  d.decision_id,
  d.outcome,
  d.requested_model,
  d.reported_model,
  d.created_at
FROM reckoner.tasks t
LEFT JOIN reckoner.decisions d
  ON d.tenant_id = t.tenant_id AND d.run_id = t.run_id AND d.task_id = t.task_id;

REVOKE ALL ON reckoner.api_run_summaries, reckoner.api_results FROM PUBLIC;
GRANT SELECT ON reckoner.api_run_summaries, reckoner.api_results TO reckoner_api;
