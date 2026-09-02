"""Optional PokerAI presolved-preflop integration.

The provider returns solver-produced strategy frequencies, but its preflop
endpoint is a lookup over fixed 6-max packs rather than a live solve.  Pokerit
therefore exposes it as a presolved strategy reference and retains the exact
provider/version/node assumptions in every evidence record.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import os
from typing import Any, Awaitable, Callable

import httpx


POKERAI_BASE_URL = "https://pokerai.bet"
DEFAULT_PREFLOP_VERSION = "6max"
_ALLOWED_POSITIONS = frozenset({"SB", "BB", "UTG", "MP", "CO", "BTN"})
_POSITION_MAP = {"HJ": "MP"}
_ALLOWED_ACTIONS = frozenset({"fold", "call", "raise"})
_ALLOWED_SITUATIONS = frozenset({"RFI", "Limp", "Raise", "3-Bet", "4-Bet", "5-Bet"})


@dataclass(frozen=True)
class PokerAIQueryResult:
    evidence: dict[str, Any] | None
    failure: str | None = None


def configured_api_key() -> str | None:
    value = os.environ.get("POKERAI_API_KEY", "").strip()
    return value or None


def configured_preflop_version() -> str:
    configured = os.environ.get(
        "POKERAI_PREFLOP_VERSION", DEFAULT_PREFLOP_VERSION,
    ).strip()
    return configured or DEFAULT_PREFLOP_VERSION


def _position(value: Any) -> str | None:
    normalized = _POSITION_MAP.get(str(value or "").upper(), str(value or "").upper())
    return normalized if normalized in _ALLOWED_POSITIONS else None


def _cards(value: Any) -> str | None:
    cards = list(value or [])
    if len(cards) != 2:
        return None
    compact = "".join(str(card) for card in cards)
    if len(compact) != 4 or compact[:2].lower() == compact[2:].lower():
        return None
    if any(compact[index].upper() not in "23456789TJQKA" for index in (0, 2)):
        return None
    if any(compact[index].lower() not in "shdc" for index in (1, 3)):
        return None
    return compact[0].upper() + compact[1].lower() + compact[2].upper() + compact[3].lower()


def build_preflop_request(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Build one supported PokerAI request, or fail closed.

    PokerAI's contract accepts the action sequence before Hero's *first*
    preflop decision and currently exposes 6-max fixed-stack packs.  A later
    Hero re-decision is deliberately not rewritten into a different node.
    """
    if snapshot.get("street") != "preflop":
        return None
    facts = snapshot.get("known_facts") or {}
    derived = snapshot.get("derived_calculations") or {}
    ante = facts.get("ante") or {}
    if str(facts.get("format") or "").lower() != "cash":
        return None
    if int(facts.get("players_dealt") or 0) != 6:
        return None
    if ante.get("type") not in {None, "none"} or float(ante.get("amount") or 0) != 0:
        return None
    hero_stack_bb = float(derived.get("hero_stack_bb_at_decision") or 0)
    if not 98 <= hero_stack_bb <= 102:
        return None

    hero_position = _position(facts.get("hero_position"))
    hole_cards = _cards(facts.get("hero_cards"))
    if hero_position is None or hole_cards is None:
        return None

    blinds = facts.get("blinds") or {}
    big_blind = float(blinds.get("big") or 0)
    small_blind = float(blinds.get("small") or 0)
    if big_blind <= 0 or small_blind <= 0:
        return None

    actions: list[dict[str, Any]] = [
        {"position": "SB", "action": "small blind", "amount": small_blind / big_blind},
        {"position": "BB", "action": "big blind", "amount": 1.0},
    ]
    observed_raises: list[float] = []
    for item in facts.get("action_history_before") or []:
        if item.get("actor") == "Hero":
            return None
        position = _position(item.get("position"))
        action = str(item.get("action") or "").lower()
        if position is None or action not in _ALLOWED_ACTIONS:
            return None
        row: dict[str, Any] = {"position": position, "action": action}
        if action != "fold":
            amount_paid = float(item.get("amount_paid") or 0)
            if amount_paid <= 0:
                return None
            row["amount"] = amount_paid / big_blind
        if item.get("all_in"):
            row["allin"] = True
        if action == "raise":
            observed_raises.append(round(float(item.get("amount_to") or 0) / big_blind, 4))
        actions.append(row)

    return {
        "hole_cards": hole_cards,
        "positions": {"hero": hero_position},
        "preflop_actions": actions,
        "preflop_version": configured_preflop_version(),
        "_pokerit": {
            "decision_id": snapshot.get("decision_id"),
            "hero_stack_bb": hero_stack_bb,
            "observed_raise_sizes_bb": observed_raises,
        },
    }


def _normalized_strategy(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    raw = payload.get("strategy")
    if not isinstance(raw, list) or not raw:
        return None
    strategy: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        action = str(item.get("action") or "").lower()
        if action not in _ALLOWED_ACTIONS:
            return None
        try:
            frequency = float(item.get("frequency"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(frequency) or not 0 <= frequency <= 1:
            return None
        normalized: dict[str, Any] = {
            "action": action,
            "frequency": round(frequency, 6),
        }
        for key in ("amount_bb", "sizing_pot"):
            if item.get(key) is not None:
                try:
                    value = float(item[key])
                except (TypeError, ValueError):
                    return None
                if not math.isfinite(value):
                    return None
                normalized[key] = value
        if item.get("allin") is True:
            normalized["all_in"] = True
        strategy.append(normalized)
    if not 0.98 <= sum(item["frequency"] for item in strategy) <= 1.02:
        return None
    return strategy


def _evidence_from_response(
    payload: dict[str, Any], request_body: dict[str, Any],
) -> dict[str, Any] | None:
    situation = str(payload.get("situation") or "")
    strategy = _normalized_strategy(payload)
    voluntary_actions = [
        item for item in request_body["preflop_actions"]
        if item.get("action") in _ALLOWED_ACTIONS
    ]
    raises = sum(item.get("action") == "raise" for item in voluntary_actions)
    expected_situation = (
        "Limp" if not raises and voluntary_actions else
        "RFI" if not raises else
        "Raise" if raises == 1 else
        "3-Bet" if raises == 2 else
        "4-Bet" if raises == 3 else
        "5-Bet"
    )
    if (
        situation not in _ALLOWED_SITUATIONS
        or situation != expected_situation
        or payload.get("hole_cards") != request_body["hole_cards"]
        or strategy is None
    ):
        return None
    private = request_body.get("_pokerit") or {}
    evidence = {
        "type": "preflop_strategy_api",
        "label": "PokerAI presolved 6-max 100BB preflop reference (fixed, sizing-insensitive pack)",
        "provider": "PokerAI",
        "endpoint": "/v1/gto/preflop",
        "version": request_body["preflop_version"],
        "decision_id": private.get("decision_id"),
        "node": situation,
        "hero_position": request_body["positions"]["hero"],
        "hero_cards": request_body["hole_cards"],
        "strategy": strategy,
        "observed_raise_sizes_bb": private.get("observed_raise_sizes_bb") or [],
        "solution_assumptions": {
            "table_size": 6,
            "stack_bb": 100,
            "open_to_bb": 3,
            "three_bet_to_bb": 9,
            "four_bet_to_bb": 25,
            "five_bet_to_bb": 100,
            "frequencies_change_with_observed_sizing": False,
        },
        "match_status": "presolved_reference",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }
    quota = payload.get("quota")
    if isinstance(quota, dict):
        try:
            used = int(quota.get("used"))
            quota_limit = int(quota.get("limit"))
        except (TypeError, ValueError):
            pass
        else:
            if used >= 0 and quota_limit > 0:
                evidence["quota_after_query"] = {
                    "used": used,
                    "limit": quota_limit,
                }
    return evidence


async def query_preflop_strategy(
    snapshot: dict[str, Any],
    *,
    api_key: str | None = None,
    client: httpx.AsyncClient | None = None,
    retry_transient: bool = True,
) -> PokerAIQueryResult:
    request_body = build_preflop_request(snapshot)
    if request_body is None:
        return PokerAIQueryResult(None, "unsupported_spot")
    api_token = api_key or configured_api_key()
    if not api_token:
        return PokerAIQueryResult(None, "not_configured")

    public_body = {
        field: value
        for field, value in request_body.items()
        if field != "_pokerit"
    }
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(5.0))
    try:
        attempts = 2 if retry_transient else 1
        for attempt in range(attempts):
            try:
                response = await http.post(
                    f"{POKERAI_BASE_URL}/v1/gto/preflop",
                    headers={"Authorization": f"Bearer {api_token}"},
                    json=public_body,
                )
            except httpx.RequestError:
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.25)
                    continue
                return PokerAIQueryResult(None, "network_error")

            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError:
                    return PokerAIQueryResult(None, "invalid_response")
                evidence = _evidence_from_response(payload, request_body)
                return PokerAIQueryResult(
                    evidence,
                    None if evidence is not None else "invalid_response",
                )

            retryable = response.status_code >= 500
            if response.status_code == 429:
                try:
                    retryable = response.json().get("status") == "busy"
                except ValueError:
                    retryable = False
            if retryable and attempt + 1 < attempts:
                await asyncio.sleep(0.25)
                continue
            try:
                error_code = str(response.json().get("error") or response.status_code)
            except ValueError:
                error_code = str(response.status_code)
            return PokerAIQueryResult(None, f"provider_{error_code}")
        return PokerAIQueryResult(None, "provider_error")
    except Exception:  # noqa: BLE001 - optional provider must always fail closed
        return PokerAIQueryResult(None, "client_error")
    finally:
        if owns_client:
            try:
                await http.aclose()
            except Exception:  # noqa: BLE001 - never break coaching on cleanup
                pass


def apply_pokerai_evidence(
    snapshot: dict[str, Any], evidence: dict[str, Any],
) -> dict[str, Any]:
    """Prefer successful API evidence while preserving local packs as fallback."""
    sources = [
        source
        for source in snapshot.get("evidence_sources") or []
        if source.get("type") != "range_knowledge_base"
    ]
    sources.append(evidence)
    snapshot["evidence_sources"] = sources
    return snapshot


async def query_postgame_snapshots(
    snapshots: list[dict[str, Any]],
    *,
    limit: int,
    query: Callable[[dict[str, Any]], Awaitable[PokerAIQueryResult]] | None = None,
) -> dict[str, Any]:
    """Query at most one eligible decision per hand, capped per evaluation.

    The default provider call does not retry, so ``limit`` is also a strict
    upper bound on post-game HTTP requests, not merely selected decisions.
    """
    limit = max(0, int(limit))
    selected: list[dict[str, Any]] = []
    seen_rounds: set[int] = set()
    for snapshot in snapshots:
        round_count = int(snapshot.get("round_count") or 0)
        if round_count in seen_rounds or build_preflop_request(snapshot) is None:
            continue
        selected.append(snapshot)
        seen_rounds.add(round_count)
        if len(selected) >= limit:
            break

    evidence_by_decision: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, str]] = []
    quota_after: dict[str, int] | None = None
    for snapshot in selected:
        decision_id = str(snapshot.get("decision_id") or "")
        try:
            result = (
                await query(snapshot)
                if query is not None
                else await query_preflop_strategy(snapshot, retry_transient=False)
            )
        except Exception:  # noqa: BLE001 - keep the remaining capped batch usable
            failures.append({"decision_id": decision_id, "reason": "client_error"})
            continue
        if result.evidence is not None:
            evidence_by_decision[decision_id] = result.evidence
            if isinstance(result.evidence.get("quota_after_query"), dict):
                quota_after = result.evidence["quota_after_query"]
        else:
            failures.append({"decision_id": decision_id, "reason": result.failure or "unknown"})

    metadata = {
        "configured": configured_api_key() is not None,
        "version": configured_preflop_version(),
        "call_limit": limit,
        "attempted": len(selected),
        "resolved": len(evidence_by_decision),
        "failures": failures,
        "evidence_by_decision": evidence_by_decision,
    }
    if quota_after is not None:
        metadata["quota_after"] = quota_after
    return metadata
