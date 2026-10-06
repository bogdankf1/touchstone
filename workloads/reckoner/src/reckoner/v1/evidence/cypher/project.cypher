CALL gds.graph.project.cypher($name,
 'MATCH (n:Entity) WHERE n.tenant_id IN $tenants AND n.observed_at < datetime($cutoff) RETURN id(n) AS id',
 'MATCH (a:Account)-[r:OWNS]->(c:Card)
  WHERE a.tenant_id IN $tenants AND r.occurred_at < datetime($cutoff)
  RETURN id(a) AS source,id(c) AS target,1.0 AS weight
  UNION ALL
  MATCH (c:Card)<-[r:OWNS]-(a:Account)
  WHERE a.tenant_id IN $tenants AND r.occurred_at < datetime($cutoff)
  RETURN id(c) AS source,id(a) AS target,1.0 AS weight
  UNION ALL
  MATCH (c:Card)-[:USED]->(t:Transaction)-[:PAID]->(m:Merchant)
  WHERE c.tenant_id IN $tenants AND t.occurred_at < datetime($cutoff)
    AND t.occurred_at >= datetime($cutoff)-duration({days:30})
  RETURN id(c) AS source,id(m) AS target,toFloat(count(t)) AS weight
  UNION ALL
  MATCH (c:Card)-[:USED]->(t:Transaction)-[:PAID]->(m:Merchant)
  WHERE c.tenant_id IN $tenants AND t.occurred_at < datetime($cutoff)
    AND t.occurred_at >= datetime($cutoff)-duration({days:30})
  RETURN id(m) AS source,id(c) AS target,toFloat(count(t)) AS weight
  UNION ALL
  MATCH (a:Merchant)-[r:SHARED_IDENTITY]->(b:Merchant)
  WHERE a.tenant_id IN $tenants AND b.tenant_id IN $tenants
    AND r.occurred_at < datetime($cutoff)
  RETURN id(a) AS source,id(b) AS target,1.0 AS weight
  UNION ALL
  MATCH (a:Merchant)-[r:SHARED_IDENTITY]->(b:Merchant)
  WHERE a.tenant_id IN $tenants AND b.tenant_id IN $tenants
    AND r.occurred_at < datetime($cutoff)
  RETURN id(b) AS source,id(a) AS target,1.0 AS weight',
 {parameters:{tenants:$tenants,cutoff:$cutoff},readConcurrency:1})
YIELD nodeCount,relationshipCount
RETURN nodeCount,relationshipCount
