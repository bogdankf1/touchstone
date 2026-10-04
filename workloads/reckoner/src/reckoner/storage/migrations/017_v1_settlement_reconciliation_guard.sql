-- Reconciling a call that was recorded uncertain, or that has no persisted provider
-- response, is an owner action backed by recorded provider evidence. Checked at
-- commit so a scorer may still settle a responded call alongside its response.
CREATE FUNCTION reckoner.v1_settlement_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status = 'settled' AND (
       EXISTS (SELECT 1 FROM reckoner.v1_settlements u
               WHERE u.call_id = NEW.call_id AND u.status = 'uncertain')
       OR NOT EXISTS (SELECT 1 FROM reckoner.v1_provider_responses r
                      WHERE r.call_id = NEW.call_id)) THEN
    IF pg_has_role(current_user, 'reckoner_runner', 'member')
       AND NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
      RAISE EXCEPTION 'reconciling an uncertain or unanswered call requires the owner'
        USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM reckoner.v1_settlement_evidence e
                   WHERE e.call_id = NEW.call_id) THEN
      RAISE EXCEPTION 'reconciliation requires recorded provider evidence'
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER settlement_reconciliation AFTER INSERT ON reckoner.v1_settlements
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reckoner.v1_settlement_guard();
