"""Exercise the installed CLI over a real loopback socket, without inference."""
import json
import os
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen


def test_standalone_server_starts_outside_repository(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = tmp_path / "server.toml"
    config.write_text(f'[server]\nport = {port}\n', encoding="utf-8")
    env = {**os.environ, "DEEPSEEK_API_KEY": "offline-placeholder-never-sent"}
    process = subprocess.Popen(
        [sys.executable, "-m", "jev_gateway", "serve", "--config", str(config)],
        cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{port}/health", timeout=.5) as response:
                    health = json.load(response)
                break
            except URLError:
                if process.poll() is not None:
                    raise AssertionError(process.stderr.read().decode(errors="replace"))
                time.sleep(.05)
        else:
            raise AssertionError("gateway startup timed out")
        assert health["scope"] == "configuration"
        with urlopen(f"http://127.0.0.1:{port}/openapi.json", timeout=2) as response:
            schema = json.load(response)
        assert "/v1/systemone" in schema["paths"]
        assert schema["components"]["schemas"]["ScoreAnswer"]["properties"]["score"]["type"] == "number"
        with urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=2) as response:
            assert "jev-latest" in [row["name"] for row in json.load(response)["models"]]
    finally:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
