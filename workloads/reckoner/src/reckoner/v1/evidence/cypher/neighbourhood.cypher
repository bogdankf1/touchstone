MATCH (c:Card)-[:USED]->(t:Transaction)-[:PAID]->(m:Merchant)
WHERE t.occurred_at < datetime($query_time)
  AND t.occurred_at >= datetime($query_time)-duration({days:30})
  AND ((c.tenant_id=$tenant_id AND c.identity=$card_id)
    OR (m.tenant_id=$tenant_id AND m.identity=$merchant_id)
    OR ($merchant_identity IS NOT NULL AND m.shared_identity=$merchant_identity))
RETURN t.document AS document,m.shared_identity AS merchant_identity
ORDER BY t.tenant_id,t.transaction_id
