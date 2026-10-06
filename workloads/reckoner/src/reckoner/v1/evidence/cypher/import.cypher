UNWIND $rows AS row
MERGE (a:Entity:Account {key:row.account_key})
ON CREATE SET a.tenant_id=row.tenant_id,a.identity=row.account_id,a.observed_at=datetime(row.occurred_at)
SET a.observed_at=CASE WHEN a.observed_at>datetime(row.occurred_at) THEN datetime(row.occurred_at) ELSE a.observed_at END
MERGE (c:Entity:Card {key:row.card_key})
ON CREATE SET c.tenant_id=row.tenant_id,c.identity=row.card_id,c.observed_at=datetime(row.occurred_at)
SET c.observed_at=CASE WHEN c.observed_at>datetime(row.occurred_at) THEN datetime(row.occurred_at) ELSE c.observed_at END
MERGE (m:Entity:Merchant {key:row.merchant_key})
ON CREATE SET m.tenant_id=row.tenant_id,m.identity=row.merchant_id,m.observed_at=datetime(row.occurred_at)
SET m.observed_at=CASE WHEN m.observed_at>datetime(row.occurred_at) THEN datetime(row.occurred_at) ELSE m.observed_at END,
    m.shared_identity=coalesce(row.merchant_identity,m.shared_identity)
MERGE (t:Transaction {tenant_id:row.tenant_id,transaction_id:row.transaction_id})
ON CREATE SET t.document=row.document,t.occurred_at=datetime(row.occurred_at),t.resolution=row.resolution,
    t.resolved_at=datetime(row.resolved_at)
MERGE (a)-[owns:OWNS]->(c)
ON CREATE SET owns.tenant_id=row.tenant_id,owns.occurred_at=datetime(row.occurred_at)
SET owns.occurred_at=CASE WHEN owns.occurred_at>datetime(row.occurred_at) THEN datetime(row.occurred_at) ELSE owns.occurred_at END
MERGE (c)-[use:USED]->(t)
ON CREATE SET use.tenant_id=row.tenant_id,use.occurred_at=datetime(row.occurred_at)
MERGE (t)-[pay:PAID]->(m)
ON CREATE SET pay.tenant_id=row.tenant_id,pay.occurred_at=datetime(row.occurred_at)
