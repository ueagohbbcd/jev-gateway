from __future__ import annotations

import asyncio
import json
import math
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import httpx
from fastapi.testclient import TestClient

from jev_gateway.api import create_app
from jev_gateway.config import Adapter, Server, Settings, Upstream


def _port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _config(path: Path, port: int, *, temperature: float = 1.0, extra: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"[adapter]\ntemperature = {temperature}\n[server]\nport = {port}\n{extra}",
        encoding="utf-8",
    )


def _wait_for_health(port: int, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5):
                return
        except URLError:
            if process.poll() is not None:
                assert process.stderr is not None
                raise AssertionError(process.stderr.read().decode(errors="replace"))
            time.sleep(0.05)
    raise AssertionError("gateway startup timed out")


def _readline_with_timeout(stream, timeout: float = 3) -> bytes:
    result: queue.Queue[bytes] = queue.Queue()
    threading.Thread(target=lambda: result.put(stream.readline()), daemon=True).start()
    return result.get(timeout=timeout)


def test_subprocess_stdin_status_reload_failures_and_eof(tmp_path: Path) -> None:
    port = _port()
    initial = tmp_path / "initial.toml"
    changed = tmp_path / "配置 with spaces" / "next config.toml"
    invalid = tmp_path / "invalid-template.toml"
    server_change = tmp_path / "server-change.toml"
    _config(initial, port)
    _config(changed, port, temperature=2.0)
    _config(
        invalid,
        port,
        extra='[prompt]\nsystem = ""\nuser = "{{state}}"\n',
    )
    _config(server_change, port + 1)
    env = {**os.environ, "DEEPSEEK_API_KEY": "offline-placeholder-never-sent"}
    process = subprocess.Popen(
        [sys.executable, "-m", "jev_gateway", "serve", "--config", str(initial)],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    try:
        _wait_for_health(port, process)

        process.stdin.write(b"status\n")
        process.stdin.flush()
        first = json.loads(_readline_with_timeout(process.stdout))
        assert first == {
            "command": "status",
            "ok": True,
            "config_path": str(initial.resolve()),
            "config_id": first["config_id"],
            "mode": "single",
            "temperature": 1.0,
        }

        relative_changed = changed.relative_to(tmp_path)
        process.stdin.write(f'reload "{relative_changed}"\n'.encode())
        process.stdin.flush()
        reloaded = json.loads(_readline_with_timeout(process.stdout))
        assert reloaded["ok"] is True
        assert reloaded["config_path"] == str(changed.resolve())
        assert reloaded["temperature"] == 2.0
        assert reloaded["config_id"] != first["config_id"]

        _config(changed, port, temperature=2.5)
        process.stdin.write(b"reload\n")
        process.stdin.flush()
        active_reload = json.loads(_readline_with_timeout(process.stdout))
        assert active_reload["ok"] is True
        assert active_reload["config_path"] == str(changed.resolve())
        assert active_reload["temperature"] == 2.5
        assert active_reload["config_id"] != reloaded["config_id"]
        reloaded = active_reload

        for command, code in [
            (f'reload "{tmp_path / "missing.toml"}"', "config_not_found"),
            (f'reload "{invalid}"', "validation_failed"),
            (f'reload "{server_change}"', "restart_required"),
        ]:
            process.stdin.write((command + "\n").encode())
            process.stdin.flush()
            failed = json.loads(_readline_with_timeout(process.stdout))
            assert failed["ok"] is False
            assert failed["error"]["code"] == code
            assert failed["config_path"] == str(changed.resolve())
            assert failed["config_id"] == reloaded["config_id"]
            assert str(invalid) not in json.dumps(failed)

        process.stdin.write(b"status\n")
        process.stdin.flush()
        unchanged = json.loads(_readline_with_timeout(process.stdout))
        assert unchanged["config_id"] == reloaded["config_id"]
        assert unchanged["config_path"] == str(changed.resolve())

        process.stdin.close()
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
            assert json.load(response)["status"] == "ready"
    finally:
        process.terminate()
        process.wait(timeout=5)
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def test_subprocess_graceful_shutdown_does_not_wait_for_open_stdin(
    tmp_path: Path,
) -> None:
    port = _port()
    config = tmp_path / "server.toml"
    _config(config, port)
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        [sys.executable, "-m", "jev_gateway", "serve", "--config", str(config)],
        cwd=tmp_path,
        env={**os.environ, "DEEPSEEK_API_KEY": "offline-placeholder-never-sent"},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
    )
    try:
        _wait_for_health(port, process)
        assert process.stdin is not None and not process.stdin.closed
        process.send_signal(
            signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM
        )
        # Uvicorn re-raises the captured console signal after its lifespan exits;
        # Windows reports CTRL_BREAK as status 3.
        assert process.wait(timeout=5) in ({0, 3} if os.name == "nt" else {0})
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def _completion(tokens: list[str]) -> httpx.Response:
    entries = [
        {"token": token, "logprob": math.log(1 / len(tokens))} for token in tokens
    ]
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "length",
                    "logprobs": {
                        "content": [{**entries[0], "top_logprobs": entries}]
                    },
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )


def test_inflight_request_keeps_snapshot_and_capacity_is_shared(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("UPSTREAM_TEST_KEY", "key")
    entered = threading.Event()
    release = threading.Event()
    seen_models: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_models.append(body["model"])
        if len(seen_models) == 1:
            entered.set()
            assert await asyncio.to_thread(release.wait, 5)
        prompt = body["messages"][-1]["content"]
        marker = "Output exactly one of: "
        labels = prompt.split(marker, 1)[1].split(".", 1)[0].split(", ")
        return _completion(labels)

    initial = Settings(
        upstream=Upstream(
            base_url="https://upstream.invalid",
            model="old-model",
            api_key_env="UPSTREAM_TEST_KEY",
        ),
        adapter=Adapter(temperature=1.0),
        server=Server(max_concurrent_requests=1),
    )
    config_path = tmp_path / "new.toml"
    config_path.write_text(
        """[upstream]
base_url = "https://upstream.invalid"
model = "new-model"
api_key_env = "UPSTREAM_TEST_KEY"
[adapter]
temperature = 2.0
[server]
max_concurrent_requests = 1
""",
        encoding="utf-8",
    )
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(initial, client=upstream, config_path=config_path)
    payload = {
        "model": "jev-latest",
        "state": "state",
        "questions": {"q": {"type": "noul", "instructions": "ask"}},
    }

    async def post_direct() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as direct:
            return await asyncio.wait_for(
                direct.post("/v1/systemone", json=payload), timeout=2
            )

    with TestClient(app) as local:
        holder: dict[str, httpx.Response] = {}
        request_thread = threading.Thread(
            target=lambda: holder.setdefault("old", local.post("/v1/systemone", json=payload)),
            daemon=True,
        )
        try:
            request_thread.start()
            assert entered.wait(2)
            old_id = initial.config_id
            assert local.portal is not None
            snapshot = local.portal.call(app.state.runtime.reload)
            new_id = snapshot.settings.config_id

            overflow = local.portal.call(post_direct)
            assert overflow.status_code == 529
            assert overflow.headers["x-jev-config-id"] == new_id

            release.set()
            request_thread.join(3)
            old = holder["old"]
            assert old.status_code == 200
            assert old.headers["x-jev-config-id"] == old_id

            new = local.post("/v1/systemone", json=payload)
            assert new.status_code == 200
            assert new.headers["x-jev-config-id"] == new_id
            assert {row["name"] for row in local.get("/v1/models").json()["models"]} == {
                "jev-latest",
                "new-model",
            }
        finally:
            release.set()
            request_thread.join(3)

    assert seen_models == ["old-model", "new-model"]
