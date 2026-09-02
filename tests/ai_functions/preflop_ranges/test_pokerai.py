from __future__ import annotations

import asyncio

import httpx

import ai_functions.preflop_ranges.pokerai as pokerai_module
from ai_functions.preflop_ranges.pokerai import (
    PokerAIQueryResult,
    apply_pokerai_evidence,
    build_preflop_request,
    query_postgame_snapshots,
    query_preflop_strategy,
)


def _snapshot(
    *,
    decision_id: str = "1:preflop:2",
    round_count: int = 1,
    position: str = "HJ",
    history: list[dict] | None = None,
    game_format: str = "cash",
) -> dict:
    return {
        "decision_id": decision_id,
        "round_count": round_count,
        "street": "preflop",
        "known_facts": {
            "format": game_format,
            "players_dealt": 6,
            "blinds": {"small": 50, "big": 100},
            "ante": {"type": "none", "amount": 0},
            "hero_position": position,
            "hero_cards": ["Ah", "Kh"],
            "action_history_before": history or [],
        },
        "derived_calculations": {"hero_stack_bb_at_decision": 100},
        "evidence_sources": [
            {"type": "heuristic_inference"},
            {"type": "range_knowledge_base", "pack_id": "cash_6max_100bb_v1"},
        ],
    }


def test_build_request_maps_six_max_hj_to_mp_and_uses_incremental_amounts(monkeypatch):
    monkeypatch.setenv("POKERAI_PREFLOP_VERSION", "6max")
    snapshot = _snapshot(history=[
        {
            "actor": "Bot", "position": "UTG", "action": "raise",
            "amount_paid": 250, "amount_to": 250, "all_in": False,
        },
    ])

    request = build_preflop_request(snapshot)

    assert request["positions"] == {"hero": "MP"}
    assert request["hole_cards"] == "AhKh"
    assert request["preflop_actions"] == [
        {"position": "SB", "action": "small blind", "amount": 0.5},
        {"position": "BB", "action": "big blind", "amount": 1.0},
        {"position": "UTG", "action": "raise", "amount": 2.5},
    ]
    assert request["_pokerit"]["observed_raise_sizes_bb"] == [2.5]


def test_build_request_fails_closed_for_mtt_and_later_hero_redecision():
    assert build_preflop_request(_snapshot(game_format="tournament")) is None
    assert build_preflop_request(_snapshot(history=[
        {
            "actor": "Hero", "position": "CO", "action": "raise",
            "amount_paid": 250, "amount_to": 250,
        },
    ])) is None


def test_query_returns_sanitized_presolved_reference_and_replaces_local_fallback(monkeypatch):
    monkeypatch.setenv("POKERAI_PREFLOP_VERSION", "6max")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(200, json={
            "hole_cards": "AhKh",
            "situation": "Raise",
            "strategy": [
                {"action": "call", "frequency": 0.25},
                {"action": "raise", "frequency": 0.75, "amount_bb": 9},
            ],
            "quota": {"used": 1, "limit": 1000},
        })

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await query_preflop_strategy(
                _snapshot(history=[{
                    "actor": "Bot", "position": "UTG", "action": "raise",
                    "amount_paid": 300, "amount_to": 300,
                }]),
                api_key="test-key",
                client=client,
            )

    result = asyncio.run(run())

    assert result.failure is None
    assert result.evidence["type"] == "preflop_strategy_api"
    assert result.evidence["match_status"] == "presolved_reference"
    assert result.evidence["strategy"] == [
        {"action": "call", "frequency": 0.25},
        {"action": "raise", "frequency": 0.75, "amount_bb": 9.0},
    ]
    assert result.evidence["quota_after_query"] == {"used": 1, "limit": 1000}
    snapshot = apply_pokerai_evidence(_snapshot(), result.evidence)
    assert {source["type"] for source in snapshot["evidence_sources"]} == {
        "heuristic_inference", "preflop_strategy_api",
    }


def test_query_rejects_response_for_a_different_node_or_hand():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "hole_cards": "QsQh",
            "situation": "RFI",
            "strategy": [{"action": "raise", "frequency": 1}],
        })

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await query_preflop_strategy(
                _snapshot(history=[{
                    "actor": "Bot", "position": "UTG", "action": "raise",
                    "amount_paid": 300, "amount_to": 300,
                }]),
                api_key="test-key",
                client=client,
            )

    result = asyncio.run(run())

    assert result.evidence is None
    assert result.failure == "invalid_response"


def test_postgame_query_cap_is_fifteen_and_selects_one_decision_per_hand(monkeypatch):
    monkeypatch.setenv("POKERAI_API_KEY", "configured")
    snapshots = []
    for round_count in range(20):
        snapshots.append(_snapshot(
            decision_id=f"{round_count}:preflop:0",
            round_count=round_count,
            position="BTN",
        ))
        snapshots.append(_snapshot(
            decision_id=f"{round_count}:preflop:2",
            round_count=round_count,
            position="BTN",
        ))
    calls: list[str] = []

    async def fake_query(snapshot):
        calls.append(snapshot["decision_id"])
        return PokerAIQueryResult({
            "type": "preflop_strategy_api",
            "decision_id": snapshot["decision_id"],
        })

    metadata = asyncio.run(query_postgame_snapshots(
        snapshots, limit=15, query=fake_query,
    ))

    assert metadata["attempted"] == 15
    assert metadata["resolved"] == 15
    assert len(calls) == 15
    assert len({call.split(":", 1)[0] for call in calls}) == 15


def test_postgame_default_path_disables_retries_and_keeps_strict_request_cap(monkeypatch):
    monkeypatch.setenv("POKERAI_API_KEY", "configured")
    calls: list[bool] = []

    async def fake_provider(snapshot, *, retry_transient=True):
        calls.append(retry_transient)
        return PokerAIQueryResult(None, "network_error")

    monkeypatch.setattr(pokerai_module, "query_preflop_strategy", fake_provider)
    snapshots = [
        _snapshot(decision_id=f"{index}:preflop:0", round_count=index, position="BTN")
        for index in range(20)
    ]

    metadata = asyncio.run(query_postgame_snapshots(snapshots, limit=15))

    assert metadata["attempted"] == 15
    assert len(calls) == 15
    assert calls == [False] * 15
