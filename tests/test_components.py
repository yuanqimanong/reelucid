from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reelucid.api import create_app
from reelucid.component_jobs import (
    HEARTBEAT_TTL,
    AgentPoll,
    Completion,
    ComponentJobs,
    ReportedStates,
)
from reelucid.domain import ReelucidError
from reelucid.settings import AppConfig

ROUTE = "/api/v1/admin/components"
POLL = ROUTE + "/agent/poll"
AGENT = "a" * 32
REPORT = AgentPoll(agent_id=AGENT, components=ReportedStates(dtk="not_installed", xhs="stopped"))


def test_component_routes_auth_and_fixed_inputs(tmp_path: Path) -> None:
    config = AppConfig(tmp_path)
    with TestClient(create_app(config)) as client:
        token = config.admin_token_file.read_text().strip()
        assert client.get(ROUTE).status_code == 401
        assert client.post(POLL, json=REPORT.model_dump()).status_code == 401
        assert client.post("/api/v1/auth/session", json={"token": token}).is_success
        assert client.get(ROUTE).json()["agent_connected"] is False
        origin = {"Origin": "http://testserver"}
        bearer = {"Authorization": f"Bearer {token}"}
        assert client.post(POLL, headers=origin, json=REPORT.model_dump()).status_code == 401
        assert (
            client.post(
                POLL,
                headers={**origin, "Authorization": "Bearer invalid"},
                json=REPORT.model_dump(),
            ).status_code
            == 401
        )
        install = ROUTE + "/dtk/install"
        assert client.post(install).status_code == 403
        assert client.post(install, headers=origin).status_code == 409
        assert client.post(POLL, headers=bearer, json=REPORT.model_dump()).json() == {
            "request": None
        }
        assert client.get(ROUTE).json()["components"] == [
            {"id": "dtk", "state": "not_installed", "install_state": "idle", "error_code": None},
            {"id": "xhs", "state": "stopped", "install_state": "idle", "error_code": None},
        ]
        assert client.post(ROUTE + "/other/install", headers=origin).status_code == 422
        assert client.post(install, headers=origin, json={"image": "other"}).status_code == 422
        assert client.post(install + "?command=other", headers=origin).status_code == 422
        for bad in (
            {"command": "other"},
            {"agent_id": "bad"},
            {"components": {"dtk": "healthy"}},
            {"components": {"dtk": "healthy", "xhs": "stopped", "other": "stopped"}},
            {"components": {"dtk": "bogus", "xhs": "stopped"}},
        ):
            assert (
                client.post(POLL, headers=bearer, json={**REPORT.model_dump(), **bad}).status_code
                == 422
            )
        response = client.post(install, headers=origin, json={})
        assert response.status_code == 202
        assert client.post(install, headers=origin).json() == response.json()
        assert client.post(POLL, headers=bearer, json=REPORT.model_dump()).json() == {
            "request": response.json()
        }


def test_install_recovers_acknowledgements_and_claims_after_restart(tmp_path: Path) -> None:
    journal = tmp_path / "component-jobs.json"
    jobs = ComponentJobs(journal)
    jobs.poll(REPORT)
    with ThreadPoolExecutor(6) as pool:
        submissions = [pool.submit(jobs.enqueue, "dtk") for _ in range(12)]
        duplicates = [submission.result() for submission in submissions]
    dtk = duplicates[0]
    assert all(request == dtk for request in duplicates)
    xhs = jobs.enqueue("xhs")
    assert jobs.poll(REPORT).request == dtk
    restarted = ComponentJobs(journal)
    assert not restarted.inventory().agent_connected
    with pytest.raises(ReelucidError):
        restarted.enqueue("dtk")
    assert restarted.poll(REPORT).request == dtk
    assert restarted.enqueue("dtk") == dtk
    succeeded = Completion(id=dtk.id, status="succeeded")
    poll = AgentPoll(agent_id=AGENT, components=REPORT.components, completion=succeeded)
    assert restarted.poll(poll).request == xhs
    next_dtk = restarted.enqueue("dtk")
    assert next_dtk != dtk
    # The agent missed its response. A newer request must not erase the preceding receipt.
    assert restarted.poll(poll).request == xhs
    failed = Completion(id=xhs.id, status="failed", error_code="INSTALL_FAILED")
    assert (
        restarted.poll(
            AgentPoll(agent_id=AGENT, components=REPORT.components, completion=failed)
        ).request
        == next_dtk
    )
    xhs_info = restarted.inventory().components[1]
    assert (xhs_info.install_state, xhs_info.error_code) == ("failed", "INSTALL_FAILED")


def test_live_agent_lease_expires_and_new_owner_cannot_run_old_install(tmp_path: Path) -> None:
    now = 100.0
    jobs = ComponentJobs(tmp_path / "component-jobs.json", lambda: now)
    jobs.poll(REPORT)
    request = jobs.enqueue("dtk")
    finished = Completion(id=request.id, status="succeeded")
    with pytest.raises(ReelucidError):  # A queued request has not yet been claimed.
        jobs.poll(AgentPoll(agent_id=AGENT, components=REPORT.components, completion=finished))
    assert jobs.poll(REPORT).request == request
    second_agent = AgentPoll(agent_id="b" * 32, components=REPORT.components)
    with pytest.raises(ReelucidError):
        jobs.poll(second_agent)
    now += HEARTBEAT_TTL
    offline = jobs.inventory()
    assert not offline.agent_connected and all(
        item.state == "unknown" for item in offline.components
    )
    with pytest.raises(ReelucidError):
        jobs.enqueue("xhs")
    with pytest.raises(ReelucidError):  # Wrong owner cannot complete the old agent's claim.
        jobs.poll(second_agent.model_copy(update={"completion": finished}))
    assert jobs.poll(second_agent).request is None
    old = jobs.inventory().components[0]
    assert (old.install_state, old.error_code) == ("failed", "AGENT_CHANGED")
    with pytest.raises(ReelucidError):
        jobs.poll(AgentPoll(agent_id=AGENT, components=REPORT.components, completion=finished))
    retried = jobs.enqueue("dtk")
    assert retried.id != request.id and jobs.poll(second_agent).request == retried
