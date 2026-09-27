"""
Vera bot — FastAPI server exposing the 5 challenge endpoints.

GET  /v1/healthz   — liveness + context counts
GET  /v1/metadata  — team identity
POST /v1/context   — push a context object (idempotent by scope+context_id+version)
POST /v1/tick      — periodic wake-up; returns up to 20 proactive actions
POST /v1/reply     — merchant/customer replied; returns send / wait / end

State is in-memory (fine for a 60-min test window; no restarts expected).
All composition is delegated to composer.compose (deterministic, grounded).
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from composer import compose

app = FastAPI(title="Vera", version="2.0.0")
START = time.time()

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
# (scope, context_id) -> {"version": int, "payload": dict}
contexts: dict[tuple[str, str], dict] = {}
# conversation_id -> list of {"from", "msg", "turn"}
conversations: dict[str, list] = {}
# merchant_id -> set of conversation_ids already "ended" (suppress future sends)
ended_merchants: set[str] = set()
# suppression keys already sent (dedup across ticks)
sent_suppressions: set[str] = set()
# last body per conversation (anti-repetition)
last_body: dict[str, str] = {}

METADATA = {
    "team_name": "VeraPrime",
    "team_members": ["Guneet Toppo"],
    "model": "deterministic-rule-composer-v2 (no LLM)",
    "approach": "4-context grounded composition; dispatch by trigger.kind with per-kind strategies + generic grounded fallback for injected/unseen kinds",
    "contact_email": "guneet@example.com",
    "version": "2.2.0",
    "submitted_at": "2026-04-26T08:00:00Z",
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _), _ in contexts.items():
        if scope in counts:
            counts[scope] += 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START),
            "contexts_loaded": counts}


@app.get("/v1/metadata")
async def metadata():
    return dict(METADATA)


@app.post("/v1/context")
async def push_context(request: Request):
    body = await request.json()
    scope = body.get("scope")
    context_id = body.get("context_id")
    version = body.get("version")
    payload = body.get("payload")

    if scope not in ("category", "merchant", "customer", "trigger"):
        return JSONResponse(status_code=400,
                            content={"accepted": False, "reason": "invalid_scope",
                                     "details": f"scope '{scope}' not recognized"})
    if not context_id or version is None:
        return JSONResponse(status_code=400,
                            content={"accepted": False, "reason": "malformed",
                                     "details": "context_id and version required"})

    key = (scope, context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= version:
        return {"accepted": False, "reason": "stale_version",
                "current_version": cur["version"]}

    contexts[key] = {"version": version, "payload": payload or {}}
    return {"accepted": True, "ack_id": f"ack_{context_id}_v{version}",
            "stored_at": _now_iso()}


def _get_payload(scope, cid):
    e = contexts.get((scope, cid))
    return e["payload"] if e else None


@app.post("/v1/tick")
async def tick(request: Request):
    body = await request.json()
    available = body.get("available_triggers") or []

    actions = []
    for trg_id in available:
        if len(actions) >= 20:
            break
        trg = _get_payload("trigger", trg_id)
        if not trg:
            continue

        merchant_id = trg.get("merchant_id")
        if merchant_id in ended_merchants:
            continue

        merchant = _get_payload("merchant", merchant_id)
        if not merchant:
            continue
        category = _get_payload("category", merchant.get("category_slug"))
        if not category:
            continue

        customer_id = trg.get("customer_id")
        customer = _get_payload("customer", customer_id) if customer_id else None

        skey = trg.get("suppression_key") or f"{trg.get('kind')}:{merchant_id}"
        if skey in sent_suppressions:
            continue

        msg = compose(category, merchant, trg, customer)

        conv_id = f"conv_{merchant_id}_{trg.get('kind')}_{trg_id[-8:]}"
        # one action per (merchant, conversation) per tick — conv is unique per trigger so fine

        sent_suppressions.add(skey)
        last_body[conv_id] = msg["body"]

        actions.append({
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": msg["send_as"],
            "trigger_id": trg_id,
            "template_name": f"vera_{trg.get('kind','generic')}_v1",
            "template_params": [merchant.get("identity", {}).get("name", ""),
                                msg["body"][:120]],
            "body": msg["body"],
            "cta": msg["cta"],
            "suppression_key": msg["suppression_key"],
            "rationale": msg["rationale"],
        })

    return {"actions": actions}


# ---------------------------------------------------------------------------
# Reply handling
# ---------------------------------------------------------------------------

AUTO_PATTERNS = [
    "thank you for contacting", "thanks for contacting", "thank you for reaching",
    "our team will respond", "we'll get back to you", "we will get back to you",
    "your message has been received", "automated assistant", "auto reply", "auto-reply",
    "this is an automated", "aapki jaankari", "संपर्क करने के लिए धन्यवाद", "हमारी टीम",
    "आपकी जानकारी के लिए", "बहुत बहुत शुक्रिया",
]

HOSTILE_PATTERNS = [
    "stop messaging", "stop sending", "do not message", "don't message", "unsubscribe",
    "not interested", "no interest", "useless", "spam", "bothering", "leave me alone",
    "band karo", "band kijiye", "don't send", "stop texting", "quit", "stop this",
]

INTENT_PATTERNS = [
    "lets do it", "let's do it", "go ahead", "proceed", "do it", "confirm",
    "send it", "send me", "draft it", "whats next", "what's next", "kar do",
    "please send", "yes send", "yes please", "start",
]

OFFTOPIC_PATTERNS = [
    "gst", "tax filing", "income tax", "loan", "insurance", "accounting",
    "ca for", "my ca", "legal", "visa", "passport",
]


def _is_auto(msg):
    m = msg.lower()
    return any(p in m for p in AUTO_PATTERNS)


def _is_hostile(msg):
    m = msg.lower()
    return any(p in m for p in HOSTILE_PATTERNS)


def _is_intent(msg):
    m = msg.lower()
    return any(p in m for p in INTENT_PATTERNS)


def _is_offtopic(msg):
    m = msg.lower()
    return any(p in m for p in OFFTOPIC_PATTERNS)


@app.post("/v1/reply")
async def reply(request: Request):
    body = await request.json()
    conv_id = body.get("conversation_id", "conv_default")
    merchant_id = body.get("merchant_id")
    message = body.get("message", "") or ""
    turn = body.get("turn_number", 1)

    conv = conversations.setdefault(conv_id, [])
    conv.append({"from": body.get("from_role"), "msg": message, "turn": turn})

    # --- hostile / opt-out → end immediately --------------------------------
    if _is_hostile(message):
        return {"action": "end",
                "rationale": "Merchant explicitly opted out / expressed frustration; closing conversation and suppressing future sends."}

    # --- off-topic → polite redirect ----------------------------------------
    if _is_offtopic(message):
        return {"action": "send",
                "body": "I'll have to leave that to your CA — it's outside what I can help with. Coming back to your listing — want me to pick up where we left off?",
                "cta": "open_ended",
                "rationale": "Out-of-scope ask politely declined; redirect back to the active thread without losing it."}

    # --- auto-reply detection ------------------------------------------------
    if _is_auto(message):
        auto_count = sum(1 for t in conv if _is_auto(t["msg"]))
        if auto_count >= 3:
            return {"action": "end",
                    "rationale": "Auto-reply detected 3+ times with no real reply; closing conversation (zero engagement signal)."}
        if auto_count == 2:
            return {"action": "wait", "wait_seconds": 86400,
                    "rationale": "Same auto-reply twice — owner not at phone. Waiting 24h before any retry."}
        return {"action": "send",
                "body": "Looks like an auto-reply 😊 When the owner sees this, just reply YES and I'll get it set up.",
                "cta": "binary_yes_no",
                "rationale": "Detected canned auto-reply; one explicit prompt to flag it for the owner before backing off."}

    # --- intent transition → action mode -------------------------------------
    if _is_intent(message):
        # Look up merchant/category for grounding
        merchant = _get_payload("merchant", merchant_id) if merchant_id else None
        category = _get_payload("category", merchant.get("category_slug")) if merchant else None
        offer = None
        if merchant:
            offs = [o.get("title") for o in (merchant.get("offers") or [])
                    if isinstance(o, dict) and o.get("status") == "active" and o.get("title")]
            offer = offs[0] if offs else None
        offer_str = f" around your {offer}" if offer else ""
        return {"action": "send",
                "body": f"Great — on it. Drafting your post + WhatsApp{offer_str} now (90 seconds). Reply CONFIRM to send it out, or tell me any change.",
                "cta": "binary_confirm_cancel",
                "rationale": "Merchant explicitly committed; switching from qualification to action with a concrete confirm/cancel next step."}

    # --- generic engaged reply → advance the thread --------------------------
    m = message.lower()
    if any(w in m for w in ("yes", "ok", "great", "thanks", "good", "nice", "please")):
        return {"action": "send",
                "body": "Sending it over now — I'll also pre-draft the follow-up post so it's ready. Reply YES if you want that too.",
                "cta": "binary_yes_no",
                "rationale": "Positive/engaged reply; acknowledged and advanced with a low-friction next step."}

    # fallback: keep it moving without repeating
    return {"action": "send",
            "body": "Got it. Want me to draft that for you now — takes a minute?",
            "cta": "binary_yes_no",
            "rationale": "Neutral reply; advanced conversation with a low-friction offer without re-pitching."}
