"""Post-submission context-injection test.

The real judge injects (a) new digest items, (b) updated performance numbers, and
(c) NEW trigger kinds. This verifies the bot composes grounded, non-hallucinating
messages — via named handlers for the kinds the brief names, and via the generic
fallback for anything genuinely unseen.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from composer import compose

root = Path(__file__).parent.parent
cat = json.load(open(root / "dataset" / "categories" / "dentists.json", encoding="utf-8"))
merch = json.load(open(root / "expanded" / "merchants" / "m_001_drmeera_dentist_delhi.json", encoding="utf-8"))

# (a) injected NEW digest item (version bump)
cat_injected = json.loads(json.dumps(cat))
cat_injected["digest"] = [{
    "id": "d_NEW_injected", "kind": "research",
    "title": "4-month fluoride recall beats 6-month in a 4,500-patient trial",
    "source": "JIDA Nov 2026, p.22", "trial_n": 4500,
    "patient_segment": "high_risk_adults", "summary": "...", "actionable": "..."
}]

# (b) updated performance (metric shift)
merch_shifted = json.loads(json.dumps(merch))
merch_shifted["performance"]["ctr"] = 0.031  # now ABOVE peer median 0.030

BAD = re.compile(r"\bNone\b|\?\?|\bNaN\b|  +", re.IGNORECASE)
fails = 0

def check(tag, cond, detail):
    global fails
    if not cond:
        fails += 1
    print(f"[{'PASS' if cond else 'FAIL'}] {tag}\n      {detail}")

def mk(kind, payload):
    return {"id": "trg_" + kind, "scope": "merchant", "kind": kind, "source": "external",
            "merchant_id": merch["merchant_id"], "customer_id": None,
            "payload": payload, "urgency": 2, "suppression_key": "k_" + kind, "expires_at": "x"}

print("=== (c) injected kinds the brief names explicitly (§4.3) ===\n")
r = compose(cat_injected, merch, mk("weather_heatwave", {"city": "Delhi", "temp_c": 42}), None)
check("weather_heatwave", "42°C" in r["body"] and not BAD.search(r["body"]), r["body"])

r = compose(cat_injected, merch, mk("local_news_event", {"event": "metro line closure", "hours": 3}), None)
check("local_news_event", "metro line closure" in r["body"] and "3h" in r["body"], r["body"])

r = compose(cat_injected, merch, mk("category_trend_movement", {"query": "clear aligners delhi", "delta_yoy": 0.62}), None)
check("category_trend_movement", "62%" in r["body"] and "YoY" in r["body"], r["body"])

r = compose(cat_injected, merch, mk("scheduled_recurring", {}), None)
check("scheduled_recurring", not BAD.search(r["body"]) and r["body"].strip(), r["body"])

print("\n=== (d) genuinely unseen kind → generic grounded fallback ===\n")
r = compose(cat_injected, merch, mk("surprise_promotion_window", {"window_days": 7}), None)
check("surprise_promotion_window", "7" in r["body"] and not BAD.search(r["body"]) and "Lajpat Nagar" in r["body"], r["body"])

print("\n=== (a) injected digest item picked up (no duplicated trial phrasing) ===\n")
trg_digest = mk("research_digest", {"category": "dentists", "top_item_id": "d_NEW_injected"})
r = compose(cat_injected, merch, trg_digest, None)
dup = r["body"].count("4,500-patient trial") > 1
check("research_digest uses injected item w/o dup", "JIDA Nov 2026" in r["body"] and not dup, r["body"])

print("\n=== (b) shifted metric reflected (CTR now above peer) ===\n")
trg_dip = mk("perf_dip", {"metric": "views", "delta_pct": -0.12, "window": "7d", "vs_baseline": 2410})
r = compose(cat_injected, merch_shifted, trg_dip, None)
check("perf_dip uses shifted CTR", "3.1%" in r["body"], r["body"])

print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILURE(S)'}")
sys.exit(1 if fails else 0)
