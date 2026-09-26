"""An explicit Dagster asset with a deployment-enabled conservative schedule."""

from dagster import Definitions, MaterializeResult, ScheduleDefinition, asset, define_asset_job

from touchstone_platform.refresh import refresh
from touchstone_platform.settings import Settings


@asset
def warehouse_refresh() -> MaterializeResult:
    result = refresh(Settings.from_env())
    return MaterializeResult(
        metadata={
            "generation": result.generation,
            "cutoff": result.cutoff,
            "accepted_measurements": result.accepted_measurements,
            "rejected_count": result.rejected_count,
        }
    )


def make_definitions(settings: Settings | None = None) -> Definitions:
    settings = settings or Settings.from_env()
    job = define_asset_job("touchstone_refresh")
    schedules = []
    if settings.schedule_enabled:
        schedules.append(
            ScheduleDefinition(
                name="touchstone_refresh_15m",
                job=job,
                cron_schedule=f"*/{settings.refresh_interval_minutes} * * * *",
            )
        )
    return Definitions(assets=[warehouse_refresh], jobs=[job], schedules=schedules)


defs = make_definitions()
