# Vera — Merchant-AI Message Engine

**magicpin AI Challenge — "Build Vera Better"**

A deterministic, grounded message-composition bot that acts as the outbound
messaging engine behind Vera, magicpin's merchant-growth AI assistant. It exposes the
five HTTP endpoints the judge harness calls and composes every message from four
layers of structured context — never inventing a fact it wasn't given.

```
compose(category, merchant, trigger, customer?)  →  { body, cta, send_as, suppression_key, rationale }
```

---

## 1. The challenge in a nutshell

Vera talks to ~6,000–10,000 Indian merchants a day over WhatsApp, helping them grow
their Google Business Profile, run campaigns, and reply to customers faster. The
challenge is to build a **better** version of the message engine behind it.

What you submit is a **public bot URL** exposing five endpoints. magicpin's judge
harness pushes structured context (category, merchant, customer, trigger), ticks your
bot periodically, simulates merchant/customer replies, and scores every message.

**Scoring is by an LLM judge, 0–10 on five dimensions (total /50):**

| Dimension | What the judge checks |
|---|---|
| Decision quality | Did you pick the *one* signal that should drive this message? |
| Specificity | Real numbers, dates, prices, sources — anchored, verifiable facts |
| Category fit | Voice matches the business type (clinical, visual, timely, utility-first) |
| Merchant fit | Personalized to *this* merchant's metrics, offers, history, language |
| Engagement compulsion | One strong, low-effort reason to reply now |

**The twist:** after submission the judge injects context you've never seen — new
digest items, shifted performance numbers, new triggers, surprise customer scopes.
Bots that ground every output in the context they were *actually given* win; bots
that pattern-match or hallucinate lose.

---

## 2. Architecture

```
                        ┌─────────────────────────────────┐
   judge harness  ─────►│  bot.py  (FastAPI, 5 endpoints) │
   HTTP / JSON          │  ─ context store (versioned)    │
                        │  ─ tick → compose               │
                        │  ─ reply → send / wait / end     │
                        └───────────────┬─────────────────┘
                                        │
                          ┌─────────────▼─────────────┐
                          │  composer.py (deterministic)│
                          │  dispatch by trigger.kind  │
                          │  26 strategies + fallback  │
                          └───────────────────────────┘
```

No network calls, no LLM, no external dependencies beyond FastAPI + Uvicorn.

### File layout

| File | Responsibility |
|---|---|
| `bot.py` | FastAPI server: the 5 endpoints, context store, tick/suppression, reply routing |
| `composer.py` | The deterministic `compose()` engine — 26 per-`trigger.kind` strategies + a generic grounded fallback |
| `test_compose.py` | Runs all 100 triggers (and 30 canonical pairs) through the composer, flags fabrication/format defects |
| `test_injection.py` | Exercises the brief's §4.3 injected trigger kinds + a truly unknown kind |
| `test_api.py` | Full HTTP harness simulation: context push, idempotency, tick cap, suppression, all 5 reply scenarios |
| `requirements.txt` | `fastapi`, `uvicorn` (only two runtime deps) |
| `Dockerfile` | Container build for Railway / Hetzner / any Docker host |
| `render.yaml` | Render free-tier web service definition |

---

## 3. The composition engine (`composer.py`)

### Core principle: grounded, never fabricated

Every number, price, date, source, and offer in a message is pulled from the pushed
context. If a fact is absent, the sentence is omitted — never guessed. This is the
single most important design decision, because it maps directly onto the judge's
penalties:

- Fabricating data not in context → **-2**
- Generic templated copy → capped low
- Hallucinated citations / competitor names → rejected quickly

### The four contexts

| Context | Answers | Used for |
|---|---|---|
| **Category** | How do we talk to *this type* of business? | voice, offer catalog, peer benchmarks, digest, seasonal beats, trend signals |
| **Merchant** | Who is *this* business and how are they doing? | name/owner/locality, performance + deltas, active offers, customer aggregate, signals, review themes |
| **Trigger** | Why now? | kind (dispatch key), scope, urgency, payload, suppression key |
| **Customer** (optional) | Who is the merchant's customer? | name, language pref, relationship/state, preferences, consent |

### Dispatch by trigger kind

`compose()` routes on `trigger.kind`. 26 kinds have a dedicated strategy; anything
unseen falls through to a generic grounded composer.

| Kind | Scope | Strategy highlights |
|---|---|---|
| `research_digest` | merchant | Find the digest item, cite its source + trial N, tie to merchant cohort |
| `regulation_change` | merchant | Compliance item + deadline, offer a checklist |
| `recall_due` | customer | Real slots + active offer, honor language + emoji by category |
| `perf_dip` / `perf_spike` | merchant | Real delta vs baseline, CTR vs peer median; **never claims a drop that isn't in the data** |
| `renewal_due` | merchant | Expired → winback angle; ≤30d → renew; else → value check-in |
| `festival_upcoming` | merchant | Festival + category seasonal beat |
| `wedding_package_followup` | customer | Days-to-wedding + offer, single binary commit |
| `curious_ask_due` | merchant | "Asking the merchant" lever + trend-signal guess |
| `winback_eligible` | merchant | Days since expiry + lapsed count, loss-aversion framing |
| `ipl_match_today` | merchant | **Contrarian call**: weekend match → skip dine-in, push delivery |
| `review_theme_emerged` | merchant | Theme + occurrences + real quote |
| `milestone_reached` | merchant | Value-now vs milestone gap |
| `active_planning_intent` | merchant | Switch to *action* (draft the artifact) — no re-qualifying |
| `seasonal_perf_dip` | merchant | Reframe as expected lull, redirect spend to retention |
| `customer_lapsed_hard` / `_soft` | customer | No-shame winback, offer matched to prior goal |
| `trial_followup` | customer | Trial date + next slot + offer |
| `supply_alert` | merchant | Molecule + batches + derived affected-customer count |
| `chronic_refill_due` | customer | Molecules + run-out date + senior/delivery offers (pharmacy-only guard) |
| `category_seasonal` | merchant | Shelf re-arrange from trend list |
| `gbp_unverified` | merchant | Uplift estimate + verification path |
| `cde_opportunity` | merchant | CDE item + credits + fee |
| `competitor_opened` | merchant | Competitor + distance + their offer vs yours |
| `dormant_with_vera` | merchant | Days since last message + a concrete choice |
| `appointment_tomorrow` | customer | Confirm / reschedule |
| *(anything else)* | any | **Generic grounded composer** — extracts whatever facts exist, asks one low-friction question |

### Category voice is data-driven

Salutation, emoji, and tone come from each category's `voice` profile, not hardcoded
strings. Dentists get `Dr. {name},` and a clinical peer tone; salons get `Hi {name},`
and warm-practical tone; pharmacies get precise, trustworthy copy. Emoji are
category-appropriate and only where they fit (🦷 dentistry, 💪 gyms, 💇‍♀️ salons).

### Language / code-mix

`identity.languages` and the customer's `language_pref` drive the language. `hi` /
`hi-en mix` customers get natural Hindi-English code-mix ("Apke liye 2 slots ready
hain…"); senior citizens get a respectful `Namaste` salutation.

### CTA discipline

One primary call-to-action per send, landing in the last sentence:

| CTA | When |
|---|---|
| `binary_yes_no` | Action triggers — lowest friction |
| `open_ended` | Knowledge/curiosity triggers inviting continuation |
| `multi_choice_slot` | Booking flows (Reply 1 / Reply 2) |
| `binary_confirm_cancel` | Post-commitment confirm |
| `none` | Pure-information sends |

### The generic fallback is load-bearing

The generated dataset ships a large share of triggers with `payload: {placeholder: true}`
and merchants with empty `offers: []`. More importantly, **the real judge injects new
trigger kinds you've never seen**. The generic composer extracts whatever
payload/merchant/category facts *do* exist and produces a grounded, specific, single-CTA
message — so the bot adapts to injected context without hallucinating.

---

## 4. The API server (`bot.py`)

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/v1/healthz` | Liveness + context counts | Polled every 60s; 3 consecutive failures = disqualification |
| GET | `/v1/metadata` | Team identity + model | Dynamic — reports the polish model when enabled |
| POST | `/v1/context` | Push a context object | Idempotent by `(scope, context_id, version)`; higher version replaces atomically; stale → 409, bad scope → 400 |
| POST | `/v1/tick` | Periodic wake-up | Returns ≤20 actions; honors suppression keys; skips opted-out merchants |
| POST | `/v1/reply` | Merchant/customer replied | Returns `send` / `wait` / `end` |

### State & dedup

- **Context store** — `(scope, context_id) → {version, payload}`, in-memory (fine for a
  60-min test; no restarts expected).
- **Suppression** — a `suppression_key` is marked sent once; re-ticking the same trigger
  produces nothing (restraint is rewarded, spam is penalized).
- **Tick cap** — hard 20 actions/tick, one per `(merchant, conversation)`.

### Reply routing (rule-based, deterministic)

| Situation | Response |
|---|---|
| Canned auto-reply ("Thank you for contacting…") | 1st → one-line flag; 2nd → `wait` 24h; 3rd+ → `end` |
| Explicit opt-out / hostility ("stop messaging", "useless spam") | `end` immediately |
| Intent transition ("let's do it", "go ahead", "send it") | Switch to **action mode** — confirm + concrete next step, no re-qualifying |
| Off-topic ("can you help with my GST?") | Polite redirect back to the active thread |
| Engaged ("yes", "please send") | Acknowledge + advance with a low-friction next step |

---

## 5. Why deterministic-only (no LLM)

The bot ships **pure rule-engine composition** — no LLM call on any endpoint. This is a
deliberate design choice, not a missing feature:

1. **The challenge scores decisions, not prose.** "Our AI scores decisions, not just
   writing style." Every rubric dimension — decision quality, specificity, category fit,
   merchant fit, engagement — is about *choosing and grounding* the right message. Prose
   polish moves the needle far less than picking the right signal and citing real numbers.
2. **Zero failure modes.** No network call can time out, rate-limit, return empty, or
   hallucinate. The bot cannot fabricate a fact, cannot exceed the 30s timeout, and cannot
   drift between runs — all three are instant-disqualifiers in the rubric.
3. **Cost is zero.** No API budget, no key management, no rate limits to design around.
4. **Determinism is required.** "Your output should stay deterministic for the same input
   and simulator settings." A rule engine satisfies this by construction; a temperature-0
   LLM only approximates it.

An LLM polish layer was built and measured during development (guarded against
hallucination, with a deterministic fallback). It scored 34/50 versus 35/50 for the pure
rule engine — no improvement — at meaningful complexity and cost. It was removed. The
35/50 figure is the local judge_simulator result; see §8.

---

## 6. Getting started

**Prerequisites:** Python 3.10+ (developed on 3.11), and the challenge dataset.

```bash
# 1. (optional) generate the expanded dataset
cd D:/magicpin
python dataset/generate_dataset.py --seed-dir dataset --out expanded
# → expanded/ with 50 merchants, 200 customers, 100 triggers, 30 test pairs

# 2. install + run the bot
cd vera-bot
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Then verify: `curl http://localhost:8080/v1/healthz` → `{"status":"ok", ...}`.

---

## 7. Testing

```bash
cd vera-bot

# Composer correctness — all 100 triggers + 30 canonical pairs
# flags empty bodies, "None" leaks, double spaces, bad CTA/send_as
python test_compose.py

# Full HTTP contract — context push, idempotency, version bump, invalid scope,
# tick cap (20), suppression (→0), and all 5 reply scenarios
python test_api.py http://127.0.0.1:8081   # pass your bot URL
```

**Judge simulator (official):** the challenge's `judge_simulator.py` scores your bot
with an LLM judge. It needs an LLM key for *the judge itself*:

```bash
cd D:/magicpin
# edit judge_simulator.py: set LLM_PROVIDER, LLM_API_KEY, LLM_MODEL, BOT_URL
python judge_simulator.py
```

The simulator uses the seed dataset (25 triggers / 10 merchants); your bot handles any
context pushed regardless.

---

## 8. Deployment

**Render (recommended, free):** push this repo to GitHub, create a **Web Service**, and
point it at `render.yaml` (or set build/start manually):

- Build: `pip install -r requirements.txt`
- Start: `uvicorn bot:app --host 0.0.0.0 --port $PORT`
- Health check: `/v1/healthz`

**Docker (Railway / Hetzner / any host):**

```bash
docker build -t vera-bot .
docker run -p 8080:8080 vera-bot
```

---

## 9. Known limitations & what would help

- **Placeholder payloads** — a large share of the *generated* dataset ships
  `payload: {placeholder: true}` and empty `offers: []`. The engine handles these
  gracefully, but fully-populated payloads would exercise the deeper paths. This is
  exactly what the real judge will stress, and the design targets it.
- **Single CTA per send** is enforced by construction; multi-turn *cadence* (beyond the
  reply handler) is deliberately minimal.
- **No URLs in messages** (Meta would reject them; the brief flags a -3 penalty).
