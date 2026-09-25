from fastapi import FastAPI

app = FastAPI(title="Reckoner", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}
