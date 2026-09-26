"""Uvicorn module entry point for the read-only Reckoner API."""

import os

from reckoner.api import create_app

app = create_app(os.environ.get("RECKONER_API_DSN"))

__all__ = ["app", "create_app"]
