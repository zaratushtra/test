#!/usr/bin/env python3
"""
Phase 0(a) — Polymarket market screen and independence survey.

Answers the gate question from SPINE §3.1: can the T1 universe supply enough
*independent* markets per week to reach the n_eff target on an acceptable timeline?

Raw market count is not the answer. Correlated markets contribute almost nothing:
    n_eff = n / (1 + (n - 1) * r_bar)
Fifteen markets about one election are one observation.

Stdlib only — no installs needed on the target host.

UNVERIFIED: the Gamma API field names below were written without network access to
the live API. Run with --dump-schema first; if extraction reports nulls, fix the
candidate key lists in FIELD_CANDIDATES.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta

GAMMA_BASE = "https://gamma-api.polymarket.com"

# UNVERIFIED and time-sensitive: venue geoblock tiers change. Confirmed 19 Sep 2026
# that the UK is close-only on BOTH frontend and API. Ireland/Netherlands were
# frontend-only at that date. Re-check before each run; this list is a reminder,
# not an authority.
CLOSE_ONLY_JURISDICTIONS = {"GB", "UK"}
USER_AGENT = "spine-phase0-screen/0.1"

# Tolerant extraction: Gamma has changed field names historically and we could not
# test against it. First match wins.
FIELD_CANDIDATES = {
    "id": ["id", "conditionId", "condition_id"],
    "question": ["question", "title", "slug"],
    "end_date": ["endDate", "end_date_iso", "endDateIso", "end_date"],
    "liquidity": ["liquidityNum", "liquidity", "liquidityClob"],
    "volume": ["volumeNum", "volume", "volume24hr"],
    "closed": ["closed"],
    "active": ["active"],
    "event_id": ["eventId", "event_id"],
    # The rules govern settlement, not the title. Without this the screen's
    # output cannot be registered as a contract at all (spine/registry.py).
    "rules_text": ["description", "resolutionCriteria", "rules"],
    "outcome_token_id": ["clobTokenIds", "clob_token_ids", "tokenId"],
}

STOPWORDS = {
    "will", "the", "a", "an", "be", "by", "in", "on", "at", "of", "to", "for",
    "and", "or", "is", "are", "was", "were", "before", "after", "any", "have",
    "has", "there", "this", "that", "than", "then", "if", "it", "its", "as",
    "who", "what", "when", "which", "does", "do", "did", "how", "many", "more",
}


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------

def _get(url: str, timeout: int = 30) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_markets(limit: int, page_size: int = 500, verbose: bool = True) -> list[dict]:
    """Page through active, unclosed markets."""
    out: list[dict] = []
    offset = 0
    while len(out) < limit:
        want = min(page_size, limit - len(out))
        qs = urllib.parse.urlencode(
            {"limit": want, "offset": offset, "active": "true", "closed": "false"}
        )
        url = f"{GAMMA_BASE}/markets?{qs}"
        try:
            batch = _get(url)
        except urllib.error.HTTPError as e:
            print(f"  HTTP {e.code} at offset {offset}: {e.reason}", file=sys.stderr)
            break
        except Exception as e:  # noqa: BLE001 - surface anything network-shaped and stop
            print(f"  fetch failed at offset {offset}: {e}", file=sys.stderr)
            break
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        offset += len(batch)
        if verbose:
            print(f"  fetched {len(out)}", file=sys.stderr)
        if len(batch) < want:
            break
    return out


def pick(raw: dict, key: str):
    for cand in FIELD_CANDIDATES[key]:
        if cand in raw and raw[cand] not in (None, ""):
            return raw[cand]
    return None


# --------------------------------------------------------------------------
# normalise
# --------------------------------------------------------------------------

@dataclass
class Market:
    id: str
    question: str
    end_date: str | None
    horizon_days: float | None
    liquidity: float
    volume: float
    event_id: str | None
    # Empty means the venue exposed no settlement text. Registration refuses such
    # a market rather than inventing a rule for it.
    rules_text: str = ""
    outcome_token_id: str = ""
    tags: list[str] = field(default_factory=list)
    zone: str = "excluded"
    cluster_id: str = ""


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _parse_dt(s) -> datetime | None:
    if not s:
        return None
    txt = str(s).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def extract_tags(raw: dict) -> list[str]:
    tags: list[str] = []
    for ev in raw.get("events") or []:
        if isinstance(ev, dict):
            for t in ev.get("tags") or []:
                label = t.get("label") or t.get("slug") if isinstance(t, dict) else t
                if label:
                    tags.append(str(label).lower())
    for t in raw.get("tags") or []:
        label = t.get("label") or t.get("slug") if isinstance(t, dict) else t
        if label:
            tags.append(str(label).lower())
    return sorted(set(tags))


def extract_event_id(raw: dict) -> str | None:
    direct = pick(raw, "event_id")
    if direct:
        return str(direct)
    events = raw.get("events") or []
    if events and isinstance(events[0], dict) and events[0].get("id"):
        return str(events[0]["id"])
    return None


def extract_token_id(raw: dict) -> str:
    """
    The YES outcome token. Gamma returns `clobTokenIds` as a JSON *string* of a
    two-element list, ordered [YES, NO]; take the first. A market with no token
    is not tradeable and the registry will fall back to a synthetic id.
    """
    v = pick(raw, "outcome_token_id")
    if v is None:
        return ""
    if isinstance(v, str) and v.strip().startswith("["):
        try:
            v = json.loads(v)
        except json.JSONDecodeError:
            return v.strip()
    if isinstance(v, (list, tuple)):
        return str(v[0]) if v else ""
    return str(v).strip()


def classify(horizon_days: float | None, liquidity: float) -> str:
    """
    Zones per SPINE v2 tiering: T1 7-14d, T2 1-6mo, T3 structural.

    This used to return a second value, `firewall_cap` — the per-horizon ceiling
    on the *probability* that v2 §5.1 removed. Nothing consumed it and the
    concept no longer exists, so it is gone rather than renamed. The discipline
    it was reaching for now lives in `min_width_bp` on the forecast, which is a
    floor on interval width and a separate, still-undeclared decision.
    """
    if horizon_days is None or horizon_days <= 0:
        return "excluded"
    if horizon_days <= 14 and liquidity >= 5_000:
        return "T1"
    if 30 <= horizon_days <= 183 and liquidity >= 5_000:
        return "T2"
    if horizon_days >= 730 and liquidity >= 10_000:
        return "T3_structural"
    return "excluded"


def normalise(raw_markets: list[dict], now: datetime) -> list[Market]:
    out = []
    for raw in raw_markets:
        if not isinstance(raw, dict):
            continue
        end_raw = pick(raw, "end_date")
        end_dt = _parse_dt(end_raw)
        horizon = (end_dt - now).total_seconds() / 86400.0 if end_dt else None
        liq = _to_float(pick(raw, "liquidity"))
        zone = classify(horizon, liq)
        out.append(
            Market(
                id=str(pick(raw, "id") or ""),
                question=str(pick(raw, "question") or ""),
                end_date=str(end_raw) if end_raw else None,
                horizon_days=round(horizon, 2) if horizon is not None else None,
                liquidity=liq,
                volume=_to_float(pick(raw, "volume")),
                event_id=extract_event_id(raw),
                rules_text=str(pick(raw, "rules_text") or "").strip(),
                outcome_token_id=extract_token_id(raw),
                tags=extract_tags(raw),
                zone=zone,
            )
        )
    return out


# --------------------------------------------------------------------------
# correlation clustering
# --------------------------------------------------------------------------

def tokens(question: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", question.lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 2}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cluster(markets: list[Market], text_threshold: float = 0.45,
            tag_threshold: float = 0.70) -> dict[str, list[Market]]:
    """
    Union-find over three correlation proxies, strongest first.

    True outcome correlation needs resolved history we do not have at Phase 0.
    These are deliberately conservative proxies — they over-merge rather than
    under-merge, because over-merging understates n_eff and understating is the
    safe direction for a go/no-go gate.
    """
    parent: dict[int, int] = {i: i for i in range(len(markets))}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # 1. Same Polymarket event -> near-certain correlation
    by_event: dict[str, list[int]] = defaultdict(list)
    for i, m in enumerate(markets):
        if m.event_id:
            by_event[m.event_id].append(i)
    for idxs in by_event.values():
        for j in idxs[1:]:
            union(idxs[0], j)

    # 2/3. Tag overlap and question-token overlap
    toks = [tokens(m.question) for m in markets]
    tagsets = [set(m.tags) for m in markets]
    for i in range(len(markets)):
        for j in range(i + 1, len(markets)):
            if find(i) == find(j):
                continue
            if tagsets[i] and jaccard(tagsets[i], tagsets[j]) >= tag_threshold:
                union(i, j)
            elif jaccard(toks[i], toks[j]) >= text_threshold:
                union(i, j)

    groups: dict[str, list[Market]] = defaultdict(list)
    for i, m in enumerate(markets):
        root = find(i)
        cid = f"c{root}"
        m.cluster_id = cid
        groups[cid].append(m)
    return dict(groups)


# --------------------------------------------------------------------------
# n_eff and basket selection
# --------------------------------------------------------------------------

def n_eff(n: int, r_bar: float) -> float:
    if n <= 0:
        return 0.0
    return n / (1 + (n - 1) * r_bar)


def select_independent_basket(groups: dict[str, list[Market]], size: int) -> list[Market]:
    """
    Greedy max-independence: take at most one market per cluster, richest
    liquidity first, before taking any second member of a cluster.
    """
    ranked = {
        cid: sorted(ms, key=lambda m: m.liquidity, reverse=True)
        for cid, ms in groups.items()
    }
    order = sorted(ranked, key=lambda c: ranked[c][0].liquidity, reverse=True)
    basket: list[Market] = []
    depth = 0
    while len(basket) < size:
        added = False
        for cid in order:
            if depth < len(ranked[cid]):
                basket.append(ranked[cid][depth])
                added = True
                if len(basket) >= size:
                    break
        if not added:
            break
        depth += 1
    return basket


def basket_r_bar(basket: list[Market], within_cluster_r: float = 0.80,
                 cross_cluster_r: float = 0.05) -> float:
    """Mean pairwise correlation over the basket, from cluster co-membership."""
    n = len(basket)
    if n < 2:
        return 0.0
    same = diff = 0
    for i in range(n):
        for j in range(i + 1, n):
            if basket[i].cluster_id == basket[j].cluster_id:
                same += 1
            else:
                diff += 1
    total = same + diff
    return (same * within_cluster_r + diff * cross_cluster_r) / total


# Corrected after the 19 Sep 2026 review. The original 24.7 was derived with a
# one-sided alpha=0.05 (z=1.645) but the gate uses the 2.5th bootstrap percentile,
# which is one-sided 2.5% (z=1.96). Consistent with the stated gate:
#   (1.96 + 0.8416)^2 * 4 = 31.4
# Every projection moves out ~27%. This remains an approximation, not a bound: it
# substitutes benchmark variance for the model-conditional term and drops Var(d^2).
POWER_COEFFICIENT = 31.4


def required_n_eff(bss: float) -> float:
    """Approximate n_eff for 80% power against a 95% CI lower bound. See above."""
    return POWER_COEFFICIENT / bss if bss > 0 else float("inf")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="SPINE Phase 0(a) market screen")
    ap.add_argument("--limit", type=int, default=3000, help="max markets to fetch")
    ap.add_argument("--weekly-size", type=int, default=15, help="target markets per week")
    ap.add_argument("--target-bss", type=float, default=0.10,
                    help="effect size the Register A gate must detect")
    ap.add_argument("--jurisdiction", default="unknown",
                    help="ISO-3166 code of the operating jurisdiction; gates trading")
    ap.add_argument("--outdir", default="out")
    ap.add_argument("--dump-schema", action="store_true",
                    help="print the keys of the first raw market and exit")
    ap.add_argument("--input-json", help="use a saved raw dump instead of fetching")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)

    # Gate zero. Polymarket is close-only on BOTH frontend and API for some
    # jurisdictions (the UK among them: no new positions, no Gambling Commission
    # licence). Where that applies, no amount of forecasting skill produces a
    # tradeable T1/T2 system, and the screen below is research-only. This is a
    # ten-minute check that can moot months of work, so it runs first and loudly.
    print(f"\n{'#'*66}\n# GATE ZERO — TRADING ELIGIBILITY\n{'#'*66}")
    if args.jurisdiction == "unknown":
        print("Operating jurisdiction: NOT DECLARED.\n"
              "  Pass --jurisdiction <ISO-3166 code> once confirmed. Until then the\n"
              "  screen reports the research universe only; no result here implies a\n"
              "  tradeable system. Verify against the venue's own geoblock policy —\n"
              "  frontend and API tiers differ, and 'close-only' means exactly that.")
    elif args.jurisdiction.upper() in CLOSE_ONLY_JURISDICTIONS:
        print(f"Operating jurisdiction: {args.jurisdiction.upper()} — CLOSE-ONLY.\n"
              "  New positions cannot be opened. T1/T2 trading is not available.\n"
              "  The screen continues in RESEARCH-ONLY mode; Phase 5/6 do not apply.")
    else:
        print(f"Operating jurisdiction: {args.jurisdiction.upper()} — not on the "
              "close-only list used here.\n"
              "  UNVERIFIED: confirm against the venue's live geoblock policy before "
              "relying on this.")

    if args.input_json:
        with open(args.input_json, encoding="utf-8") as fh:
            raw = json.load(fh)
    else:
        print("Fetching markets from Gamma API...", file=sys.stderr)
        raw = fetch_markets(args.limit)

    if not raw:
        print("FAIL: no markets retrieved. Check network reachability to "
              f"{GAMMA_BASE} and rerun.", file=sys.stderr)
        return 2

    if args.dump_schema:
        print(json.dumps(sorted(raw[0].keys()), indent=2))
        print("\n--- sample record ---")
        print(json.dumps(raw[0], indent=2)[:3000])
        return 0

    markets = normalise(raw, now)
    extracted = sum(1 for m in markets if m.end_date and m.liquidity > 0)
    if extracted < len(markets) * 0.5:
        print(f"WARNING: only {extracted}/{len(markets)} markets yielded both an end "
              "date and liquidity. FIELD_CANDIDATES is probably stale — rerun with "
              "--dump-schema and fix the key lists.", file=sys.stderr)

    by_zone: dict[str, list[Market]] = defaultdict(list)
    for m in markets:
        by_zone[m.zone].append(m)

    print(f"\n{'='*66}\nSPINE PHASE 0(a) — MARKET SCREEN\n{'='*66}")
    print(f"as of {now.isoformat()}   raw markets: {len(raw)}\n")
    print(f"{'zone':<16}{'count':>8}{'median liq':>14}{'median horizon d':>20}")
    for zone in ("T1", "T2", "T3_structural", "excluded"):
        ms = by_zone.get(zone, [])
        if not ms:
            print(f"{zone:<16}{0:>8}")
            continue
        liqs = sorted(m.liquidity for m in ms)
        hors = sorted(m.horizon_days for m in ms if m.horizon_days is not None)
        med_l = liqs[len(liqs) // 2] if liqs else 0
        med_h = hors[len(hors) // 2] if hors else 0
        print(f"{zone:<16}{len(ms):>8}{med_l:>14,.0f}{med_h:>20.1f}")

    # The gate turns on T1 independence.
    t1 = by_zone.get("T1", [])
    print(f"\n{'-'*66}\nT1 INDEPENDENCE SURVEY  (the gate)\n{'-'*66}")
    if len(t1) < 2:
        print("Insufficient T1 markets to assess independence.")
        return 1

    groups = cluster(t1)
    sizes = sorted((len(v) for v in groups.values()), reverse=True)
    print(f"T1 markets: {len(t1)}   correlation clusters: {len(groups)}")
    print(f"largest clusters: {sizes[:8]}")

    # n_eff is NOT monotonic in basket size. Past the number of independent
    # clusters you are adding correlated duplicates, which raise r_bar faster
    # than n rises — so evidence per week *falls* while workload climbs. Sweep
    # to find the turn rather than assuming a basket size.
    need = required_n_eff(args.target_bss)
    sweep = []
    for size in range(2, min(len(t1), 60) + 1):
        b = select_independent_basket(groups, size)
        rb = basket_r_bar(b)
        ne = n_eff(len(b), rb)
        sweep.append({"size": len(b), "r_bar": round(rb, 4),
                      "n_eff_per_week": round(ne, 3),
                      "weeks": round(need / ne, 1) if ne > 0 else None})
    best = max(sweep, key=lambda s: s["n_eff_per_week"]) if sweep else None

    if best:
        print(f"\nbasket-size sweep (n_eff peaks where correlated duplicates start):")
        print(f"  {'size':>6}{'r_bar':>10}{'n_eff/wk':>11}{'weeks':>9}")
        shown = {best["size"], args.weekly_size, 2, len(t1)}
        for s in sweep:
            if s["size"] in shown or s["size"] % 5 == 0:
                mark = "  <- optimum" if s["size"] == best["size"] else ""
                print(f"  {s['size']:>6}{s['r_bar']:>10.3f}"
                      f"{s['n_eff_per_week']:>11.2f}{s['weeks']:>9.0f}{mark}")
        print(f"\n  OPTIMAL weekly basket: {best['size']} markets "
              f"({best['n_eff_per_week']:.2f} n_eff/wk, {best['weeks']:.0f} weeks)")
        if best["size"] < args.weekly_size:
            waste = args.weekly_size - best["size"]
            print(f"  Taking {args.weekly_size} would add {waste} correlated markets, "
                  f"raising r_bar and\n  LOWERING evidence per week. More work, less signal.")

    basket = select_independent_basket(groups, args.weekly_size)
    r_bar = basket_r_bar(basket)
    weekly_neff = n_eff(len(basket), r_bar)
    weeks = need / weekly_neff if weekly_neff > 0 else float("inf")

    print(f"\nat requested size:")
    print(f"weekly basket size      : {len(basket)}")
    print(f"implied r_bar           : {r_bar:.3f}")
    print(f"n_eff per week          : {weekly_neff:.2f}")
    print(f"n_eff required @BSS={args.target_bss:.2f}: {need:.0f}")
    print(f"projected Register A    : {weeks:.0f} weeks  (~{weeks/4.33:.1f} months)")

    verdict = "PASS" if weeks <= 78 else "FAIL"
    print(f"\nGATE: {verdict}  (threshold: Register A reachable within 18 months)")
    if verdict == "FAIL":
        print("  The T1 universe is too correlated or too thin. Options: widen domain\n"
              "  coverage, lower the claimed effect size, or stop.")

    import os
    os.makedirs(args.outdir, exist_ok=True)
    csv_path = os.path.join(args.outdir, "market_screen.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(asdict(markets[0]).keys()))
        w.writeheader()
        for m in markets:
            row = asdict(m)
            row["tags"] = "|".join(row["tags"])
            w.writerow(row)

    summary = {
        "as_of": now.isoformat(),
        "raw_markets": len(raw),
        "zone_counts": {z: len(v) for z, v in by_zone.items()},
        "t1_markets": len(t1),
        "t1_clusters": len(groups),
        "weekly_basket_size": len(basket),
        "r_bar": round(r_bar, 4),
        "n_eff_per_week": round(weekly_neff, 3),
        "target_bss": args.target_bss,
        "n_eff_required": round(need, 1),
        "projected_weeks": round(weeks, 1),
        "gate": verdict,
        "jurisdiction": args.jurisdiction,
        "trading_available": args.jurisdiction.upper() not in CLOSE_ONLY_JURISDICTIONS
                             and args.jurisdiction != "unknown",
        "power_coefficient": POWER_COEFFICIENT,
        "field_extraction_ok": extracted >= len(markets) * 0.5,
        "optimal_basket": best,
        "basket_sweep": sweep,
    }
    json_path = os.path.join(args.outdir, "market_screen_summary.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    print(f"\nwrote {csv_path}\nwrote {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
