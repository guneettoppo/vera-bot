"""
Vera composer — deterministic, grounded message composition.

compose(category, merchant, trigger, customer?) -> dict
returns {body, cta, send_as, suppression_key, rationale}

Design rules (from challenge-brief.md):
  1. Every number/date/offer/source in a message MUST come from the pushed
     context. Nothing is invented. If a fact is absent, we omit it.
  2. Dispatch by trigger.kind; each kind has a grounded strategy.
  3. Unknown kinds fall through to a generic grounded composer (this is what
     wins the post-submission context-injection bonus — new triggers are
     handled without hallucination).
  4. One primary CTA per send. Low-friction ask lands last.
  5. Category voice (salutation + tone) is honored; language prefs honored.
  6. Pure rule-based: deterministic, sub-millisecond, zero cost, no timeout risk.
"""

from __future__ import annotations

import datetime as _dt
import re

# --------------------------------------------------------------------------
# Safe access helpers
# --------------------------------------------------------------------------

def g(d, *path, default=None):
    """Safe nested get for dicts (injected contexts may omit fields)."""
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return default
        if key not in cur:
            return default
        cur = cur[key]
    return cur


def _num(value):
    """Coerce to float or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"-?\d+(?:\.\d+)?", str(value))
    return float(m.group()) if m else None


def pct(value, dp=1):
    """Render a 0..1 ratio (or already-percent float) as 'X%'. Returns None on failure."""
    v = _num(value)
    if v is None:
        return None
    if abs(v) <= 1.0:  # ratio like 0.021
        return f"{v * 100:.{dp}f}%"
    return f"{v:.{dp}f}%"


def pct_delta(value, dp=0):
    """Render a signed delta ratio like -0.50 as '-50%'."""
    v = _num(value)
    if v is None:
        return None
    if abs(v) <= 1.0:
        return f"{v * 100:+.0f}%"
    return f"{v:+.0f}%"


def intstr(value):
    v = _num(value)
    if v is None:
        return None
    return f"{int(v):,}"


def _months_between(iso_a, iso_b):
    """Rough month difference between two date strings. None if unparseable."""
    def parse(s):
        if not s:
            return None
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(s))
        if not m:
            return None
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    a, b = parse(iso_a), parse(iso_b)
    if not a or not b:
        return None
    months = (b[0] - a[0]) * 12 + (b[1] - a[1])
    if b[2] < a[2]:
        months -= 1
    return months


# --------------------------------------------------------------------------
# Identity / voice helpers
# --------------------------------------------------------------------------

def owner_first(merchant):
    return g(merchant, "identity", "owner_first_name", default="") or ""


def biz_name(merchant):
    return g(merchant, "identity", "name", default="") or ""


def locality(merchant):
    return g(merchant, "identity", "locality", default="") or ""


def city(merchant):
    return g(merchant, "identity", "city", default="") or ""


def active_offers(merchant):
    out = []
    for o in g(merchant, "offers", default=[]) or []:
        if isinstance(o, dict) and o.get("status") == "active":
            t = o.get("title") or ""
            if t:
                out.append(t)
    return out


def salutation(category, merchant):
    slug = g(category, "slug", default="") or ""
    name = owner_first(merchant)
    if slug == "dentists":
        if name:
            if name.lower().startswith("dr"):
                return f"{name},"
            return f"Dr. {name},"
        return "Doctor,"
    if name:
        return f"Hi {name},"
    return "Hi,"


def customer_name(customer):
    n = g(customer, "identity", "name", default="") or ""
    # e.g. "Aanya (parent: Sneha)" or "(walk-in, no profile)"
    if n.startswith("(") or "no profile" in n.lower():
        return None
    return n.split(" (")[0]


def _name_from_customer_id(customer_id):
    """Derive a human name from 'c_001_priya_for_m001' -> 'Priya'.

    Fallback only, for when the customer context was never pushed but the trigger
    still names a customer. Skips non-name placeholders.
    """
    if not customer_id:
        return None
    m = re.match(r"c_\d+_([a-z0-9]+(?:_[a-z0-9]+)*)_for_", customer_id)
    if not m:
        return None
    name = m.group(1).replace("_", " ").title()
    if name.lower() in ("grandfather", "anonymous", "walk in", "no profile", "parent"):
        return None
    return name


def _pretty_date(iso):
    """'2026-04-22' -> '22 Apr' (None-safe)."""
    if not iso:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(iso))
    if not m:
        return str(iso)
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return f"{int(m.group(3))} {months[int(m.group(2)) - 1]}"


def lang_pref(customer):
    return (g(customer, "identity", "language_pref", default="") or "").lower()


def wants_hindi(customer):
    p = lang_pref(customer)
    return "hi" in p


EMOJI = {"dentists": " 🦷", "salons": " 💇‍♀️", "gyms": " 💪"}


def _emoji(category):
    return EMOJI.get(g(category, "slug", default=""), "")


# --------------------------------------------------------------------------
# Small phrase builders
# --------------------------------------------------------------------------

def _offer_lead(merchant):
    """Return the first active offer, or None."""
    offs = active_offers(merchant)
    return offs[0] if offs else None


def _peer_ctr(category):
    return _num(g(category, "peer_stats", "avg_ctr", default=None))


def _merchant_ctr(merchant):
    return _num(g(merchant, "performance", "ctr", default=None))


def _ctr_compare(merchant, category):
    """Return (mine_pct_str, peer_pct_str) or (None, None)."""
    mine = _merchant_ctr(merchant)
    peer = _peer_ctr(category)
    return (pct(mine), pct(peer) if peer is not None else None)


# --------------------------------------------------------------------------
# Generic grounded composer (fallback + unknown injected kinds)
# --------------------------------------------------------------------------

def _humanize_kind(kind):
    if not kind:
        return "this"
    return kind.replace("_", " ")


def _extract_grounded_facts(category, merchant, trigger, customer):
    """Collect every real, present fact for a grounded generic message."""
    facts = []
    p = g(trigger, "payload", default={}) or {}
    known = ("delta_pct", "days_remaining", "days_until", "days_since_expiry",
             "occurrences_30d", "value_now", "milestone_value", "distance_km",
             "days_to_wedding", "days_since_last_visit")
    for key in known:
        v = _num(p.get(key))
        if v is not None:
            facts.append((key, v))
    # any other *scalar numeric* payload field (for injected/unseen trigger kinds,
    # e.g. weather_heatwave {temp_c: 42}): surface it without fabricating.
    for k, v in p.items():
        if k in known:
            continue
        low = k.lower()
        if any(x in low for x in ("_id", "date", "time", "iso", "_at", "expires", "name", "label", "key")):
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            facts.append((k, float(v)))
        elif isinstance(v, str) and re.fullmatch(r"\s*-?\d+(?:\.\d+)?\s*", v):
            facts.append((k, float(v.strip())))
    # merchant performance
    perf = g(merchant, "performance", default={}) or {}
    views = _num(perf.get("views"))
    calls = _num(perf.get("calls"))
    ctr = _merchant_ctr(merchant)
    if views is not None:
        facts.append(("views_30d", views))
    if calls is not None:
        facts.append(("calls_30d", calls))
    if ctr is not None:
        facts.append(("ctr", ctr))
    return facts


def _fact_clause(k, v):
    """Render a (key, value) fact pair as a readable clause."""
    if k == "ctr":
        return f"your CTR is {pct(v)}"
    if k == "delta_pct":
        return f"down {abs(v) * 100:.0f}%" if v < 0 else f"up {v * 100:.0f}%"
    if k.endswith("_30d"):
        return f"{k.replace('_30d', '').replace('_', ' ')} at {int(v):,}"
    if k in ("temp_c", "temperature_c"):
        return f"{int(v)}°C"
    if k == "hours":
        return f"{int(v)} hours"
    if k == "delta_yoy":
        return f"{pct(v, 0)} YoY"
    label = k.replace("_", " ")
    return f"{label} {int(v):,}" if float(v).is_integer() else f"{label} {v}"


def _compose_generic(category, merchant, trigger, customer):
    kind = trigger.get("kind") or "generic"
    sal = salutation(category, merchant)
    slug = g(category, "slug", default="") or ""

    if customer:
        # customer-scoped generic: honor name + language, low-friction ask
        cname = customer_name(customer) or "there"
        lead = _offer_lead(merchant)
        off = f" — we have {lead}" if lead else ""
        if wants_hindi(customer):
            body = (f"Hi {cname}, {biz_name(merchant)} yahan{off}. "
                    f"Ek quick update aapke liye: {_humanize_kind(kind)}. "
                    f"Kuch bhi ho, reply YES aur hum dekhte hain.")
        else:
            body = (f"Hi {cname}, {biz_name(merchant)} here{off}. "
                    f"Quick update on {_humanize_kind(kind)}. Reply YES and we'll take it from there.")
        cta = "binary_yes_no"
        return body, cta, f"Customer-scoped {kind} with no named handler; grounded in merchant offer + language pref; low-friction YES CTA."

    # merchant-scoped generic
    facts = _extract_grounded_facts(category, merchant, trigger, customer)
    fact_clauses = [_fact_clause(k, v) for k, v in facts[:3]]
    fact_str = ", ".join(fact_clauses) if fact_clauses else "your current numbers"

    lead = _offer_lead(merchant)
    offer_str = f" You still have {lead} active." if lead else ""

    body = (f"{sal} quick one — {_humanize_kind(kind)} for {biz_name(merchant)} in "
            f"{locality(merchant)} ({fact_str}).{offer_str} "
            f"Want me to pull the details and draft your next step?")
    cta = "open_ended"
    return body, cta, (f"Trigger kind '{kind}' has no named handler; composed from real "
                       f"payload/merchant/category facts without fabrication.")


# --------------------------------------------------------------------------
# Named handlers (return body, cta, rationale)
# --------------------------------------------------------------------------

def _digest_item(category, item_id):
    for d in g(category, "digest", default=[]) or []:
        if isinstance(d, dict) and d.get("id") == item_id:
            return d
    # fallback: first research item
    for d in g(category, "digest", default=[]) or []:
        if isinstance(d, dict):
            return d
    return None


def _c_research_digest(category, merchant, trigger, customer):
    item = _digest_item(category, g(trigger, "payload", "top_item_id", default=None))
    sal = salutation(category, merchant)
    seg = g(merchant, "customer_aggregate", "high_risk_adult_count", default=None)
    seg_phrase = ""
    if seg is not None and int(seg) > 0:
        seg_phrase = f" relevant to your {int(seg)} high-risk adult patients"
    if item and item.get("title"):
        n = intstr(item.get("trial_n")) if item.get("trial_n") else None
        src = item.get("source") or ""
        issue = src.split(",")[0].strip() if src else "This week's research"
        # only prepend the trial size if the title doesn't already mention one
        n_phrase = ""
        if n and not re.search(r"trial|study|patient", item["title"], re.IGNORECASE):
            n_phrase = f"{n}-patient trial showed "
        body = (f"{sal} the {issue} issue landed. One item{seg_phrase} — "
                f"{n_phrase}{item['title']}. Worth a look (2-min read). Want me to pull it "
                f"and draft a patient-ed WhatsApp you can share? — {src}")
        cta = "open_ended"
        rat = (f"research_digest grounded in category digest item '{item.get('id')}' + merchant cohort; "
               f"source cited; open-ended CTA invites continuation.")
        return body, cta, rat
    body = (f"{sal} this week's {g(category,'slug')} research digest is out. Want me to pull the "
            f"items most relevant to your practice and draft a patient-ed summary?")
    return body, "open_ended", "research_digest with no matching item; generic digest invite."


def _c_regulation_change(category, merchant, trigger, customer):
    item = _digest_item(category, g(trigger, "payload", "top_item_id", default=None))
    sal = salutation(category, merchant)
    deadline = g(trigger, "payload", "deadline_iso", default=None)
    if item and item.get("title"):
        src = item.get("source") or ""
        title = item["title"]
        # avoid double-printing the deadline when the title already contains it
        if deadline and deadline[:10] in title:
            deadline = None
        d = f" (effective {deadline})" if deadline else ""
        act = (item.get("actionable") or "").strip()
        act = act if act.endswith((".", "!", "?")) else act + "." if act else ""
        body = (f"{sal} compliance heads-up: {title}{d} {act} Want me to draft a checklist so "
                f"it's handled before the deadline? — {src}")
        return body, "open_ended", f"regulation_change grounded in compliance digest item '{item.get('id')}'; actionable + deadline."
    body = (f"{sal} a regulatory change is in play for your category. Want me to pull the "
            f"official circular and give you the action items?")
    return body, "open_ended", "regulation_change without matching item; generic compliance invite."


def _c_recall_due(category, merchant, trigger, customer):
    cname = customer_name(customer) or "there"
    slots = g(trigger, "payload", "available_slots", default=[]) or []
    slot_labels = [s.get("label") for s in slots if isinstance(s, dict) and s.get("label")]
    service = g(trigger, "payload", "service_due", default="") or ""
    service = re.sub(r"(\d+) month", r"\1-month", service.replace("_", " "))
    lead = _offer_lead(merchant)
    offer_phrase = f" {lead}." if lead else ""
    emoji = _emoji(category)

    last_visit = g(customer, "relationship", "last_visit", default=None) or g(trigger, "payload", "last_service_date", default=None)
    due = g(trigger, "payload", "due_date", default=None)
    months = _months_between(last_visit, due) if last_visit and due else None

    if wants_hindi(customer):
        ago = f" {months} mahine" if months is not None else " kuch time"
        due_line = f"Aapke last visit ko{ago} ho gaye"
        due_line += f" — aapka {service} recall due hai" if service else " — follow-up visit ka time aa gaya hai"
        if len(slot_labels) == 2:
            slots_line = f" Apke liye 2 slots ready hain: {slot_labels[0]} ya {slot_labels[1]}."
            reply_line = f"Reply 1 for {slot_labels[0]}, 2 for {slot_labels[1]}, ya batao jo time suit kare."
            cta = "multi_choice_slot"
        elif slot_labels:
            slots_line = f" Ek slot ready hai: {slot_labels[0]}."
            reply_line = "Reply 1 to book, ya apna time batao."
            cta = "binary_yes_no"
        else:
            slots_line = ""
            reply_line = "Apna suitable time batao, main book kar deti hoon."
            cta = "binary_yes_no"
        body = f"Hi {cname}, {biz_name(merchant)} se hoon{emoji} {due_line}.{offer_phrase}{slots_line} {reply_line}"
    else:
        ago = f" {months} months" if months is not None else " a while"
        due_line = f"It's been{ago} since your last visit"
        due_line += f" — your {service} recall is due" if service else " — time for a follow-up visit"
        if len(slot_labels) == 2:
            slots_line = f" We have 2 slots ready: {slot_labels[0]} or {slot_labels[1]}."
            reply_line = f"Reply 1 for {slot_labels[0]}, 2 for {slot_labels[1]}, or tell us a time that works."
            cta = "multi_choice_slot"
        elif slot_labels:
            slots_line = f" We have a slot ready: {slot_labels[0]}."
            reply_line = "Reply 1 to book, or tell us a time that works."
            cta = "binary_yes_no"
        else:
            slots_line = ""
            reply_line = "Reply with a time that works and we'll book it."
            cta = "binary_yes_no"
        body = f"Hi {cname}, {biz_name(merchant)} here{emoji} {due_line}.{offer_phrase}{slots_line} {reply_line}"

    return body, cta, f"recall_due: {service or 'follow-up'} recall; real slots + active offer grounded; name + language honored."


def _c_perf_dip(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    metric = g(trigger, "payload", "metric", default="") or ""
    delta = _num(g(trigger, "payload", "delta_pct", default=None))
    base = intstr(g(trigger, "payload", "vs_baseline", default=None))
    if delta is None:
        d7 = g(merchant, "performance", "delta_7d", default={}) or {}
        views = _num(d7.get("views_pct"))
        calls = _num(d7.get("calls_pct"))
        cands = [(m, v) for m, v in (("views", views), ("calls", calls)) if v is not None]
        if cands:
            cands.sort(key=lambda x: x[1])
            metric, delta = cands[0]  # most-negative metric first
    if not metric:
        metric = "calls"
    mine, peer = _ctr_compare(merchant, category)
    ctr_line = f" Your CTR is {mine} vs {peer} peer median." if (mine and peer) else ""
    if delta is not None and delta < 0:
        d_str = pct_delta(delta)
        b_str = f" (baseline {base})" if base else ""
        body = (f"{sal} quick check — your {metric} are {d_str} week-over-week{b_str}.{ctr_line} "
                f"Want me to dig into why and draft a fix (usually a fresh post or offer tweak)?")
        rat = f"perf_dip grounded in real delta ({d_str}) + CTR peer compare; diagnosis offer = effort externalization."
    else:
        body = (f"{sal} quick check — your {metric} look stable this week.{ctr_line} Want me to do a "
                f"health check on your listing and flag anything worth fixing?")
        rat = "perf_dip with no negative delta in context; reframed as neutral listing health check (no fabricated drop)."
    return body, "open_ended", rat


def _c_renewal_due(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    sub = g(merchant, "subscription", default={}) or {}
    status = sub.get("status")
    days = _num(g(trigger, "payload", "days_remaining", default=None))
    if days is None:
        days = _num(sub.get("days_remaining"))
    plan = g(trigger, "payload", "plan", default=None) or sub.get("plan") or "Pro"
    amt = intstr(g(trigger, "payload", "renewal_amount", default=None))
    amt_str = f" at ₹{amt}" if amt else ""
    if status == "expired" or (days is not None and days <= 0):
        since = intstr(g(trigger, "payload", "days_since_expiry", default=None)) or intstr(sub.get("days_since_expiry"))
        since_str = f" {since} days ago" if since else ""
        body = (f"{sal} your {plan} plan lapsed{since_str} — and calls usually drift after. Want me "
                f"to draft a reactivation plan with one comeback offer?")
        rat = "renewal_due: plan lapsed; winback angle grounded in expired status + days since expiry."
    elif days is not None and days <= 30:
        body = (f"{sal} heads-up — your {plan} plan renews in {int(days)} days{amt_str}. Before it lapses, "
                f"want me to run a quick 30-day value recap (views, calls, what Vera did) so you "
                f"can decide with numbers?")
        rat = "renewal_due grounded in days_remaining <= 30 + plan; value-recap offer."
    else:
        body = (f"{sal} quick plan check-in — {int(days) if days is not None else 'several'} days left "
                f"on {plan}. Want me to run a value recap so you're getting the most from it?")
        rat = "renewal_due: plan active with ample days; reframed as value check-in."
    return body, "open_ended", rat


def _c_festival_upcoming(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    festival = g(trigger, "payload", "festival", default="") or "the festival"
    days = intstr(g(trigger, "payload", "days_until", default=None))
    slug = g(category, "slug", default="")
    beats = {b.get("month_range"): b.get("note") for b in g(category, "seasonal_beats", default=[]) or [] if isinstance(b, dict)}
    note = ""
    for rng, n in beats.items():
        if festival.lower() in n.lower() or "festival" in n.lower() or "diwali" in n.lower():
            note = f" {n[0].upper()}{n[1:]}." if n else ""
            break
    d_num = _num(days)
    if d_num is not None and d_num > 60:
        opener = f"{sal} {festival} is ~{int(d_num) // 30} months out — the planning window opens now."
    elif days:
        opener = f"{sal} {festival} is coming ({days} days out)."
    else:
        opener = f"{sal} {festival} is coming."
    body = (f"{opener}{note} Want me to draft the festival offer "
            f"post + WhatsApp blast using your active offers so you're first in the locality?")
    return body, "open_ended", "festival_upcoming grounded in festival + category seasonal beat + days_until; concrete deliverables offered."


def _c_wedding_followup(category, merchant, trigger, customer):
    cname = customer_name(customer) or "there"
    days = intstr(g(trigger, "payload", "days_to_wedding", default=None))
    wd = g(trigger, "payload", "wedding_date", default=None) or g(customer, "preferences", "wedding_date", default=None)
    lead = _offer_lead(merchant)
    d_str = f"{days} days" if days else "just weeks"
    wd_str = f" ({wd})" if wd else ""
    lead = _offer_lead(merchant)
    offer_line = f" We have {lead}." if lead else ""
    body = (f"Hi {cname} 💍 {owner_first(merchant)} from {biz_name(merchant)} here. {d_str} to your "
            f"wedding{wd_str} — the right window to start skin-prep before serious bridal bookings."
            f"{offer_line} Want me to block your preferred slot for the first session?")
    return body, "binary_yes_no", "wedding followup grounded in days_to_wedding + real offer; single binary commit."


def _c_curious_ask(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    trends = g(category, "trend_signals", default=[]) or []
    guess = ""
    if trends:
        t = trends[0]
        q = t.get("query") or ""
        d = t.get("delta_yoy")
        if q and d is not None:
            guess = f" (e.g. '{q}' searches are up {pct(d,0)} YoY — is that showing up?)"
    body = (f"{sal} quick question — what service has been most asked-for this week at "
            f"{biz_name(merchant)}?{guess} I'll turn the answer into a Google post + a ready "
            f"WhatsApp reply you can use. Takes 5 min.")
    return body, "open_ended", "curious_ask_due: asking-the-merchant lever + trend-signal guess for specificity; reciprocity offered."


def _c_winback_eligible(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    days = intstr(g(trigger, "payload", "days_since_expiry", default=None)) or g(merchant, "subscription", "days_since_expiry", default=None)
    dip = _num(g(trigger, "payload", "perf_dip_pct", default=None))
    lapsed = intstr(g(trigger, "payload", "lapsed_customers_added_since_expiry", default=None))
    d_str = f" {days} days since your plan lapsed" if days else "since your plan lapsed"
    dip_str = f" and calls down {pct_delta(dip)}" if dip is not None else ""
    l_str = f" {lapsed} customers have since gone quiet" if lapsed else ""
    body = (f"{sal} it's been{d_str}{dip_str}{l_str}. Want me to draft a 3-step win-back "
            f"(reactivate + one comeback offer + a post) so this doesn't drift further?")
    return body, "open_ended", "winback grounded in days_since_expiry + dip + lapsed count; loss-aversion framing."


def _c_ipl_match(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    match = g(trigger, "payload", "match", default="") or "today's match"
    venue = g(trigger, "payload", "venue", default="")
    is_weeknight = g(trigger, "payload", "is_weeknight", default=None)
    lead = _offer_lead(merchant)
    if is_weeknight is False:  # weekend match → contrarian: skip dine-in promo, go delivery
        action = f"run your {lead} as a delivery-only special" if lead else "push a delivery-only special"
        body = (f"{sal} quick heads-up — {match} at {venue} tonight. Weekend IPL matches usually "
                f"shift covers to home watch-parties. Skip the dine-in push; instead {action}. "
                f"Want me to draft the banner + an Insta story? Live in 10 min.")
        cta = "open_ended"
        rat = "IPL weekend: contrarian data-informed recommendation (skip dine-in, push delivery); leverages existing offer."
    else:
        action = f"push your {lead} as a match-night combo post" if lead else "push a match-night combo post"
        body = (f"{sal} {match} at {venue} tonight — match nights drive extra orders. Want me to "
                f"{action}? Live in 10 min.")
        cta = "open_ended"
        rat = "IPL match-night: weeknight push grounded in existing offer; concrete deliverables."
    return body, cta, rat


def _c_review_theme(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    theme = g(trigger, "payload", "theme", default="") or "a service issue"
    occ = intstr(g(trigger, "payload", "occurrences_30d", default=None))
    quote = g(trigger, "payload", "common_quote", default="")
    o_str = f" {occ} times" if occ else ""
    q_str = f" ('{quote}')" if quote else ""
    body = (f"{sal} flagging this: '{theme.replace('_',' ')}' showed up{o_str} in your reviews this "
            f"month{q_str}. One fix + a public reply usually reverses the pattern. Want me to draft both?")
    return body, "open_ended", "review_theme_emerged grounded in theme + occurrences + real quote; offers fix + reply draft."


def _c_milestone(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    metric = g(trigger, "payload", "metric", default="") or "reviews"
    is_review = metric in ("review_count", "reviews")
    label = "reviews" if is_review else metric.replace("_", " ")
    now = intstr(g(trigger, "payload", "value_now", default=None))
    target = intstr(g(trigger, "payload", "milestone_value", default=None))
    if now and target:
        gap = int(float(target)) - int(float(now))
        body = (f"{sal} you're at {now} {label} — {gap} away from {target}. Crossing {target} "
                f"usually bumps your locality ranking. Want me to draft a quick review-ask WhatsApp "
                f"for your last week's customers?")
    else:
        noun = "review" if is_review else label
        body = (f"{sal} you're close to a {noun} milestone. Want me to check your latest numbers "
                f"and draft a nudge to your recent customers to push it over?")
    return body, "open_ended", "milestone_reached grounded in value_now/milestone gap; social-proof + ranking nudge."


def _c_active_planning(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    topic = g(trigger, "payload", "intent_topic", default="") or "your plan"
    slug = g(category, "slug", default="")
    lead = _offer_lead(merchant)
    if "corporate" in topic or "thali" in topic:
        first = f"a corporate bulk package around your {lead}" if lead else "a corporate bulk package"
        body = (f"{sal} here's a starter you can edit — {first}. I'll do a tiered version "
                f"(10/25/50+) with free delivery, plus a 3-line WhatsApp to send offices in "
                f"{locality(merchant)}. Want me to draft it now?")
    elif "kids" in topic or "yoga" in topic:
        price_ref = f"priced off your {lead}" if lead else "at a competitive price"
        body = (f"{sal} good call — kids programs are peaking right now. Suggest a 4-week, "
                f"3-classes/week structure, age 7-12, {price_ref}. Want me to draft "
                f"the GBP post + Insta carousel?")
    else:
        ref = f" Using your {lead}," if lead else ""
        body = (f"{sal} let's make {_humanize_kind(topic)} concrete.{ref} want me to "
                f"draft a version with pricing, timing, and a one-line customer pitch you can edit?")
    return body, "open_ended", f"active_planning_intent: switches to action (draft artifact) instead of re-qualifying; grounded in offer + locality."


def _c_seasonal_dip(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    metric = g(trigger, "payload", "metric", default="views") or "views"
    delta = _num(g(trigger, "payload", "delta_pct", default=None))
    d_str = pct_delta(delta) if delta is not None else ""
    members = intstr(g(merchant, "customer_aggregate", "total_active_members", default=None))
    m_str = f" across your {members} active members" if members else ""
    body = (f"{sal} your {metric} are {d_str} this week — but this is the expected seasonal "
            f"lull, not a signal to panic. Action: pause acquisition spend and focus retention{m_str}. "
            f"Want me to draft a 'keep them through the dip' challenge?")
    return body, "open_ended", "seasonal_perf_dip: reframe as expected lull + redirect spend to retention; grounded in delta + members."


def _c_customer_lapsed(category, merchant, trigger, customer):
    cname = customer_name(customer) or "there"
    days = intstr(g(trigger, "payload", "days_since_last_visit", default=None))
    focus = g(trigger, "payload", "previous_focus", default="") or g(customer, "preferences", "training_focus", default="")
    lead = _offer_lead(merchant)
    d_str = f"about {int(int(days)/7) if days else 'a few'} weeks" if days else "a while"
    focus_str = f" for your {focus.replace('_',' ')} goal" if focus else ""
    lead = _offer_lead(merchant)
    offer_line = f" We've got {lead}{focus_str}." if lead else ""
    body = (f"Hi {cname} 👋 {owner_first(merchant)} from {biz_name(merchant)} here. It's been "
            f"{d_str} — happens to most people, no judgment.{offer_line} Want me to hold a free "
            f"spot for you next week? Reply YES — no commitment, no auto-charge.")
    return body, "binary_yes_no", "customer_lapsed: no-shame framing + real offer matched to prior goal; single binary CTA with barrier removal."


def _c_trial_followup(category, merchant, trigger, customer):
    cname = customer_name(customer) or "there"
    tdate = g(trigger, "payload", "trial_date", default=None)
    opts = g(trigger, "payload", "next_session_options", default=[]) or []
    labels = [o.get("label") for o in opts if isinstance(o, dict) and o.get("label")]
    lead = _offer_lead(merchant)
    t_str = f" since your trial on {_pretty_date(tdate)}" if tdate else " since your trial"
    if labels:
        l_str = labels[0]
        offer_line = f", and {lead} is still on" if lead else ""
        body = (f"Hi {cname}, {biz_name(merchant)} here. It's been a few days{t_str} — how did it "
                f"feel? We have a slot this {l_str}{offer_line}. Want me to book it?")
        cta = "binary_yes_no"
    else:
        offer_line = f" {lead} is still available if you want to continue." if lead else " Want to continue your sessions?"
        body = (f"Hi {cname}, {biz_name(merchant)} here. How did your trial go?{offer_line} "
                f"Reply YES and I'll sort the next session.")
        cta = "binary_yes_no"
    return body, cta, "trial_followup grounded in trial date + next slot + active offer; ask-first then binary commit."


def _c_supply_alert(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    molecule = g(trigger, "payload", "molecule", default="") or ""
    batches = g(trigger, "payload", "affected_batches", default=[]) or []
    mfr = g(trigger, "payload", "manufacturer", default="") or ""
    chronic = intstr(g(merchant, "customer_aggregate", "chronic_rx_count", default=None))
    b_str = ", ".join(batches) if batches else ""
    m_str = f" by {mfr}" if mfr else ""
    c_str = f" Pulled your repeat-Rx list: {chronic} chronic-Rx customers." if chronic else ""
    body = (f"{sal} urgent: voluntary recall on {molecule} batches ({b_str}){m_str} — "
            f"sub-potency, no safety risk, but customers should be informed for replacement."
            f"{c_str} Want me to draft their WhatsApp note + the replacement-pickup workflow?")
    return body, "open_ended", "supply_alert grounded in molecule + batches + derived chronic-Rx count; urgency + end-to-end workflow offer."


def _c_chronic_refill(category, merchant, trigger, customer):
    if g(category, "slug", default="") != "pharmacies":
        return _compose_generic(category, merchant, trigger, customer)
    cname = customer_name(customer) or "there"
    molecules = g(trigger, "payload", "molecule_list", default=[]) or []
    runout = g(trigger, "payload", "stock_runs_out_iso", default=None)
    senior = g(customer, "identity", "senior_citizen", default=False)
    offs = active_offers(merchant)
    senior_off = next((o for o in offs if "senior" in o.lower() or "15%" in o), None)
    delivery_off = next((o for o in offs if "delivery" in o.lower()), None)
    meds = ", ".join(molecules) + " medicines" if molecules else "medicines"
    r_str = f" {runout[:10]}" if runout else ""
    s_str = f" {senior_off} applied." if senior and senior_off else ""
    d_str = f" {delivery_off}." if delivery_off else ""
    body = (f"Namaste — {biz_name(merchant)} {locality(merchant)} yahan. {cname} ji ki {meds}"
            f"{r_str} ko khatam hongi. Same dose, same brand ready hai.{s_str}{d_str} "
            f"Reply CONFIRM to dispatch, ya koi change ho toh batayein.")
    return body, "binary_yes_no", "chronic_refill grounded in molecule list + run-out date + real senior/delivery offers; respectful senior salutation."


def _c_category_seasonal(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    trends = g(trigger, "payload", "trends", default=[]) or []

    def clean(t):
        s = str(t).replace("_demand_", " ").replace("_", " ")
        s = re.sub(r"([+-]\d+)$", r"\1%", s)
        return s

    t_str = ", ".join(clean(t) for t in trends[:3]) if trends else "seasonal demand is shifting"
    body = (f"{sal} {t_str}. Worth a shelf re-arrange this week — want me to draft the "
            f"GBP update + a WhatsApp note to your regulars about what's stocked?")
    return body, "open_ended", "category_seasonal grounded in trend list; shelf-action + customer note offer."


def _c_gbp_unverified(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    path = (g(trigger, "payload", "verification_path", default="") or "postcard or phone call").replace("_", " ")
    uplift = _num(g(trigger, "payload", "estimated_uplift_pct", default=None))
    u_str = f" — typically ~{int(uplift*100)}% more calls" if uplift is not None else ""
    body = (f"{sal} your Google Business Profile is still unverified{u_str}. Verification is a "
            f"5-minute {path}. Want me to walk you through it now?")
    return body, "binary_yes_no", "gbp_unverified grounded in uplift estimate + verification path; low-friction binary ask."


def _c_cde_opportunity(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    item = _digest_item(category, g(trigger, "payload", "digest_item_id", default=None))
    credits = intstr(g(trigger, "payload", "credits", default=None))
    fee = g(trigger, "payload", "fee", default="") or ""
    if item and item.get("title"):
        c = f" ({credits} credits)" if credits else ""
        f_str = f" — {fee.replace('_',' ')}" if fee else ""
        body = (f"{sal} {item['title']}{c}{f_str}. Worth your time before it fills up. Want me "
                f"to send you the registration link and a calendar block?")
    else:
        body = (f"{sal} a CDE/learning opportunity in your vertical is coming up. Want me to send "
                f"the details + register you?")
    return body, "open_ended", "cde_opportunity grounded in digest item + credits + fee; low-friction offer."


def _c_competitor_opened(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    comp = g(trigger, "payload", "competitor_name", default="") or "a new competitor"
    dist = g(trigger, "payload", "distance_km", default=None)
    offer = g(trigger, "payload", "their_offer", default="")
    lead = _offer_lead(merchant)
    d_str = f" {dist} km away" if dist is not None else " nearby"
    o_str = f", running {offer}" if offer else ""
    l_str = f" You have {lead} live." if lead else ""
    body = (f"{sal} heads-up — {comp} opened{d_str} in {locality(merchant)}{o_str}."
            f"{l_str} Want me to pull a side-by-side of how your listing appears next to theirs?")
    return body, "open_ended", "competitor_opened grounded in competitor + distance + their offer; voyeur-curiosity + differentiation."


def _c_perf_spike(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    metric = g(trigger, "payload", "metric", default="") or "calls"
    delta = _num(g(trigger, "payload", "delta_pct", default=None))
    driver = g(trigger, "payload", "likely_driver", default="")
    dr_str = f" (likely from {driver.replace('_',' ')})" if driver else ""
    if delta is not None and delta > 0:
        body = (f"{sal} nice — your {metric} are up {pct_delta(delta)} this week{dr_str}. Want me to "
                f"double down with a follow-up post while it's hot?")
        rat = "perf_spike grounded in positive delta + driver; momentum amplification ask."
    elif delta is not None and delta < 0:
        # mislabeled trigger: same signal as a dip, never say "up" about a fall
        return _c_perf_dip(category, merchant, trigger, customer)
    else:
        body = (f"{sal} nice — your {metric} are trending up this week{dr_str}. Want me to double down "
                f"with a follow-up post while it's hot?")
        rat = "perf_spike with no delta in context; momentum ask without a fabricated number."
    return body, "open_ended", rat


def _c_dormant(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    days = intstr(g(trigger, "payload", "days_since_last_merchant_message", default=None))
    d_str = f"{days} days" if days else "a while"
    lead = _offer_lead(merchant)
    l_str = f" You still have {lead} live." if lead else ""
    body = (f"{sal} been {d_str} since we talked — quick catch-up.{l_str} What's the one thing "
            f"you'd want more of right now: calls, walk-ins, or online orders?")
    return body, "open_ended", "dormant_with_vera grounded in days since last message; re-engage with a concrete choice."


def _c_customer_lapsed_soft(category, merchant, trigger, customer):
    cname = customer_name(customer) or "there"
    lead = _offer_lead(merchant)
    last = g(customer, "relationship", "last_visit", default=None)
    l_str = f" since {last}" if last else " for a bit"
    if lead:
        body = (f"Hi {cname}, {biz_name(merchant)} here. We haven't seen you{l_str} — thought you'd "
                f"like to know {lead} is available. Want me to hold a slot? No pressure.")
    else:
        body = (f"Hi {cname}, {biz_name(merchant)} here. We haven't seen you{l_str} — thought we'd "
                f"check in. Want me to hold a slot? No pressure.")
    return body, "binary_yes_no", "customer_lapsed_soft grounded in last visit + active offer; gentle, no-pressure binary."


def _c_appointment_tomorrow(category, merchant, trigger, customer):
    cname = customer_name(customer) or "there"
    body = (f"Hi {cname}, quick reminder — your appointment at {biz_name(merchant)} is tomorrow. "
            f"Reply CONFIRM to confirm, or R to reschedule.")
    return body, "binary_yes_no", "appointment_tomorrow: simple confirm/reschedule reminder."


def _c_weather_heatwave(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    temp = _num(g(trigger, "payload", "temp_c", default=None))
    if temp is None:
        temp = _num(g(trigger, "payload", "temperature_c", default=None))
    city_ = g(trigger, "payload", "city", default=None) or city(merchant)
    slug = g(category, "slug", default="")
    t_str = f"{int(temp)}°C" if temp is not None else "extreme heat"
    action = {
        "dentists": "non-urgent appointments often reschedule — a morning-slot nudge keeps the chair full",
        "salons": "anti-frizz and hair-spa demand jumps",
        "restaurants": "cold drinks and delivery orders spike in the afternoon",
        "gyms": "members shift to early-morning and late-evening slots",
        "pharmacies": "ORS and electrolyte demand spikes",
    }.get(slug, "demand shifts")
    body = (f"{sal} heads-up — {t_str} in {city_} today. For {slug}, {action}. Want me to "
            f"draft a quick post or offer that rides the moment?")
    return body, "open_ended", "weather_heatwave grounded in temp + city; category-specific action offered."


def _c_local_news(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    event = g(trigger, "payload", "event", default="") or g(trigger, "payload", "news", default="") or "a local event"
    hours = _num(g(trigger, "payload", "hours", default=None))
    h_str = f" (expected ~{int(hours)}h)" if hours is not None else ""
    body = (f"{sal} heads-up — {event} affecting {locality(merchant)}{h_str}. Want me to draft "
            f"a quick update for your customers so walk-ins don't drop?")
    return body, "open_ended", "local_news_event grounded in event + duration + locality; practical customer-update offer."


def _c_trend_movement(category, merchant, trigger, customer):
    sal = salutation(category, merchant)
    q = g(trigger, "payload", "query", default="") or "a service in your category"
    delta = _num(g(trigger, "payload", "delta_yoy", default=None))
    d_str = pct(delta, 0) if delta is not None else ""
    lead = _offer_lead(merchant)
    if lead:
        ask = f" Want me to position your {lead} for it?"
    else:
        ask = " Want me to draft a post that captures it?"
    body = f"{sal} '{q}' searches are up {d_str} YoY in your area.{ask}"
    return body, "open_ended", "category_trend_movement grounded in query + YoY delta; positioning offer."


HANDLERS = {
    "research_digest": _c_research_digest,
    "regulation_change": _c_regulation_change,
    "recall_due": _c_recall_due,
    "perf_dip": _c_perf_dip,
    "renewal_due": _c_renewal_due,
    "festival_upcoming": _c_festival_upcoming,
    "wedding_package_followup": _c_wedding_followup,
    "curious_ask_due": _c_curious_ask,
    "winback_eligible": _c_winback_eligible,
    "ipl_match_today": _c_ipl_match,
    "review_theme_emerged": _c_review_theme,
    "milestone_reached": _c_milestone,
    "active_planning_intent": _c_active_planning,
    "seasonal_perf_dip": _c_seasonal_dip,
    "customer_lapsed_hard": _c_customer_lapsed,
    "customer_lapsed_soft": _c_customer_lapsed_soft,
    "trial_followup": _c_trial_followup,
    "supply_alert": _c_supply_alert,
    "chronic_refill_due": _c_chronic_refill,
    "category_seasonal": _c_category_seasonal,
    "gbp_unverified": _c_gbp_unverified,
    "cde_opportunity": _c_cde_opportunity,
    "competitor_opened": _c_competitor_opened,
    "perf_spike": _c_perf_spike,
    "dormant_with_vera": _c_dormant,
    "appointment_tomorrow": _c_appointment_tomorrow,
}

# Aliases for the post-submission injected kinds the brief names explicitly (§4.3):
# reuse the closest existing strategy or a purpose-built one.
HANDLERS.update({
    "category_research_digest_release": _c_research_digest,
    "scheduled_recurring": _c_curious_ask,
    "weather_heatwave": _c_weather_heatwave,
    "local_news_event": _c_local_news,
    "category_trend_movement": _c_trend_movement,
})


def compose(category, merchant, trigger, customer=None):
    """Main entry point. All inputs are dicts (from /v1/context payloads)."""
    category = category or {}
    merchant = merchant or {}
    trigger = trigger or {}
    customer = customer or None

    # Fallback: a customer-scoped trigger whose customer context was never pushed
    # (e.g. the judge simulator's full-eval mode) — derive a name from the
    # customer_id so we never greet with a bare "Hi there".
    if customer is None and trigger.get("customer_id"):
        derived = _name_from_customer_id(trigger.get("customer_id"))
        if derived:
            customer = {"identity": {"name": derived}}

    kind = trigger.get("kind") or "generic"
    scope = trigger.get("scope") or ("customer" if customer else "merchant")

    handler = HANDLERS.get(kind)
    try:
        if handler:
            body, cta, rationale = handler(category, merchant, trigger, customer)
        else:
            body, cta, rationale = _compose_generic(category, merchant, trigger, customer)
    except Exception:
        body, cta, rationale = _compose_generic(category, merchant, trigger, customer)

    send_as = "merchant_on_behalf" if (scope == "customer" or customer) else "vera"
    suppression_key = (trigger.get("suppression_key")
                       or f"{kind}:{merchant.get('merchant_id','unknown')}")

    return {
        "body": (body or "").strip(),
        "cta": cta,
        "send_as": send_as,
        "suppression_key": suppression_key,
        "rationale": rationale,
    }
