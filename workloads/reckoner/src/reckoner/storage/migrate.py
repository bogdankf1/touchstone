"""Checksum-verified PostgreSQL schema migrations."""

import hashlib
from pathlib import Path

import psycopg

MIGRATIONS = Path(__file__).with_name("migrations")


def migrate(owner_dsn: str) -> None:
    """Apply numbered migrations, rejecting edits to applied migration bytes."""
    migrations = sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql"))
    if not migrations:
        raise ValueError("no database migrations found")
    with psycopg.connect(owner_dsn) as connection:
        with connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (732019001,))
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS public.reckoner_schema_migrations (
                  version text PRIMARY KEY,
                  checksum text NOT NULL,
                  applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            connection.execute("REVOKE ALL ON public.reckoner_schema_migrations FROM PUBLIC")
            for path in migrations:
                payload = path.read_bytes()
                checksum = hashlib.sha256(payload).hexdigest()
                applied = connection.execute(
                    "SELECT checksum FROM public.reckoner_schema_migrations WHERE version = %s",
                    (path.name,),
                ).fetchone()
                if applied:
                    if applied[0] != checksum:
                        raise ValueError(f"migration checksum mismatch: {path.name}")
                    continue
                connection.execute(payload.decode("utf-8"))
                connection.execute(
                    "INSERT INTO public.reckoner_schema_migrations "
                    "(version, checksum) VALUES (%s, %s)",
                    (path.name, checksum),
                )
