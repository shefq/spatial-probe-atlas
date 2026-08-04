from fastapi import APIRouter

from . import calibration_registration, capture_contract, contract, contract_more, diagnostics_contract, hardware_contract, integrity_contract, legacy_import, live_websockets, mapping_dispatch_contract, native_contract, not_found, projects_mapping, sessions_review, system, ui_contract, websocket_security, websockets, workflow_contract


websocket_security.install(websockets)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(legacy_import.router)
api_router.include_router(capture_contract.router)
api_router.include_router(workflow_contract.router)
api_router.include_router(ui_contract.router)
api_router.include_router(diagnostics_contract.router)
api_router.include_router(mapping_dispatch_contract.router)
api_router.include_router(integrity_contract.router)
api_router.include_router(native_contract.router)
api_router.include_router(hardware_contract.router)
api_router.include_router(contract_more.router)
api_router.include_router(contract.router)
api_router.include_router(projects_mapping.router)
api_router.include_router(calibration_registration.router)
api_router.include_router(sessions_review.router)
api_router.include_router(system.router)
api_router.include_router(not_found.router)

root_router = APIRouter()
root_router.include_router(system.health_router)
root_router.include_router(live_websockets.router, prefix="/ws/v1")
root_router.include_router(websockets.router, prefix="/ws/v1")
