from uuid import uuid4

from test_v2_credential_console import add
from test_v2_execution_controller import case, controller, database, routes, setup
from test_v2_execution_routes import request

from services.v2_reentry_prepare_entry import bound_scope

__all__ = ["case", "controller", "database", "routes", "setup"]


def test_preparer_follows_actual_binding_not_web_selection(controller):
    ctrl, store, console, state = controller
    a = add(console)["registry_id"]
    b = add(console)["registry_id"]
    with console.connect() as c:
        assert bound_scope(c, console.tenant) is None
    request(store, console, a)
    ctrl.step()
    with console.connect() as c:
        first = bound_scope(c, console.tenant)
        assert first is not None and first.environment == "SANDBOX"
        assert bound_scope(c, str(uuid4())) is None
    request(store, console, b, expected_epoch=1)
    state["source"] = ["EXISTING_POSITION_REQUIRES_RECOVERY"]
    ctrl.step()
    with console.connect() as c:
        assert bound_scope(c, console.tenant) is None
    state["source"] = []
    ctrl.step()
    with console.connect() as c:
        second = bound_scope(c, console.tenant)
        assert second is not None and second.account_id != first.account_id
