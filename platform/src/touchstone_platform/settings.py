"""Local Touchstone refresh settings; cloud warehouse activation is separate."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    warehouse_dir: Path = Path("artifacts/touchstone-warehouse")
    clickhouse_host: str = "localhost"
    clickhouse_port: int = 8123
    clickhouse_username: str = "default"
    clickhouse_password: str = ""
    clickhouse_database: str = "otel"
    refresh_interval_minutes: int = 15
    schedule_enabled: bool = False
    dbt_threads: int = 2
    duckdb_memory_limit: str = "2GB"
    extraction_page_size: int = 500

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            warehouse_dir=Path(
                os.getenv("TOUCHSTONE_WAREHOUSE_DIR", "artifacts/touchstone-warehouse")
            ),
            clickhouse_host=os.getenv("TOUCHSTONE_CLICKHOUSE_HOST", "localhost"),
            clickhouse_port=int(os.getenv("TOUCHSTONE_CLICKHOUSE_PORT", "8123")),
            clickhouse_username=os.getenv("TOUCHSTONE_CLICKHOUSE_USERNAME", "default"),
            clickhouse_password=os.getenv("TOUCHSTONE_CLICKHOUSE_PASSWORD", ""),
            clickhouse_database=os.getenv("TOUCHSTONE_CLICKHOUSE_DATABASE", "otel"),
            refresh_interval_minutes=int(os.getenv("TOUCHSTONE_REFRESH_MINUTES", "15")),
            schedule_enabled=os.getenv("TOUCHSTONE_SCHEDULE_ENABLED", "false").lower() == "true",
            dbt_threads=int(os.getenv("TOUCHSTONE_DBT_THREADS", "2")),
            duckdb_memory_limit=os.getenv("TOUCHSTONE_DUCKDB_MEMORY_LIMIT", "2GB"),
            extraction_page_size=int(os.getenv("TOUCHSTONE_EXTRACTION_PAGE_SIZE", "500")),
        )
