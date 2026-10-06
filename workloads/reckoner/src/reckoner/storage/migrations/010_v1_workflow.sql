-- Durable simulated workflow policy, references and LangGraph checkpoint records.
CREATE TABLE reckoner.v1_workflow_runs (
  tenant_id text NOT NULL,
  run_id text NOT NULL,
  config_id text NOT NULL,
  document jsonb NOT NULL,
  PRIMARY KEY (tenant_id, run_id),
  FOREIGN KEY (tenant_id, run_id, config_id)
    REFERENCES reckoner.v1_runs (tenant_id, run_id, config_id)
);
CREATE TABLE reckoner.v1_workflow_tasks (
  tenant_id text NOT NULL,
  run_id text NOT NULL,
  task_id text NOT NULL,
  transaction_id text NOT NULL,
  evidence_id text NOT NULL,
  document jsonb NOT NULL,
  PRIMARY KEY (tenant_id, run_id, task_id),
  FOREIGN KEY (tenant_id, run_id) REFERENCES reckoner.v1_workflow_runs,
  FOREIGN KEY (tenant_id, run_id, task_id, transaction_id)
    REFERENCES reckoner.v1_tasks (tenant_id, run_id, task_id, transaction_id),
  FOREIGN KEY (tenant_id, evidence_id, transaction_id)
    REFERENCES reckoner.v1_evidence (tenant_id, evidence_id, transaction_id)
);
CREATE TABLE reckoner.v1_checkpoints (
  tenant_id text NOT NULL,
  run_id text NOT NULL,
  task_id text NOT NULL,
  checkpoint_ns text NOT NULL,
  checkpoint_id text NOT NULL,
  parent_id text,
  encoding text NOT NULL,
  payload bytea NOT NULL,
  metadata jsonb NOT NULL,
  PRIMARY KEY (tenant_id, run_id, task_id, checkpoint_ns, checkpoint_id),
  FOREIGN KEY (tenant_id, run_id, task_id) REFERENCES reckoner.v1_workflow_tasks
);
CREATE TABLE reckoner.v1_checkpoint_writes (
  tenant_id text NOT NULL,
  run_id text NOT NULL,
  task_id text NOT NULL,
  checkpoint_ns text NOT NULL,
  checkpoint_id text NOT NULL,
  node_task_id text NOT NULL,
  write_index integer NOT NULL,
  channel text NOT NULL,
  encoding text NOT NULL,
  payload bytea NOT NULL,
  PRIMARY KEY (tenant_id, run_id, task_id, checkpoint_ns, checkpoint_id,
               node_task_id, write_index),
  FOREIGN KEY (tenant_id, run_id, task_id, checkpoint_ns, checkpoint_id)
    REFERENCES reckoner.v1_checkpoints
);
CREATE FUNCTION reckoner.reject_workflow_binding_change() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
  RAISE EXCEPTION 'immutable workflow binding' USING ERRCODE='23514';
END $$;
CREATE TRIGGER immutable_workflow_run BEFORE UPDATE OR DELETE
ON reckoner.v1_workflow_runs FOR EACH ROW EXECUTE FUNCTION reckoner.reject_workflow_binding_change();
CREATE TRIGGER immutable_workflow_task BEFORE UPDATE OR DELETE
ON reckoner.v1_workflow_tasks FOR EACH ROW EXECUTE FUNCTION reckoner.reject_workflow_binding_change();
REVOKE ALL ON reckoner.v1_workflow_runs, reckoner.v1_workflow_tasks,
  reckoner.v1_checkpoints, reckoner.v1_checkpoint_writes FROM PUBLIC;
GRANT SELECT, INSERT ON reckoner.v1_workflow_runs, reckoner.v1_workflow_tasks,
  reckoner.v1_checkpoints, reckoner.v1_checkpoint_writes TO reckoner_runner;
GRANT UPDATE (channel, encoding, payload) ON reckoner.v1_checkpoint_writes TO reckoner_runner;
GRANT SELECT ON reckoner.v1_workflow_runs, reckoner.v1_workflow_tasks,
  reckoner.v1_checkpoints, reckoner.v1_checkpoint_writes TO reckoner_evaluator;
