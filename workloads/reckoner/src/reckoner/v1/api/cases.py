from typing import Annotated

from fastapi import Query

from reckoner.v1.api.schemas import CaseDetail, CasePage, CaseStatus, OpaqueID
from reckoner.v1.storage.cases import get_case, list_cases


def register(app, repository):
    @app.get("/v1/cases", response_model=CasePage)
    def queue(
        tenant_id: OpaqueID,
        run_id: OpaqueID | None = None,
        status: CaseStatus = "open",
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(max_length=4096)] = None,
    ) -> dict:
        with repository() as repo:
            return list_cases(
                repo, tenant_id=tenant_id, run_id=run_id, status=status, limit=limit, cursor=cursor
            )

    @app.get("/v1/case", response_model=CaseDetail)
    def detail(tenant_id: OpaqueID, case_id: OpaqueID) -> dict:
        with repository() as repo:
            return get_case(repo, tenant_id=tenant_id, case_id=case_id)
