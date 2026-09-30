"""Register the operational routes on the existing local-demo FastAPI app."""

from contextlib import contextmanager

import psycopg
from fastapi import HTTPException

from reckoner.v1.storage.repository import V1Repository


def register(application, api_dsn):
    from reckoner.v1.api import cases, configurations, reviews

    @contextmanager
    def repository():
        if not api_dsn:
            raise HTTPException(503, "database unavailable")
        try:
            with V1Repository(api_dsn) as repo:
                yield repo
        except (psycopg.errors.SerializationFailure, psycopg.errors.UniqueViolation):
            raise HTTPException(409, "conflicting or stale write") from None
        except (LookupError, psycopg.errors.NoDataFound):
            raise HTTPException(404, "record not found") from None
        except (ValueError, psycopg.errors.CheckViolation):
            raise HTTPException(422, "invalid request") from None
        except psycopg.Error:
            raise HTTPException(503, "database unavailable") from None

    cases.register(application, repository)
    reviews.register(application, repository)
    configurations.register(application, repository)
