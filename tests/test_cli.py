import json
from pathlib import Path

from jev_gateway.cli import main

ROOT = Path(__file__).resolve().parents[1]


def test_preview_is_offline_and_expands_exact_call_count(monkeypatch, capsys):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert main(["preview", "--config", str(ROOT / "examples/round-robin.toml"),
                 "--request", str(ROOT / "examples/request.json")]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["request_count"] == 9
    assert result["requests"][0]["mapping"] == {"Yes": "true", "No": "false"}
    assert result["requests"][1]["mapping"] == {"A": "billing", "B": "technical"}


def test_invalid_config_has_no_sensitive_input_in_error(tmp_path, capsys):
    path = tmp_path / "invalid.toml"
    path.write_text('[upstream]\ntimeout = "PRIVATE-INPUT"\n', encoding="utf-8")
    assert main(["check", "--config", str(path)]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert "PRIVATE-INPUT" not in output.err
    assert json.loads(output.err)["error"] == "validation_failed"


def test_callsign_example_is_independent_of_round_robin(capsys):
    assert main(["preview", "--config", str(ROOT / "examples/callsigns.toml"),
                 "--request", str(ROOT / "examples/request.json")]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["request_count"] == 3
    assert result["requests"][0]["mapping"] == {"Yes": "true", "No": "false"}
    assert result["requests"][1]["mapping"] == {"alpha": "billing", "fox": "technical"}
