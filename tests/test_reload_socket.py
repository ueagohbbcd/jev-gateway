"""Exercise reload and admission through actual HTTP sockets, without inference."""
import json
import math
import os
import queue
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from test_reload import _port, _readline_with_timeout, _wait_for_health


def test_reload_during_real_http_request(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    models = []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            models.append(body["model"])
            if body["model"] == "old-model":
                entered.set()
                release.wait(8)
            tokens = [
                {"token": "Yes", "logprob": math.log(0.9)},
                {"token": "No", "logprob": math.log(0.1)},
            ]
            data = json.dumps({
                "choices": [{"finish_reason": "length", "logprobs": {
                    "content": [{**tokens[0], "top_logprobs": tokens}]
                }}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    port = _port()
    for name, temperature in [("old", 1), ("new", 2)]:
        (tmp_path / f"{name}.toml").write_text(
            f'[upstream]\nbase_url="http://127.0.0.1:{upstream.server_port}"\n'
            f'model="{name}-model"\napi_key_env="RELOAD_SOCKET_KEY"\n'
            f'[adapter]\ntemperature={temperature}\n'
            f'[server]\nport={port}\nmax_concurrent_requests=1\n',
            encoding="utf-8",
        )

    def ask():
        request = Request(
            f"http://127.0.0.1:{port}/v1/systemone",
            data=json.dumps({"model": "jev-latest", "state": "state", "questions": {
                "q": {"type": "noul", "instructions": "ask"}
            }}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, response.headers["x-jev-config-id"], json.load(response)

    process = None
    old_result = queue.Queue()

    def ask_old():
        try:
            old_result.put(ask())
        except Exception as exc:
            old_result.put(exc)

    try:
        with (tmp_path / "server.log").open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "jev_gateway", "serve", "--config", "old.toml"],
                cwd=tmp_path,
                env={**os.environ, "RELOAD_SOCKET_KEY": "offline-only"},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
            )
            _wait_for_health(port, process)
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                old_id = json.load(response)["configID"]
            threading.Thread(target=ask_old, daemon=True).start()
            assert entered.wait(2)
            process.stdin.write(b"reload new.toml\n")
            process.stdin.flush()
            reply = json.loads(_readline_with_timeout(process.stdout))
            assert reply["ok"] is True
            new_id = reply["config_id"]
            assert new_id != old_id
            overflow = ask()
            assert overflow[:2] == (529, new_id)
            release.set()
            old = old_result.get(timeout=4)
            assert not isinstance(old, Exception), repr(old)
            assert old[:2] == (200, old_id)
            assert old[2]["answers"]["q"]["noul"] == pytest.approx(0.9)
            new = ask()
            assert new[:2] == (200, new_id)
            assert new[2]["answers"]["q"]["noul"] == pytest.approx(0.75)
            assert models == ["old-model", "new-model"]
    finally:
        release.set()
        if process is not None:
            process.terminate()
            process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()
        upstream.shutdown()
        upstream.server_close()
