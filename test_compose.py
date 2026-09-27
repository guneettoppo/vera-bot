"""Run compose() over all 100 triggers (and the 30 canonical test pairs) and flag defects."""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from composer import compose

root = Path(__file__).parent.parent
cats = {f.stem: json.load(open(f, encoding="utf-8")) for f in (root / "dataset" / "categories").glob("*.json")}

merchants = {}
for f in (root / "expanded" / "merchants").glob("*.json"):
    m = json.load(open(f, encoding="utf-8"))
    merchants[m["merchant_id"]] = m

customers = {}
for f in (root / "expanded" / "customers").glob("*.json"):
    c = json.load(open(f, encoding="utf-8"))
    customers[c["customer_id"]] = c

triggers = {}
for f in (root / "expanded" / "triggers").glob("*.json"):
    t = json.load(open(f, encoding="utf-8"))
    triggers[t["id"]] = t

pairs = json.load(open(root / "expanded" / "test_pairs.json", encoding="utf-8"))["pairs"]
pair_ids = {p["trigger_id"] for p in pairs}

BAD = re.compile(r"\bNone\b|\?\?|\bNaN\b|  +|,,|\.\.", re.IGNORECASE)
issues = 0
total = 0

def check(tid, t):
    global issues, total
    m = merchants.get(t["merchant_id"])
    if not m:
        print(f"[SKIP] {tid}: merchant missing {t['merchant_id']}")
        return
    cat = cats.get(m.get("category_slug"))
    c = customers.get(t["customer_id"]) if t.get("customer_id") else None
    r = compose(cat, m, t, c)
    total += 1
    problems = []
    if not r["body"].strip():
        problems.append("empty body")
    mm = BAD.search(r["body"])
    if mm:
        problems.append(f"bad-token '{mm.group()}' in: {r['body'][:90]}")
    if r["cta"] not in ("binary_yes_no", "open_ended", "none", "multi_choice_slot", "binary_confirm_cancel"):
        problems.append(f"cta={r['cta']}")
    if r["send_as"] not in ("vera", "merchant_on_behalf"):
        problems.append(f"send_as={r['send_as']}")
    if problems:
        issues += 1
        print(f"[FAIL] {tid} ({t['kind']})")
        for p in problems:
            print(f"    {p}")
        print(f"    body: {r['body']}")
    else:
        print(f"[ok] {tid} ({t['kind']}) :: {r['body'][:90]}")

print("=== 30 canonical test pairs ===")
for p in pairs:
    check(p["trigger_id"], triggers[p["trigger_id"]])

print(f"\n=== all {len(triggers)} triggers ===")
for tid, t in triggers.items():
    check(tid, t)

print(f"\n{issues} issues across {total} compositions")
sys.exit(1 if issues else 0)
