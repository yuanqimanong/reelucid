import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx2
import pytest

from reelucid import cli
from reelucid.component_manager.agent import Agent, admin_token, local_url
from reelucid.component_manager.install import Manager
from reelucid.domain import JsonObject, ReelucidError


def test_component_cli_without_core_data_or_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REELUCID_DATA_DIR", str(tmp_path / "core"))
    monkeypatch.setattr(
        sys, "argv", ["reelucid", "components", "list", "--state-dir", str(tmp_path / "components")]
    )
    cli.main()
    assert {c["id"]: c["state"] for c in json.loads(capsys.readouterr().out)["components"]} == {
        "dtk": "not_installed",
        "xhs": "not_installed",
    }
    assert not (tmp_path / "core").exists() and not (tmp_path / "components").exists()


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://evil.localhost:8000",
        "http://token@127.0.0.1:8000",
        "http://127.0.0.1/path",
        "http://127.0.0.1:8000?next=http://example.org",
    ],
)
def test_agent_only_connects_to_literal_loopback(url: str) -> None:
    with pytest.raises(ReelucidError, match="回环"):
        local_url(url)
    assert local_url("http://localhost:8000/") == "http://127.0.0.1:8000"
    assert local_url("https://[::1]:8443") == "https://[::1]:8443"


def test_token_file_and_local_project_scope(tmp_path: Path) -> None:
    key = tmp_path / "token"
    key.write_text("admin-secret\n")
    assert admin_token(key, "core") == "admin-secret"
    with patch(
        "reelucid.component_manager.agent.docker", side_effect=["abcde", "from-container"]
    ) as action:
        assert admin_token(None, "core") == "from-container"
    assert "label=com.docker.compose.project=core" in action.call_args_list[0].args
    assert action.call_args_list[1].args == ("exec", "abcde", "cat", "/data/secrets/admin_token")


def test_private_record_is_not_overwritten_on_conflict(tmp_path: Path) -> None:
    manager = Manager(tmp_path, "core")
    manager.root.mkdir(parents=True)
    private = manager.root / "dtk.json"
    private.write_text('{"owner":"broken","secrets":{"SECRET":"do-not-echo"}}')
    with pytest.raises(ReelucidError) as error:
        manager.install("dtk")
    assert error.value.code == "COMPONENT_STATE_ERROR"
    assert "do-not-echo" not in str(error.value.public())
    assert "do-not-echo" in private.read_text()


def test_existing_remote_alias_is_never_adopted(tmp_path: Path) -> None:
    manager = Manager(tmp_path, "core")
    # Existing network, empty managed project, and an unrelated service claiming the alias.
    with patch(
        "reelucid.component_manager.install.docker",
        side_effect=["core", "", "aabbccdd", '["xhs-resolver"]'],
    ) as run:
        with pytest.raises(ReelucidError) as error:
            manager.install("xhs")
    assert error.value.code == "COMPONENT_CONFLICT"
    assert not (manager.root / "xhs.json").exists()
    assert all("up" not in call.args for call in run.call_args_list)


def test_agent_ignores_redirects_and_does_not_forward_bearer(tmp_path: Path) -> None:
    seen: list[str] = []

    def redirect(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        return httpx2.Response(307, headers={"Location": "https://example.org/remote"})

    with (
        httpx2.Client(
            base_url="http://127.0.0.1",
            headers={"Authorization": "Bearer local"},
            follow_redirects=False,
            transport=httpx2.MockTransport(redirect),
        ) as client,
        ThreadPoolExecutor(max_workers=1) as executor,
        pytest.raises(ReelucidError),
    ):
        Agent(Manager(tmp_path), client, executor).poll()
    assert seen == ["http://127.0.0.1/api/v1/admin/components/agent/poll"]


def test_agent_keeps_polling_and_retries_completion_without_rerunning(tmp_path: Path) -> None:
    enter, release = threading.Event(), threading.Event()
    messages: list[JsonObject] = []

    def install(_component: str) -> JsonObject:
        enter.set()
        assert release.wait(5)
        return {"private_value": "not-for-api"}

    def respond(request: httpx2.Request) -> httpx2.Response:
        messages.append(json.loads(request.content))
        work = {"id": "1" * 32, "component": "xhs"}
        index = len(messages)
        if index == 3:
            return httpx2.Response(503)
        return httpx2.Response(200, json={"request": work if index < 5 else None})

    with (
        patch.object(Manager, "install", side_effect=install) as action,
        httpx2.Client(
            base_url="http://127.0.0.1", transport=httpx2.MockTransport(respond)
        ) as client,
        ThreadPoolExecutor(max_workers=1) as worker,
    ):
        agent = Agent(Manager(tmp_path), client, worker)
        agent.poll()
        assert enter.wait(5)
        agent.poll()
        assert "completion" not in messages[1]
        release.set()
        assert agent.future is not None
        agent.future.result(5)
        with pytest.raises(ReelucidError) as failed:
            agent.poll()
        assert failed.value.retryable
        agent.poll()
        agent.poll()
        action.assert_called_once_with("xhs")
        assert agent.active is None
    assert all(
        r["completion"] == {"id": "1" * 32, "status": "succeeded", "error_code": None}
        for r in messages[2:]
    )
    assert "not-for-api" not in str(messages)


@pytest.mark.parametrize(
    "work",
    [{"id": "2" * 32, "component": "shell"}, {"id": "3" * 32, "component": "dtk", "image": "bad"}],
)
def test_agent_rejects_unlisted_work(tmp_path: Path, work: JsonObject) -> None:
    with (
        httpx2.Client(
            base_url="http://127.0.0.1",
            transport=httpx2.MockTransport(lambda _: httpx2.Response(200, json={"request": work})),
        ) as client,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        agent = Agent(Manager(tmp_path), client, executor)
        with pytest.raises(ReelucidError) as caught:
            agent.poll()
        assert caught.value.code == "COMPONENT_AGENT_POLL_FAILED"
        assert agent.active is None
