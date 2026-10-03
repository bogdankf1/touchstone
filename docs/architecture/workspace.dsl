workspace "Touchstone" "Measurement platform with the simulated Reckoner workload" {
    model {
        reviewer = person "Reviewer" "Reviews escalated simulated cases in a browser."

        reckoner = softwareSystem "Reckoner" "Simulated payments-risk workload." {
            api = container "Operational API" "Ready only after every packaged migration; tenant-scoped v0 results and v1 case, review and configuration routes under the API role." "Python, FastAPI" {
                tags "Implemented"
            }
            profiler = container "Source profiler" "Streams the simulated CCTD files and writes an aggregate inventory." "Python" {
                tags "Implemented"
            }
            batch = container "Baseline runner" "Sequential explicit CLI; runner role cannot read oracle; paid execution requires gates." "Python, LiteLLM" {
                tags "Implemented"
            }
            evaluator = container "Evaluator and reports" "Evaluator-role oracle joins; fixed denominators; JSON and Markdown evidence." "Python" {
                tags "Implemented"
            }
            exports = container "OTLP export" "Separate durable runner/evaluator protobuf manifests; explicit HTTP replay." "OpenTelemetry 1.37" {
                tags "Implemented"
            }
            pipeline = container "Decision workflow" "Persisted LangGraph intake, evidence, score, route and decision; optional note after escalation; degraded escalation without dispatch when evidence is incomplete." "Python, LangGraph" {
                tags "Implemented"
            }
            evidence = container "Evidence preparation" "Owner import, temporal Postgres/pgvector features, graph import and declared GDS projections; persists bounded evidence documents." "Python" {
                tags "Implemented"
            }
            outbox = container "V1 OTLP outbox" "Transactional Postgres outbox exporting generic measurement-v1 events; undelivered events stay pending." "Python, OpenTelemetry" {
                tags "Implemented"
            }
            postgres = container "Operational store" "Durable tenant-scoped tasks, attempts, oracle, reservations, evidence, decisions, cases, reviews, checkpoints and export outboxes." "PostgreSQL 17, pgvector" {
                tags "Database" "Implemented"
            }
            neo4j = container "Graph store" "Tenant-scoped temporal entity graph and Louvain/PageRank projections; runs in the preparation profile only." "Neo4j Community, GDS" {
                tags "Database" "Implemented"
            }
        }

        touchstone = softwareSystem "Touchstone" "Workflow-independent measurement platform." {
            browser = container "Browser client" "Renders the metrics dashboard, reviewer console and settings." "Web browser" {
                tags "Implemented"
            }
            web = container "Web application" "Serves the dashboard, reviewer console and settings; proxies Reckoner operations server-side." "Next.js" {
                tags "Implemented"
            }
            collector = container "Telemetry collector" "Ingests workload telemetry only through OTLP with a persistent exporter queue." "OpenTelemetry Collector" {
                tags "Implemented"
            }
            clickhouse = container "Raw event store" "Retains append-only telemetry and server receipt times." "ClickHouse" {
                tags "Database" "Implemented"
            }
            refresh = container "Warehouse refresh" "Builds dbt and MetricFlow-checked immutable local generations; Dagster schedule is disabled by default." "Python, dbt, Dagster" {
                tags "Implemented"
            }
            readApi = container "Read API" "Pins one published generation for tenant-scoped dashboard reads." "Python, FastAPI" {
                tags "Implemented"
            }
            warehouse = container "Governed metrics" "Published DuckDB local/CI preview; live Snowflake integration awaits owner access." "DuckDB local preview; Snowflake planned" {
                tags "Database" "Implemented"
            }
        }

        synthetic = softwareSystem "Synthetic workflow" "Independent fabricated trace source proving the platform boundary." "Python" {
            tags "Implemented"
        }

        anthropic = softwareSystem "Anthropic" "Baseline, note and judge clients implemented; each paid run needs its own approved protocol." "External" {
            tags "Implemented"
        }
        jev = softwareSystem "Jev" "Binary Choice scorer adapter implemented; paid scoring needs an approved protocol." "External" {
            tags "Implemented"
        }

        api -> postgres "Reads sanitized tenant views" "SQL; API role"
        batch -> postgres "Reserves spend and persists attempts" "SQL; runner role"
        evaluator -> postgres "Reads oracle and publishes evaluations" "SQL; evaluator role"
        batch -> anthropic "Explicit paid mode only; fake smoke has no HTTP" "HTTPS"
        batch -> exports "Exports runner outbox" "OTLP protobuf"
        evaluator -> exports "Exports evaluator outbox" "OTLP protobuf"
        exports -> collector "Replays durable measured evidence" "OTLP/HTTP"
        synthetic -> collector "Emits fabricated independent workflow" "OTLP/HTTP"
        reviewer -> browser "Reviews escalated cases and settings"
        browser -> web "Requests dashboard, console and settings pages" "HTTP"
        web -> api "Proxies case, review and configuration operations" "HTTP/JSON"
        evidence -> postgres "Imports history and persists evidence" "SQL; owner and runner roles"
        evidence -> neo4j "Imports the graph and builds GDS projections" "Cypher"
        pipeline -> postgres "Persists checkpoints, decisions and cases" "SQL; runner role"
        pipeline -> jev "Scores only under an approved protocol" "HTTPS"
        pipeline -> anthropic "Writes notes only under an approved protocol" "HTTPS"
        outbox -> postgres "Reads pending generic events" "SQL; runner role"
        outbox -> collector "Exports generic measurement events" "OTLP/HTTP"
        collector -> clickhouse "Appends raw spans and events" "ClickHouse exporter"
        clickhouse -> refresh "Reads raw receipts through a fixed cutoff" "ClickHouse HTTP"
        refresh -> warehouse "Publishes tested immutable generations" "dbt, MetricFlow"
        web -> readApi "Queries tenant-scoped results" "HTTP/JSON"
        readApi -> warehouse "Reads a pinned published generation" "DuckDB"
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

        container touchstone "TouchstoneContainers" "Current local Touchstone containers and workload boundary" {
            include reviewer
            include pipeline
            include synthetic
            include *
            autoLayout lr
        }

        container touchstone "LocalMeasurement" "Phase 2 measured and fabricated workflows; DuckDB local preview" {
            include exports
            include synthetic
            include browser
            include web
            include collector
            include clickhouse
            include refresh
            include readApi
            include warehouse
            autoLayout lr
        }

        container reckoner "ReckonerV1" "Phase 3 v1 runtime on simulated data; paid runs remain separately gated" {
            include reviewer
            include browser
            include web
            include api
            include evidence
            include pipeline
            include outbox
            include postgres
            include neo4j
            include collector
            include jev
            include anthropic
            autoLayout lr
        }

        container reckoner "Foundation" "Only components delivered in Phase 0" {
            include api
            include profiler
            autoLayout lr
        }

        container reckoner "Baseline" "Phase 1 measured baseline on simulated data" {
            include api
            include profiler
            include batch
            include evaluator
            include exports
            include postgres
            include anthropic
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
