from __future__ import annotations
import csv, hashlib, io
from pathlib import Path
from .schemas import KpiSnapshot, Totals, MeasurementPlan, StrategyBrief

REQUIRED = {"date", "platform", "impressions", "reach", "likes", "comments",
            "shares", "saves", "clicks", "conversions", "spend_usd", "follower_delta"}
CMP = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
       ">": lambda a, b: a > b, ">=": lambda a, b: a >= b}

def compute_kpis(path: str | Path) -> KpiSnapshot:
    """Only real rows produce numbers. Missing file => measured=False, never zeros-as-fact."""
    p = Path(path)
    if not p.is_file():
        return KpiSnapshot(measured=False, source=str(p), sha256="", row_count=0,
                           warnings=[f"NO DATA: {p} not found. All baselines unknown."])
    raw = p.read_bytes()
    # io.StringIO, not splitlines(): quoted fields may contain newlines.
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
    if not rows:
        return KpiSnapshot(measured=False, source=str(p), sha256=hashlib.sha256(raw).hexdigest(),
                           row_count=0, warnings=["Export contained no rows."])
    missing = REQUIRED - set(rows[0])
    if missing:
        raise ValueError(f"metrics export missing columns: {sorted(missing)}")

    warnings: list[str] = []
    acct, per = Totals(), {}
    seen, dates = set(), []
    for i, r in enumerate(rows, start=2):
        plat = (r["platform"] or "").strip()
        key = (r["date"].strip(), plat, (r.get("post_id") or "").strip())
        if key in seen:
            warnings.append(f"line {i}: duplicate row skipped {key}")
            continue
        seen.add(key)
        dates.append(key[0])

        def num(col: str) -> float:
            try:
                v = float(r[col] or 0)
            except ValueError:
                warnings.append(f"line {i}: non-numeric {col}={r[col]!r}, treated as 0")
                return 0.0
            # follower_delta is legitimately negative (unfollows); everything else is a count.
            if v < 0 and col != "follower_delta":
                warnings.append(f"line {i}: negative {col}={v}, clamped to 0")
                return 0.0
            return v

        # Parse each cell once so warnings aren't duplicated per target.
        vals = {c: num(c) for c in ("impressions", "reach", "likes", "comments", "shares", "saves",
                                    "clicks", "conversions", "spend_usd", "follower_delta")}
        eng = vals["likes"] + vals["comments"] + vals["shares"] + vals["saves"]
        t = per.setdefault(plat, Totals())
        for target in (acct, t):
            target.posts += 1
            target.impressions += vals["impressions"]
            target.reach += vals["reach"]
            target.engagements += eng
            target.clicks += vals["clicks"]
            target.conversions += vals["conversions"]
            target.spend_usd += vals["spend_usd"]
            target.follower_delta += vals["follower_delta"]
    if acct.reach > acct.impressions:
        warnings.append("reach exceeds impressions — check export definitions")
    return KpiSnapshot(measured=True, source=str(p), sha256=hashlib.sha256(raw).hexdigest(),
                       row_count=len(seen), period_start=min(dates), period_end=max(dates),
                       account=acct, by_platform=per, warnings=warnings)

def evaluate_rules(plan: MeasurementPlan, snap: KpiSnapshot) -> list[dict]:
    out = []
    for r in plan.decision_rules:
        observed = snap.value(r.metric, r.scope, r.platform)
        sample = snap.value("impressions", r.scope, r.platform) or 0
        if observed is None:
            fired, reason = False, "no data"
        elif sample < r.min_impressions:
            fired, reason = False, f"insufficient sample ({sample:.0f} < {r.min_impressions})"
        else:
            fired, reason = CMP[r.comparator](observed, r.threshold), "evaluated"
        out.append({"rule_id": r.id, "metric": r.metric, "scope": r.scope, "platform": r.platform,
                    "observed": observed, "threshold": r.threshold, "comparator": r.comparator,
                    "fired": fired, "severity": r.severity, "action": r.action, "reason": reason})
    return out

def goal_progress(brief: StrategyBrief, snap: KpiSnapshot) -> list[dict]:
    out = []
    for g in brief.goals:
        v = snap.value(g.metric, "account", None)
        out.append({"goal_id": g.id, "metric": g.metric, "target": g.target_value, "observed": v,
                    "pct": round(100 * v / g.target_value, 1) if v is not None else None})
    return out

def build_directives(results: list[dict], progress: list[dict], snap: KpiSnapshot) -> str:
    if not snap.measured:
        return ("NO PRIOR DATA. Do not reference past performance. Treat every baseline as unknown "
                "and prioritise instrumentation in the measurement plan.")
    lines = [f"Prior period {snap.period_start}..{snap.period_end} ({snap.row_count} rows)."]
    fired = [r for r in results if r["fired"]]
    if fired:
        lines.append("BINDING directives from pre-committed rules (do not relitigate these):")
        lines += [f"- [{r['severity']}] {r['rule_id']}: {r['metric']}={r['observed']:.4g} "
                  f"{r['comparator']} {r['threshold']:.4g} -> {r['action']}" for r in fired]
    else:
        lines.append("No rule fired. Hold strategy; run the planned experiments.")
    lines.append("Goal progress: " + ", ".join(
        f"{p['goal_id']} {p['pct']}%" if p["pct"] is not None else f"{p['goal_id']} unknown"
        for p in progress))
    if snap.warnings:
        lines.append("Data caveats: " + "; ".join(snap.warnings[:5]))
    return "\n".join(lines)
