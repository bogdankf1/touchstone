"""Checksum-verified PostgreSQL schema migrations."""

import hashlib
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

MIGRATIONS = Path(__file__).with_name("migrations")


def provision_roles(owner_dsn: str, environment: dict[str, str]) -> None:
    """Provision local login roles from explicit DSNs, without logging passwords."""
    owner = conninfo_to_dict(owner_dsn)
    roles = []
    for kind in ("runner", "evaluator", "api"):
        dsn = environment.get(f"RECKONER_{kind.upper()}_DSN")
        if dsn is None:
            continue
        target = conninfo_to_dict(dsn)
        if (
            not target.get("user")
            or not target.get("password")
            or target["user"] == owner.get("user")
            or not target["user"].startswith("reckoner_")
            or target["user"] in {"reckoner_runner", "reckoner_evaluator", "reckoner_api"}
            or any(target.get(key) != owner.get(key) for key in ("host", "port", "dbname"))
        ):
            raise ValueError("invalid role environment")
        roles.append((kind, target["user"], target["password"]))
    if len({role for _, role, _ in roles}) != len(roles):
        raise ValueError("role logins must be distinct")
    with psycopg.connect(owner_dsn) as connection:
        for kind, role, password in roles:
            existing = connection.execute(
                "SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls "
                "FROM pg_roles WHERE rolname = %s",
                (role,),
            ).fetchone()
            memberships = connection.execute(
                "SELECT parent.rolname FROM pg_auth_members membership "
                "JOIN pg_roles parent ON parent.oid = membership.roleid "
                "JOIN pg_roles child ON child.oid = membership.member WHERE child.rolname = %s",
                (role,),
            ).fetchall()
            if existing is not None and (
                any(existing) or {row[0] for row in memberships} - {f"reckoner_{kind}"}
            ):
                raise ValueError("role login has unexpected privileges")
            verb = "ALTER" if existing else "CREATE"
            connection.execute(
                sql.SQL(verb + " ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
            connection.execute(
                sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier(f"reckoner_{kind}"), sql.Identifier(role)
                )
            )


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
