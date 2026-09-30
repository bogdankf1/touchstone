CREATE CONSTRAINT evidence_entity_key IF NOT EXISTS FOR (n:Entity) REQUIRE n.key IS UNIQUE;
CREATE CONSTRAINT evidence_transaction IF NOT EXISTS FOR (n:Transaction) REQUIRE (n.tenant_id,n.transaction_id) IS UNIQUE;
CREATE CONSTRAINT evidence_coverage IF NOT EXISTS FOR (n:EvidenceCoverage) REQUIRE n.tenant_id IS UNIQUE;
CREATE CONSTRAINT evidence_projection IF NOT EXISTS FOR (n:ProjectionReceipt) REQUIRE (n.tenant_id,n.projection_id) IS UNIQUE;
CREATE INDEX evidence_shared_identity IF NOT EXISTS FOR (n:Merchant) ON (n.shared_identity);
CREATE CONSTRAINT evidence_gds_metric IF NOT EXISTS FOR (n:GDSMetric) REQUIRE (n.tenant_id,n.entity_key,n.projection_id) IS UNIQUE;
