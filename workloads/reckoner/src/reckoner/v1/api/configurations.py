from reckoner.v1.api.schemas import (
    ActivationRequest,
    ActivationResponse,
    ConfigurationEntry,
    ConfigurationHistory,
    ConfigurationRequest,
    OpaqueID,
    PreviewResponse,
)
from reckoner.v1.storage.configurations import (
    activate_configuration,
    create_configuration,
    list_configurations,
    preview_configuration,
)


def register(app, repository):
    @app.get("/v1/configurations", response_model=ConfigurationHistory)
    def history(tenant_id: OpaqueID) -> dict:
        with repository() as repo:
            return list_configurations(repo, tenant_id=tenant_id)

    @app.post("/v1/configurations", response_model=ConfigurationEntry)
    def create(request: ConfigurationRequest) -> dict:
        with repository() as repo:
            return create_configuration(repo, request.model_dump())

    @app.post("/v1/configurations/activate", response_model=ActivationResponse)
    def activate(request: ActivationRequest) -> dict:
        with repository() as repo:
            return activate_configuration(repo, **request.model_dump())

    @app.get("/v1/configurations/preview", response_model=PreviewResponse)
    def preview(tenant_id: OpaqueID, run_id: OpaqueID, config_id: OpaqueID) -> dict:
        with repository() as repo:
            return preview_configuration(
                repo, tenant_id=tenant_id, run_id=run_id, config_id=config_id
            )
