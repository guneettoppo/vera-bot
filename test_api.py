"""End-to-end API test against a running bot (default http://127.0.0.1:8081).

Simulates the judge harness lifecycle: warmup (context push + idempotency),
tick over the 30 canonical test pairs, and the reply scenarios.
"""
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8081"
ROOT = Path(__file__).parent.parent
CATS = ROOT / "dataset" / "categories"
EXP = ROOT / "expanded"

fails = []

def call(method, path, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())

def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        fails.append(name)

# 1. healthz / metadata
s, d = call("GET", "/v1/healthz"); check("healthz", s == 200 and d["status"] == "ok", d)
s, d = call("GET", "/v1/metadata"); check("metadata", s == 200 and "team_name" in d, d.get("team_name"))

# 2. push contexts
n_cat = n_m = n_c = 0
for f in CATS.glob("*.json"):
    p = json.load(open(f, encoding="utf-8"))
    s, r = call("POST", "/v1/context", {"scope": "category", "context_id": p["slug"], "version": 1, "payload": p, "delivered_at": "x"})
    n_cat += 1 if r.get("accepted") else 0
for f in (EXP / "merchants").glob("*.json"):
    p = json.load(open(f, encoding="utf-8"))
    s, r = call("POST", "/v1/context", {"scope": "merchant", "context_id": p["merchant_id"], "version": 1, "payload": p, "delivered_at": "x"})
    n_m += 1 if r.get("accepted") else 0
for f in (EXP / "customers").glob("*.json"):
    p = json.load(open(f, encoding="utf-8"))
    s, r = call("POST", "/v1/context", {"scope": "customer", "context_id": p["customer_id"], "version": 1, "payload": p, "delivered_at": "x"})
    n_c += 1 if r.get("accepted") else 0
check("context push counts", (n_cat, n_m, n_c) == (5, 50, 200), (n_cat, n_m, n_c))

# 3. idempotency: re-push same version → stale; higher version → accepted
p = json.load(open(EXP / "merchants" / "m_001_drmeera_dentist_delhi.json", encoding="utf-8"))
s, r = call("POST", "/v1/context", {"scope": "merchant", "context_id": p["merchant_id"], "version": 1, "payload": p, "delivered_at": "x"})
check("idempotency (stale v1)", r.get("reason") == "stale_version", r)
p2 = dict(p); p2["performance"] = dict(p["performance"], views=9999)
s, r = call("POST", "/v1/context", {"scope": "merchant", "context_id": p["merchant_id"], "version": 2, "payload": p2, "delivered_at": "x"})
check("version bump v2 accepted", r.get("accepted") is True, r)
s, r = call("POST", "/v1/context", {"scope": "bogus", "context_id": "x", "version": 1, "payload": {}, "delivered_at": "x"})
check("invalid scope → 400", s == 400, r)

# 4. push triggers + tick over 30 canonical pairs
trigs = {json.load(open(f, encoding="utf-8"))["id"]: json.load(open(f, encoding="utf-8")) for f in (EXP / "triggers").glob("*.json")}
pairs = json.load(open(EXP / "test_pairs.json", encoding="utf-8"))["pairs"]
for tid, t in trigs.items():
    call("POST", "/v1/context", {"scope": "trigger", "context_id": tid, "version": 1, "payload": t, "delivered_at": "x"})

pair_tids = [p["trigger_id"] for p in pairs]
s, d = call("POST", "/v1/tick", {"now": "2026-04-26T10:30:00Z", "available_triggers": pair_tids})
actions = d.get("actions", [])
check("tick caps at 20 actions", len(actions) == 20, f"got {len(actions)}")
# verify every action has required fields
ok = all(all(k in a for k in ("conversation_id","merchant_id","send_as","trigger_id","body","cta","suppression_key","rationale")) for a in actions)
check("action schema complete", ok)
# verify send_as correctness (customer-scoped → merchant_on_behalf)
cust_tids = {t["id"] for t in trigs.values() if t.get("customer_id")}
wrong = [a["trigger_id"] for a in actions if a["send_as"] == "vera" and a["trigger_id"] in cust_tids]
check("send_as correct for customer scope", not wrong, wrong[:3])

# 5. suppression + cap: second tick returns the remaining 10, third returns 0
s, d = call("POST", "/v1/tick", {"now": "2026-04-26T10:35:00Z", "available_triggers": pair_tids})
check("re-tick returns remaining 10", len(d.get("actions", [])) == 10, f"got {len(d.get('actions', []))}")
s, d = call("POST", "/v1/tick", {"now": "2026-04-26T10:40:00Z", "available_triggers": pair_tids})
check("third tick fully suppressed → empty", d.get("actions") == [], f"got {len(d.get('actions', []))}")

# 6. reply scenarios
def reply(conv, msg, turn):
    return call("POST", "/v1/reply", {"conversation_id": conv, "merchant_id": "m_001_drmeera_dentist_delhi", "customer_id": None, "from_role": "merchant", "message": msg, "received_at": "x", "turn_number": turn})

# auto-reply x3 in same conversation
r1 = reply("conv_auto", "Thank you for contacting us! Our team will respond shortly.", 2)
check("auto-reply turn1 → send (flag)", r1[1].get("action") == "send", r1[1].get("action"))
r2 = reply("conv_auto", "Thank you for contacting us! Our team will respond shortly.", 3)
check("auto-reply turn2 → wait", r2[1].get("action") == "wait", r2[1].get("action"))
r3 = reply("conv_auto", "Thank you for contacting us! Our team will respond shortly.", 4)
check("auto-reply turn3 → end", r3[1].get("action") == "end", r3[1].get("action"))

# hostile
r4 = reply("conv_hostile", "Stop messaging me. This is useless spam.", 2)
check("hostile → end", r4[1].get("action") == "end", r4[1].get("action"))

# intent transition
r5 = reply("conv_intent", "Ok let's do it. What's next?", 2)
b = (r5[1].get("body") or "").lower()
check("intent → action (not qualifying)", r5[1].get("action") == "send" and not any(w in b for w in ("would you", "do you", "what if")), r5[1].get("body", "")[:60])

# off-topic
r6 = reply("conv_offtopic", "Btw can you help me with my GST filing?", 2)
check("off-topic → polite redirect", r6[1].get("action") == "send" and "CA" in r6[1].get("body", ""), r6[1].get("body", "")[:60])

# engaged
r7 = reply("conv_engaged", "Yes please, send it.", 2)
check("engaged → advance", r7[1].get("action") == "send", r7[1].get("action"))

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
