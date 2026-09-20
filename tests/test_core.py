import math

import pytest

from jev_gateway.core import CoreError, aggregate, build_plan, parse_response


PROMPT = {
    "system": "S={{output}} / {{instructions}}",
    "user": "{{state}}\n{{options}}\n{{output}}",
}


def api_response(top, sampled=None, finish_reason="stop"):
    if sampled is None:
        sampled = top[0]
    return {
        "id": "raw-evidence",
        "choices": [{
            "finish_reason": finish_reason,
            "logprobs": {"content": [{
                "token": sampled[0],
                "logprob": sampled[1],
                "top_logprobs": [
                    {"token": token, "logprob": logprob} for token, logprob in top
                ],
            }]},
        }],
    }


def test_build_plan_preserves_structured_content_and_substitutes_once():
    payload = {
        "model": "unused-by-core",
        "state": {"事实": [1, {"literal": "{{options}}"}]},
        "questions": {
            "never shown": {
                "type": "choice",
                "instructions": {"rule": "pick {{state}}"},
                "criteria": {
                    "first": None,
                    "secret_second": {"nested": [True, "好"]},
                },
            },
            "score-id": {
                "type": "score",
                "instructions": "rate",
                "criteria": ["none", {"level": 1}],
            },
            "boolean-id": {
                "type": "noul",
                "instructions": "true?",
                "criteria": {"true": {"is": True}, "false": ["no"]},
            },
        },
    }
    plan = build_plan(payload, {
        "adapter": {"double_round_robin": False, "callsigns": ["X", "Y"]},
        "prompt": PROMPT,
    })

    assert [branch["index"] for branch in plan] == [0, 0, 0]
    assert plan[0]["mapping"] == {"X": "first", "Y": "secret_second"}
    assert plan[0]["pair"] is None
    system, user = [message["content"] for message in plan[0]["messages"]]
    assert "never shown" not in system + user
    assert 'pick {{state}}' in system
    assert '{{options}}' in user  # evidence is not recursively templated
    assert "X: first" in user  # null choice descriptions mean the semantic key
    assert 'Y: {"nested":[true,"好"]}' in user
    assert "secret_second" not in system + user
    assert plan[1]["mapping"] == {"X": "0", "Y": "1"}
    assert 'Y: {"level":1}' in plan[1]["messages"][1]["content"]
    assert plan[2]["mapping"] == {"Yes": "true", "No": "false"}
    assert 'Yes: {"is":true}' in plan[2]["messages"][1]["content"]
    assert 'No: ["no"]' in plan[2]["messages"][1]["content"]


def test_default_callsigns_and_round_robin_pair_directions_are_exact():
    payload = {
        "model": "m", "state": "evidence",
        "questions": {"q": {
            "type": "choice", "instructions": "pick",
            "criteria": {"red fox": "red", "blue": "blue", "green": "green"},
        }},
    }
    defaulted = build_plan(payload, {
        "adapter": {"double_round_robin": False, "callsigns": []}, "prompt": PROMPT,
    })
    assert defaulted[0]["mapping"] == {"A": "red fox", "B": "blue", "C": "green"}
    assert "Output exactly one of: A, B, C." in defaulted[0]["messages"][0]["content"]
    assert "red fox" not in "".join(message["content"] for message in defaulted[0]["messages"])

    tournament = build_plan(payload, {
        "adapter": {"double_round_robin": True, "callsigns": ["A", "B"]}, "prompt": PROMPT,
    })
    assert len(tournament) == 6
    assert [branch["index"] for branch in tournament] == list(range(6))
    assert [branch["pair"] for branch in tournament] == [
        ["red fox", "blue"], ["blue", "red fox"],
        ["red fox", "green"], ["green", "red fox"],
        ["blue", "green"], ["green", "blue"],
    ]
    assert tournament[0]["mapping"] == {"A": "red fox", "B": "blue"}
    assert tournament[1]["mapping"] == {"A": "blue", "B": "red fox"}


def test_noul_never_uses_callsigns_or_round_robin():
    payload = {"model": "m", "state": "s", "questions": {"q": {
        "type": "noul", "instructions": "is it?",
    }}}
    plan = build_plan(payload, {
        "adapter": {"double_round_robin": True, "callsigns": ["X", "Y"]},
        "prompt": PROMPT,
    })
    assert len(plan) == 1
    assert plan[0]["mapping"] == {"Yes": "true", "No": "false"}
    assert "Yes: Yes\nNo: No" in plan[0]["messages"][1]["content"]


def test_parse_exact_tokens_temperature_missing_and_tie_order():
    parsed = parse_response(
        api_response([("A", math.log(.8)), ("B", math.log(.2))]),
        {"A": "first", "B": "second"},
        temperature=2,
        low_mass_threshold=.5,
    )
    assert parsed["probabilities"]["first"] == pytest.approx(2 / 3)
    assert parsed["probabilities"]["second"] == pytest.approx(1 / 3)
    assert parsed["raw_probabilities"] == pytest.approx({"first": .8, "second": .2})
    assert parsed["observed_top_tokens"][0][0] == "A"

    incomplete = parse_response(
        api_response([("A", math.log(.4)), (" A", math.log(.4)), ("x", math.log(.1))]),
        {"A": "first", "B": "second"},
        temperature=2,
        low_mass_threshold=.95,
    )
    assert incomplete["probabilities"] == {"first": 1.0, "second": 0.0}
    assert incomplete["missing_labels"] == ["second"]
    assert {warning["code"] for warning in incomplete["warnings"]} >= {
        "incomplete_labels", "temperature_with_missing", "low_label_mass",
    }
    tied = aggregate(
        {"type": "choice", "instructions": "", "criteria": {"first": None, "second": None}},
        [{"probabilities": {"first": .5, "second": .5}}],
        False,
    )
    assert tied["choice"] == "first"
    assert tied["confidence"] == pytest.approx(0)


def test_parse_mass_bound_does_not_use_sampled_token_as_top_k_cutoff():
    parsed = parse_response(
        api_response(
            [("A", math.log(.2)), ("outside", math.log(.2))],
            sampled=("B", math.log(.01)),
        ),
        {"A": "a", "B": "b", "C": "c"},
        temperature=1,
        low_mass_threshold=.3,
    )
    assert parsed["observed_label_mass"] == pytest.approx(.21)
    # Seen mass is .41, leaving .59 tail; one missing label is bounded by the
    # .20 top-k cutoff. The sampled .01 token is evidence, but not the cutoff.
    assert parsed["label_mass_upper_bound"] == pytest.approx(.41)
    assert {warning["code"] for warning in parsed["warnings"]} >= {
        "incomplete_labels", "uncertain_low_label_mass",
    }


def test_duplicate_sample_roundoff_does_not_count_probability_twice():
    parsed = parse_response(
        api_response([("A", math.log(.8)), ("B", math.log(.2))],
                     sampled=("A", math.log(.8) + 1e-8)),
        {"A": "a", "B": "b"}, 1, .99,
    )
    assert parsed["observed_label_mass"] == pytest.approx(1)
    assert parsed["raw_probabilities"] == pytest.approx({"a": .8, "b": .2})


def test_parse_rejects_no_targets_conflicts_and_bad_finish_but_uses_valid_subset():
    with pytest.raises(CoreError, match="none of the allowed"):
        parse_response(api_response([("x", -.1)]), {"A": "a"}, 1, .9)
    with pytest.raises(CoreError, match="conflicting"):
        parse_response(api_response([("A", -.1), ("A", -.2)]), {"A": "a"}, 1, .9)
    with pytest.raises(CoreError, match="finish"):
        parse_response(api_response([("A", -.1)], finish_reason="content_filter"), {"A": "a"}, 1, .9)
    malformed = api_response([("A", -.1)])
    malformed["choices"][0]["logprobs"]["content"][0] = []
    with pytest.raises(CoreError, match="first-output-token"):
        parse_response(malformed, {"A": "a"}, 1, .9)
    with pytest.raises(CoreError, match="exceeds one"):
        parse_response(api_response([("A", math.log(.7)), ("B", math.log(.7))]), {"A": "a", "B": "b"}, 1, .9)

    usable = api_response([("A", math.log(.6)), ("bad", -9999), ("worse", float("nan"))])
    parsed = parse_response(usable, {"A": "a", "B": "b"}, 1, .1)
    assert parsed["probabilities"] == {"a": 1.0, "b": 0.0}
    assert "invalid_top_token" in {warning["code"] for warning in parsed["warnings"]}


def test_parse_warns_on_non_protocol_leader_and_softmax_handles_tiny_temperature():
    parsed = parse_response(
        api_response([("outside", math.log(.6)), ("A", math.log(.3)), ("B", math.log(.1))]),
        {"A": "a", "B": "b"},
        1e-300,
        .1,
    )
    assert parsed["probabilities"] == {"a": 1.0, "b": 0.0}
    assert "non_protocol_leader" in {warning["code"] for warning in parsed["warnings"]}


def test_aggregate_official_noul_and_score_shapes():
    noul = aggregate(
        {"type": "noul", "instructions": "", "criteria": {"true": "yes", "false": "no"}},
        [{"probabilities": {"true": .7, "false": .3}}],
        False,
    )
    assert noul == {"type": "noul", "noul": .7}

    score = aggregate(
        {"type": "score", "instructions": "", "criteria": ["low", {"label": "mid"}, "high"]},
        [{"probabilities": {"0": .2, "1": .3, "2": .5}}],
        False,
    )
    assert score["score"] == pytest.approx(1.3)
    assert score["legend"] == {"0": "low", "1": '{"label":"mid"}', "2": "high"}
    assert score["probabilities"] == {"0": .2, "1": .3, "2": .5}
    assert 0 < score["confidence"] < 1


def test_tournament_averages_pair_reversals_then_normalizes_win_points():
    question = {
        "type": "choice", "instructions": "",
        "criteria": {"a": None, "b": None, "c": None},
    }
    runs = [
        {"pair": ["a", "b"], "probabilities": {"a": .8, "b": .2}},
        {"pair": ["b", "a"], "probabilities": {"a": .6, "b": .4}},
        {"pair": ["a", "c"], "probabilities": {"a": .5, "c": .5}},
        {"pair": ["c", "a"], "probabilities": {"a": .3, "c": .7}},
        {"pair": ["b", "c"], "probabilities": {"b": .9, "c": .1}},
        {"pair": ["c", "b"], "probabilities": {"b": .7, "c": .3}},
    ]
    result = aggregate(question, runs, True)
    # Pair points: a=.7+.4=1.1, b=.3+.8=1.1, c=.6+.2=.8; total=3.
    assert result["probabilities"] == pytest.approx({"a": 1.1 / 3, "b": 1.1 / 3, "c": .8 / 3})
    assert result["choice"] == "a"  # deterministic insertion-order tie

    two = aggregate(
        {**question, "criteria": {"a": None, "b": None}},
        runs[:2],
        True,
    )
    assert two["probabilities"] == pytest.approx({"a": .7, "b": .3})
