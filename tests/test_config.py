from pathlib import Path

import pytest
from pydantic import ValidationError

from jev_gateway.config import Settings, load_settings
from jev_gateway.schema import SystemOneRequest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("path", [ROOT / "config.toml", *sorted((ROOT / "examples").glob("*.toml"))])
def test_shipped_configuration(path):
    settings = load_settings(path)
    assert settings.config_id == load_settings(path).config_id
    assert settings.max_answers >= 2


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, "2.8"])
def test_temperature_rejects_invalid_values(value):
    with pytest.raises(ValidationError):
        Settings.model_validate({"adapter": {"temperature": value}})


@pytest.mark.parametrize("value", [["alpha"], ["alpha", "alpha"], ["alpha", " fox"], ["", "fox"]])
def test_callsigns_reject_ambiguous_protocol(value):
    with pytest.raises(ValidationError):
        Settings.model_validate({"adapter": {"callsigns": value}})


def test_defaults_and_configuration_identity():
    settings = Settings()
    assert settings.adapter.callsigns == []
    assert settings.max_answers == 26
    changed = Settings(adapter={"temperature": 2.8})
    assert changed.config_id != settings.config_id
    assert Settings(adapter={"callsigns": ["alpha", "fox"]}).max_answers == 2
    with pytest.raises(ValidationError):
        Settings.model_validate({"adaptor": {}})


@pytest.mark.parametrize("key", ["messages", "temperature", "max_tokens", "logprobs", "top_p", "logit_bias"])
def test_extensions_cannot_override_readout_protocol(key):
    with pytest.raises(ValidationError):
        Settings(upstream={"extra_body": {key: "bad"}})


@pytest.mark.parametrize("user", ["{{state}}", "{{state}}{{instructions}}{{options}}{{output}}{{typo}}",
                                   "{{state}}{{instructions}}{{options}}{{output}}{{oops"])
def test_template_fails_before_any_api_calls(user):
    with pytest.raises(ValidationError):
        Settings(prompt={"system": "", "user": user})


def test_structured_official_criteria_and_null_descriptions():
    payload = SystemOneRequest.model_validate({
        "model": "jev-latest", "state": {"a": ["b"]}, "questions": {
            "n": {"type": "noul", "instructions": ["is?"], "criteria": {"true": {"rule": "yes"}}},
            "c": {"type": "choice", "instructions": {}, "criteria": {"a": None, "b": ["detail"]}},
            "s": {"type": "score", "instructions": "rank", "criteria": [{"score": 0}, "high"]},
        },
    })
    assert payload.questions["n"].criteria.false == "No"
    assert payload.questions["c"].criteria["a"] is None


def test_score_limit_and_numeric_state_rejected():
    data = {"model": "jev-latest", "state": "s", "questions": {
        "q": {"type": "score", "instructions": "q", "criteria": ["x"] * 11}}}
    with pytest.raises(ValidationError):
        SystemOneRequest.model_validate(data)
    data["questions"]["q"]["criteria"] = ["a", "b"]
    data["state"] = 42
    with pytest.raises(ValidationError):
        SystemOneRequest.model_validate(data)
