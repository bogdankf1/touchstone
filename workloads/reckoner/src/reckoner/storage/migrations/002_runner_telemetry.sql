CREATE TABLE reckoner.price_tables (
  tenant_id text NOT NULL,
  price_table_version text NOT NULL,
  document jsonb NOT NULL,
  PRIMARY KEY (tenant_id, price_table_version)
);

ALTER TABLE reckoner.runs
  ADD COLUMN execution_mode text NOT NULL DEFAULT 'paid'
    CHECK (execution_mode IN ('paid', 'test')),
  ADD COLUMN preflight_code_revision text,
  ADD COLUMN preflight_at timestamptz;

ALTER TABLE reckoner.tasks
  ADD COLUMN request_document jsonb,
  ADD COLUMN request_sha256 text,
  ADD COLUMN input_token_estimate integer CHECK (input_token_estimate >= 0),
  ADD COLUMN reservation_input_tokens integer CHECK (reservation_input_tokens >= 0),
  ADD COLUMN reservation_cost numeric CHECK (reservation_cost >= 0),
  ADD COLUMN trace_id text,
  ADD COLUMN span_id text,
  ADD COLUMN event_id text,
  ADD CONSTRAINT task_preflight_complete CHECK (
    (request_document IS NULL AND request_sha256 IS NULL
      AND input_token_estimate IS NULL AND reservation_input_tokens IS NULL
      AND reservation_cost IS NULL AND trace_id IS NULL AND span_id IS NULL
      AND event_id IS NULL)
    OR
    (request_document IS NOT NULL AND length(request_sha256) = 64
      AND input_token_estimate IS NOT NULL AND reservation_input_tokens IS NOT NULL
      AND reservation_cost IS NOT NULL AND length(trace_id) = 32
      AND length(span_id) = 16 AND event_id IS NOT NULL)
  );

ALTER TABLE reckoner.attempts
  ADD COLUMN error_category text,
  ADD COLUMN started_at timestamptz,
  ADD COLUMN ended_at timestamptz,
  ADD COLUMN duration_ms numeric CHECK (duration_ms >= 0),
  ADD COLUMN trace_id text,
  ADD COLUMN span_id text;

ALTER TABLE reckoner.telemetry_outbox
  ADD COLUMN producer text NOT NULL DEFAULT 'runner'
    CHECK (producer IN ('runner', 'evaluator'));

REVOKE ALL ON reckoner.telemetry_outbox FROM reckoner_runner, reckoner_evaluator;

CREATE VIEW reckoner.runner_telemetry_outbox AS
SELECT tenant_id, event_id, run_id, task_id, status, payload, created_at, producer
FROM reckoner.telemetry_outbox
WHERE producer = 'runner'
WITH CASCADED CHECK OPTION;

CREATE VIEW reckoner.evaluator_telemetry_outbox AS
SELECT tenant_id, event_id, run_id, task_id, status, payload, created_at, producer
FROM reckoner.telemetry_outbox
WHERE producer = 'evaluator'
WITH CASCADED CHECK OPTION;

GRANT SELECT, INSERT, UPDATE ON reckoner.runner_telemetry_outbox TO reckoner_runner;
GRANT SELECT, INSERT, UPDATE ON reckoner.evaluator_telemetry_outbox TO reckoner_evaluator;

GRANT SELECT, INSERT ON reckoner.price_tables TO reckoner_runner;
GRANT INSERT ON reckoner.threshold_configs TO reckoner_runner;
GRANT INSERT ON reckoner.run_configs, reckoner.runs, reckoner.tasks TO reckoner_runner;
GRANT UPDATE ON reckoner.runs TO reckoner_runner;
GRANT SELECT ON reckoner.price_tables TO reckoner_evaluator;
