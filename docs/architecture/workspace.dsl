workspace "Touchstone" "Measurement platform with the simulated Reckoner workload" {
    model {
        reviewer = person "Reviewer" "Reviews escalated simulated cases in a browser."

        reckoner = softwareSystem "Reckoner" "Simulated payments-risk workload." {
            api = container "Operational API" "Readiness and sanitized tenant-scoped run/results reads under the API role." "Python, FastAPI" {
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
            pipeline = container "Decision pipeline" "Will score, route, and prepare escalated cases." "Python, LangGraph" {
                tags "Planned"
            }
            postgres = container "Operational store" "Durable tenant-scoped tasks, attempts, oracle, reservations, decisions and export outbox." "PostgreSQL 17" {
                tags "Database" "Implemented"
            }
            neo4j = container "Graph store" "Will hold tenant-scoped entity relationships and graph features." "Neo4j, GDS" {
                tags "Database" "Planned"
            }
        }

        touchstone = softwareSystem "Touchstone" "Workflow-independent measurement platform." {
            browser = container "Browser client" "Renders the metrics dashboard; reviewer and administration surfaces remain planned." "Web browser" {
                tags "Implemented"
            }
            web = container "Web application" "Serves the measured and fabricated workflow dashboard." "Next.js" {
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

        anthropic = softwareSystem "Anthropic" "Provider client implemented; paid experiment pending review and preflight." "External" {
            tags "Implemented"
        }
        jev = softwareSystem "Jev" "Planned scorer; integration waits for access." "External" {
            tags "Planned"
        }

        api -> postgres "Reads sanitized tenant views" "SQL; API role"
        batch -> postgres "Reserves spend and persists attempts" "SQL; runner role"
        evaluator -> postgres "Reads oracle and publishes evaluations" "SQL; evaluator role"
        batch -> anthropic "Explicit paid mode only; fake smoke has no HTTP" "HTTPS"
        batch -> exports "Exports runner outbox" "OTLP protobuf"
        evaluator -> exports "Exports evaluator outbox" "OTLP protobuf"
        exports -> collector "Replays durable measured evidence" "OTLP/HTTP"
        synthetic -> collector "Emits fabricated independent workflow" "OTLP/HTTP"
        reviewer -> browser "Will use"
        browser -> web "Requests dashboard pages" "HTTP"
        web -> pipeline "Will submit reviews and configuration changes" "HTTPS/JSON"
        pipeline -> postgres "Will persist tenant-scoped operational records"
        pipeline -> neo4j "Will query time-correct graph evidence" "Cypher"
        pipeline -> anthropic "Will request model decisions or case notes through LiteLLM" "HTTPS"
        pipeline -> jev "Will request calibrated scores" "HTTPS"
        pipeline -> collector "Will emit generic workflow telemetry" "OTLP"
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
