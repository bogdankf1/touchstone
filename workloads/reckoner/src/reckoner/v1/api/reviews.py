from reckoner.v1.api.schemas import ReviewRequest, ReviewResponse
from reckoner.v1.storage.reviews import submit_review


def register(app, repository):
    @app.post("/v1/reviews", response_model=ReviewResponse)
    def review(request: ReviewRequest) -> dict:
        values = request.model_dump()
        key = values.pop("idempotency_key")
        version = values.pop("expected_version")
        with repository() as repo:
            return submit_review(repo, values, idempotency_key=key, expected_version=version)
