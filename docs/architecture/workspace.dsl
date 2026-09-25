workspace "Touchstone" "Measurement platform with the simulated Reckoner workload" {
    model {
        reviewer = person "Reviewer" "Reviews escalated simulated cases in a browser."

        reckoner = softwareSystem "Reckoner" "Simulated payments-risk workload." {
            api = container "Health API" "Exposes only GET /health/live." "Python, FastAPI" {
                tags "Implemented"
            }
            profiler = container "Source profiler" "Streams the simulated CCTD files and writes an aggregate inventory." "Python" {
                tags "Implemented"
            }
            pipeline = container "Decision pipeline" "Will score, route, and prepare escalated cases." "Python, LangGraph" {
                tags "Planned"
            }
            postgres = container "Operational store" "Will hold tenant-scoped transactions, decisions, cases, configuration, and reviews." "PostgreSQL, pgvector" {
                tags "Database" "Planned"
            }
            neo4j = container "Graph store" "Will hold tenant-scoped entity relationships and graph features." "Neo4j, GDS" {
                tags "Database" "Planned"
            }
        }

        touchstone = softwareSystem "Touchstone" "Workflow-independent measurement platform." {
            browser = container "Browser client" "Will render the reviewer console, metrics dashboard, and administration." "Web browser" {
                tags "Planned"
            }
            web = container "Web application" "Will provide the reviewer console, metrics dashboard, and administration." "Next.js" {
                tags "Planned"
            }
            collector = container "Telemetry collector" "Will ingest workload telemetry only through OTLP." "OpenTelemetry Collector" {
                tags "Planned"
            }
            clickhouse = container "Raw event store" "Will retain append-only telemetry." "ClickHouse" {
                tags "Database" "Planned"
            }
            warehouse = container "Governed metrics" "Will hold modelled metrics for the demonstrated dashboard." "Snowflake; DuckDB in local/CI" {
                tags "Database" "Planned"
            }
        }

        anthropic = softwareSystem "Anthropic" "Planned model provider through LiteLLM." "External" {
            tags "Planned"
        }
        jev = softwareSystem "Jev" "Planned scorer; integration waits for access." "External" {
            tags "Planned"
        }

        reviewer -> browser "Will use"
        browser -> web "Will request application pages and APIs" "HTTPS"
        web -> pipeline "Will submit reviews and configuration changes" "HTTPS/JSON"
        pipeline -> postgres "Will persist tenant-scoped operational records"
        pipeline -> neo4j "Will query time-correct graph evidence" "Cypher"
        pipeline -> anthropic "Will request model decisions or case notes through LiteLLM" "HTTPS"
        pipeline -> jev "Will request calibrated scores" "HTTPS"
        pipeline -> collector "Will emit generic workflow telemetry" "OTLP"
        collector -> clickhouse "Will append raw telemetry"
        clickhouse -> warehouse "Will load scheduled batches"
        web -> warehouse "Will query governed metrics through a server-side API"
    }

    views {
        systemContext touchstone "SystemContext" "Planned product context" {
            include reviewer
            include touchstone
            include reckoner
            include anthropic
            include jev
            autoLayout lr
        }

        container reckoner "ReckonerContainers" "Current and planned Reckoner containers" {
            include reviewer
            include browser
            include web
            include *
            include anthropic
            include jev
            autoLayout lr
        }

        container touchstone "TouchstoneContainers" "Planned Touchstone containers and workload boundary" {
            include reviewer
            include pipeline
            include *
            autoLayout lr
        }

        container reckoner "Foundation" "Only components delivered in Phase 0" {
            include api
            include profiler
            autoLayout lr
        }

        styles {
            element "Implemented" {
                background #116466
                color #ffffff
            }
            element "Planned" {
                background #e5e7eb
                color #374151
                border dashed
            }
            element "Database" {
                shape cylinder
            }
        }
    }
}
