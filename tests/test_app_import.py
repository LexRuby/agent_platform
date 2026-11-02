"""Production module must construct before the ASGI lifespan starts."""


def test_production_app_imports_without_lifespan_services():
    import agent_service_app

    assert agent_service_app.app is not None
    assert agent_service_app._official_app.state.workspace_manager is not None
