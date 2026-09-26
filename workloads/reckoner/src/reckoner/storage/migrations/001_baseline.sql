DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'reckoner_runner') THEN
    CREATE ROLE reckoner_runner NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'reckoner_evaluator') THEN
    CREATE ROLE reckoner_evaluator NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'reckoner_api') THEN
    CREATE ROLE reckoner_api NOLOGIN;
  END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS reckoner;
CREATE SCHEMA IF NOT EXISTS oracle;
REVOKE ALL ON SCHEMA reckoner FROM PUBLIC;
REVOKE ALL ON SCHEMA oracle FROM PUBLIC;

CREATE TABLE reckoner.transactions (
  tenant_id text NOT NULL,
  transaction_id text NOT NULL,
  document jsonb NOT NULL,
  PRIMARY KEY (tenant_id, transaction_id)
);

CREATE TABLE reckoner.cohorts (
  tenant_id text NOT NULL,
  cohort_id text NOT NULL,
  purpose text NOT NULL CHECK (purpose IN ('pilot', 'baseline')),
  bundle_id text NOT NULL,
  document jsonb NOT NULL,
  PRIMARY KEY (tenant_id, cohort_id),
  UNIQUE (tenant_id, bundle_id, purpose)
);

CREATE TABLE reckoner.cohort_members (
  tenant_id text NOT NULL,
  cohort_id text NOT NULL,
  transaction_id text NOT NULL,
  PRIMARY KEY (tenant_id, cohort_id, transaction_id),
  FOREIGN KEY (tenant_id, cohort_id)
    REFERENCES reckoner.cohorts (tenant_id, cohort_id),
  FOREIGN KEY (tenant_id, transaction_id)
    REFERENCES reckoner.transactions (tenant_id, transaction_id)
);

CREATE TABLE reckoner.threshold_configs (
  tenant_id text NOT NULL,
  config_id text NOT NULL,
  document jsonb NOT NULL,
  PRIMARY KEY (tenant_id, config_id)
);

CREATE TABLE reckoner.run_configs (
  tenant_id text NOT NULL,
  config_id text NOT NULL,
  threshold_config_id text NOT NULL,
  document jsonb NOT NULL,
  PRIMARY KEY (tenant_id, config_id),
  FOREIGN KEY (tenant_id, threshold_config_id)
    REFERENCES reckoner.threshold_configs (tenant_id, config_id)
);

CREATE TABLE reckoner.runs (
  tenant_id text NOT NULL,
  run_id text NOT NULL,
  purpose text NOT NULL CHECK (purpose IN ('pilot', 'baseline')),
  config_id text NOT NULL,
  bundle_id text NOT NULL,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'running', 'complete', 'incomplete', 'blocked')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, run_id),
  FOREIGN KEY (tenant_id, config_id)
    REFERENCES reckoner.run_configs (tenant_id, config_id),
  FOREIGN KEY (tenant_id, bundle_id, purpose)
    REFERENCES reckoner.cohorts (tenant_id, bundle_id, purpose)
);

CREATE TABLE reckoner.tasks (
  tenant_id text NOT NULL,
  run_id text NOT NULL,
  task_id text NOT NULL,
  transaction_id text NOT NULL,
  status text NOT NULL CHECK (status IN ('pending','dispatched','completed','failed','uncertain')),
  PRIMARY KEY (tenant_id, run_id, task_id),
  UNIQUE (tenant_id, run_id, transaction_id),
  FOREIGN KEY (tenant_id, run_id)
    REFERENCES reckoner.runs (tenant_id, run_id),
  FOREIGN KEY (tenant_id, transaction_id)
    REFERENCES reckoner.transactions (tenant_id, transaction_id)
);

CREATE TABLE reckoner.attempts (
  tenant_id text NOT NULL,
  run_id text NOT NULL,
  task_id text NOT NULL,
  call_id text NOT NULL,
  status text NOT NULL CHECK (status IN ('dispatched', 'responded', 'failed', 'uncertain')),
  requested_model text,
  reported_model text,
  request_document jsonb,
  request_sha256 text,
  response_document jsonb,
  response_sha256 text,
  usage jsonb,
  maximum_cost numeric NOT NULL CHECK (maximum_cost >= 0),
  actual_cost numeric CHECK (actual_cost >= 0),
  dispatched_at timestamptz NOT NULL DEFAULT now(),
  settled_at timestamptz,
  PRIMARY KEY (tenant_id, run_id, task_id, call_id),
  UNIQUE (call_id),
  FOREIGN KEY (tenant_id, run_id, task_id)
    REFERENCES reckoner.tasks (tenant_id, run_id, task_id)
);

CREATE TABLE reckoner.decisions (
  tenant_id text NOT NULL,
  run_id text NOT NULL,
  task_id text NOT NULL,
  decision_id text NOT NULL,
  call_id text NOT NULL,
  outcome text NOT NULL CHECK (outcome IN ('auto-approve', 'auto-decline', 'escalate')),
  cohort_id text NOT NULL,
  config_id text NOT NULL,
  prompt_version text NOT NULL,
  threshold_config_id text NOT NULL,
  requested_model text NOT NULL,
  reported_model text,
  score numeric,
  effective_low_threshold numeric,
  effective_high_threshold numeric,
  scorer text,
  graph_reference jsonb,
  document jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, run_id, task_id, decision_id),
  UNIQUE (tenant_id, run_id, task_id),
  FOREIGN KEY (tenant_id, run_id, task_id, call_id)
    REFERENCES reckoner.attempts (tenant_id, run_id, task_id, call_id),
  FOREIGN KEY (tenant_id, config_id)
    REFERENCES reckoner.run_configs (tenant_id, config_id),
  FOREIGN KEY (tenant_id, threshold_config_id)
    REFERENCES reckoner.threshold_configs (tenant_id, config_id),
  FOREIGN KEY (tenant_id, cohort_id)
    REFERENCES reckoner.cohorts (tenant_id, cohort_id)
);

CREATE TABLE oracle.oracle_labels (
  tenant_id text NOT NULL,
  transaction_id text NOT NULL,
  oracle_version text NOT NULL,
  label text NOT NULL CHECK (label IN ('legitimate', 'fraud')),
  document jsonb NOT NULL,
  PRIMARY KEY (tenant_id, transaction_id, oracle_version),
  FOREIGN KEY (tenant_id, transaction_id)
    REFERENCES reckoner.transactions (tenant_id, transaction_id)
);

CREATE TABLE reckoner.evaluations (
  tenant_id text NOT NULL,
  run_id text NOT NULL,
  task_id text NOT NULL,
  evaluation_id text NOT NULL,
  correct boolean,
  document jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, run_id, task_id, evaluation_id),
  FOREIGN KEY (tenant_id, run_id, task_id)
    REFERENCES reckoner.tasks (tenant_id, run_id, task_id)
);

CREATE TABLE reckoner.telemetry_outbox (
  tenant_id text NOT NULL,
  event_id text NOT NULL,
  run_id text NOT NULL,
  task_id text,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'exported', 'failed')),
  payload bytea NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, event_id),
  FOREIGN KEY (tenant_id, run_id)
    REFERENCES reckoner.runs (tenant_id, run_id),
  FOREIGN KEY (tenant_id, run_id, task_id)
    REFERENCES reckoner.tasks (tenant_id, run_id, task_id)
);

CREATE TABLE reckoner.budget_entries (
  tenant_id text NOT NULL,
  run_id text NOT NULL,
  task_id text NOT NULL,
  call_id text NOT NULL,
  purpose text NOT NULL CHECK (purpose IN ('pilot', 'baseline')),
  maximum_cost numeric NOT NULL CHECK (maximum_cost >= 0),
  actual_cost numeric CHECK (actual_cost >= 0),
  usage jsonb,
  status text NOT NULL CHECK (status IN ('reserved', 'settled', 'uncertain')),
  created_at timestamptz NOT NULL DEFAULT now(),
  settled_at timestamptz,
  PRIMARY KEY (tenant_id, run_id, task_id, call_id),
  UNIQUE (call_id),
  FOREIGN KEY (tenant_id, run_id, task_id, call_id)
    REFERENCES reckoner.attempts (tenant_id, run_id, task_id, call_id)
);

CREATE VIEW reckoner.api_runs AS
SELECT tenant_id, run_id, purpose, config_id, bundle_id, status, created_at
FROM reckoner.runs;

CREATE VIEW reckoner.api_tasks AS
SELECT tenant_id, run_id, task_id, transaction_id, status
FROM reckoner.tasks;

CREATE VIEW reckoner.api_decisions AS
SELECT tenant_id, run_id, task_id, decision_id, outcome, created_at
FROM reckoner.decisions;

REVOKE ALL ON ALL TABLES IN SCHEMA reckoner FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA oracle FROM PUBLIC;

GRANT USAGE ON SCHEMA reckoner TO reckoner_runner, reckoner_evaluator, reckoner_api;
GRANT USAGE ON SCHEMA oracle TO reckoner_evaluator;

GRANT SELECT ON reckoner.transactions, reckoner.cohorts, reckoner.cohort_members,
  reckoner.threshold_configs, reckoner.run_configs, reckoner.runs, reckoner.tasks,
  reckoner.attempts, reckoner.decisions, reckoner.telemetry_outbox,
  reckoner.budget_entries
  TO reckoner_runner;
GRANT UPDATE ON reckoner.tasks TO reckoner_runner;
GRANT INSERT, UPDATE ON reckoner.attempts, reckoner.telemetry_outbox,
  reckoner.budget_entries TO reckoner_runner;
GRANT INSERT ON reckoner.decisions TO reckoner_runner;

GRANT SELECT ON reckoner.transactions, reckoner.cohorts, reckoner.cohort_members,
  reckoner.threshold_configs, reckoner.run_configs, reckoner.runs, reckoner.tasks,
  reckoner.attempts, reckoner.decisions, reckoner.budget_entries,
  oracle.oracle_labels TO reckoner_evaluator;
GRANT INSERT ON reckoner.evaluations TO reckoner_evaluator;
GRANT INSERT, UPDATE ON reckoner.telemetry_outbox TO reckoner_evaluator;

GRANT SELECT ON reckoner.api_runs, reckoner.api_tasks, reckoner.api_decisions TO reckoner_api;
