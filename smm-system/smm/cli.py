from __future__ import annotations
import argparse, json, tempfile
from datetime import date, timedelta
from pathlib import Path
from .config import SETTINGS
from .schemas import StrategyBrief, ContentCalendar, MeasurementPlan, Post
from .store import Store
from .loop import compute_kpis, evaluate_rules, goal_progress, build_directives
from .publish import publish_run

def monday(d: str | None) -> str:
    d0 = date.fromisoformat(d) if d else date.today()
    return (d0 - timedelta(days=d0.weekday())).isoformat()

def cmd_plan(a, store):
    from .crew import plan_week  # lazy: non-LLM commands work without crewai installed
    client = json.loads(Path(a.client).read_text())
    rid, _ = plan_week(store, client, monday(a.week_of))
    print(f"run_id={rid}  posts pending review")
    cmd_review(argparse.Namespace(run_id=rid), store)

def cmd_review(a, store):
    for r in store.posts(a.run_id):
        p = Post.model_validate_json(r["payload_json"])
        print(f"{p.id}  {r['scheduled_at']}  {p.platform:9s} {p.format:11s} "
              f"{r['status']:15s} {p.caption[:60]!r}")
        print(f"      approve: smm approve --run-id {a.run_id} --post {p.id} --approver YOU")

def cmd_approve(a, store):
    if not a.all and not a.post:
        raise SystemExit("pass --post P01 or --all")
    targets = store.posts(a.run_id) if a.all else [store.post(f"{a.run_id}:{a.post}")]
    if not targets or not all(targets):
        raise SystemExit("post not found")
    for row in targets:
        h = store.decide(row["post_uid"], a.approver, "rejected" if a.reject else "approved", a.note)
        print(f"{row['post_id']} {'rejected' if a.reject else 'approved'} hash={h[:12]}")

def cmd_publish(a, store):
    for r in publish_run(store, a.run_id, live=a.live, only=a.post, force=a.force):
        print(json.dumps(r, ensure_ascii=False))

def cmd_report(a, store):
    run = store.latest_run("plan")
    if not run: raise SystemExit("no plan run yet")
    rid = run["run_id"]
    brief = StrategyBrief.model_validate(store.artifact(rid, "strategy"))
    plan = MeasurementPlan.model_validate(store.artifact(rid, "measurement"))
    snap = compute_kpis(a.metrics)
    results, progress = evaluate_rules(plan, snap), goal_progress(brief, snap)
    for r in results: store.record_decision(rid, r["rule_id"], r["fired"], r["observed"], r["action"])
    store.record_snapshot(rid, snap)
    directives = build_directives(results, progress, snap)
    report = {"run_id": rid, "snapshot": snap.model_dump(), "rules": results,
              "goals": progress, "directives": directives}
    SETTINGS.out_dir.mkdir(parents=True, exist_ok=True)
    (SETTINGS.out_dir / f"report-{rid}.json").write_text(json.dumps(report, indent=2))
    print(directives)
    return snap, directives

def cmd_weekly(a, store):
    from .crew import plan_week
    snap, directives = cmd_report(a, store)
    client = json.loads(Path(a.client).read_text())
    rid, _ = plan_week(store, client, monday(a.week_of), directives, snap.model_dump_json())
    print(f"next week planned: {rid}")

def cmd_selftest(a, _store):
    """No LLM, no network, throwaway DB. Proves the plumbing end to end."""
    from .schemas import Goal, PlatformPlan, KPI, DecisionRule
    from .store import RETRYABLE, UNKNOWN
    tmp = Path(tempfile.mkdtemp(prefix="smm-selftest-"))
    store = Store(tmp / "selftest.sqlite3")
    week = monday(None)
    brief = StrategyBrief(goals=[Goal(id="G1", objective="sales", metric="conversions",
                                     target_value=60, deadline_days=90)],
                          audience="home brewing hobbyists",
                          platforms=[PlatformPlan(platform="Instagram", rationale="visual product",
                                                  funnel_role="conversion", posts_per_week=7,
                                                  budget_share_pct=100)],
                          organic_pct=60, paid_pct=40)
    posts = [Post(id=f"P{i:02d}", day=i, time_utc="15:00", platform="Instagram", format="Image",
                  goal_id="G1", hook="h", caption=f"Day {i} brew note", cta="Subscribe",
                  hashtags=["#coffee"], visual_brief="flat lay, 1080x1350",
                  asset_path=f"day{i}.jpg") for i in range(1, 8)]
    cal = ContentCalendar(posts=posts, engagement_playbook=["reply <2h", "like all", "no DMs"],
                          escalation_rules=["refund complaints to owner", "PR risk freeze queue"])
    plan = MeasurementPlan(
        kpis=[KPI(metric="conversions", scope="account", source_tool="GA4", goal_id="G1",
                  review_cadence="weekly")] * 3,
        decision_rules=[DecisionRule(id="R1", metric="engagement_rate", scope="account",
                                     comparator="<", threshold=0.02, min_impressions=100,
                                     action="shift 2 posts to Reels", severity="adjust"),
                        DecisionRule(id="R2", metric="ctr", scope="platform", platform="Instagram",
                                     comparator="<", threshold=0.001, min_impressions=100000,
                                     action="refresh creative", severity="adjust")],
        data_gaps=["no GA4 link tagging yet"])
    rid = store.new_run("plan", week, {"selftest": True})
    for n, o in (("strategy", brief), ("calendar", cal), ("measurement", plan)):
        store.save_artifact(rid, n, o)
    store.add_posts(rid, week, posts)
    store.finish_run(rid, {"total_tokens": 0}, "ok")

    # approval gate
    res = publish_run(store, rid)
    assert all(r["state"] == "blocked" for r in res), "unapproved posts must not send"
    for p in posts: store.decide(f"{rid}:{p.id}", "selftest", "approved")
    sent = publish_run(store, rid)
    assert all(r["state"] == "dry_run_ok" for r in sent), sent
    assert store.post(f"{rid}:P01")["status"] == "dry_run_ok", "dry run must not mark posts published"
    again = publish_run(store, rid)
    assert all(r["state"].startswith("skipped") for r in again), "idempotency failed"

    # a dry run must not consume the live idempotency key
    uid, h = f"{rid}:P01", posts[0].content_hash()
    _, may = store.claim(uid, h, "live")
    assert may, "dry run burned the live key"
    # retryable failures can be re-claimed; unknown never can
    k, _ = store.claim(f"{rid}:P02", posts[1].content_hash(), "live")
    store.settle(k, RETRYABLE, error="429")
    assert store.claim(f"{rid}:P02", posts[1].content_hash(), "live")[1], "retryable not re-claimable"
    store.settle(k, UNKNOWN, error="timeout")
    assert not store.claim(f"{rid}:P02", posts[1].content_hash(), "live")[1], "unknown was re-claimed"

    # later rejection revokes approval; tampered payload is blocked
    store.decide(f"{rid}:P03", "selftest", "rejected")
    assert store.approved_hash(f"{rid}:P03") is None, "rejection did not revoke approval"
    tampered = posts[3].model_copy(update={"caption": "Guaranteed miracle cure"})
    store.cx.execute("UPDATE posts SET payload_json=? WHERE post_uid=?",
                     (tampered.model_dump_json(), f"{rid}:P04"))
    r4 = publish_run(store, rid, only="P04")[0]
    assert r4["state"] == "blocked", r4

    # KPI math and rule firing
    csv = tmp / "metrics.csv"
    csv.write_text("date,platform,post_id,impressions,reach,likes,comments,shares,saves,"
                   "clicks,conversions,spend_usd,follower_delta\n"
                   f"{week},Instagram,P01,10000,8000,100,20,10,5,80,4,50,-3\n"
                   f"{week},Instagram,P01,10000,8000,100,20,10,5,80,4,50,-3\n")
    snap = compute_kpis(csv)
    assert snap.measured and snap.row_count == 1 and snap.account.engagements == 135
    assert snap.account.follower_delta == -3, "unfollows must not be clamped"
    assert abs(snap.account.value("engagement_rate") - 0.0135) < 1e-9
    fired = evaluate_rules(plan, snap)
    assert fired[0]["fired"], fired
    assert not fired[1]["fired"] and fired[1]["reason"].startswith("insufficient"), fired
    empty = compute_kpis(tmp / "does-not-exist.csv")
    assert not empty.measured and empty.value("ctr", "account", None) is None
    try:
        DecisionRule(id="R9", metric="ctr", scope="platform", comparator="<", threshold=1,
                     action="x", severity="info")
        raise AssertionError("platform rule without platform accepted")
    except ValueError:
        pass
    print("selftest OK: approval gate, hash binding, revocation, idempotency (per mode, retry, unknown), "
          "KPI math, rule firing, sample floor, no-data path")

def main():
    ap = argparse.ArgumentParser(prog="smm")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan"); p.add_argument("--client", default="client.json"); p.add_argument("--week-of")
    p = sub.add_parser("review"); p.add_argument("--run-id", required=True)
    p = sub.add_parser("approve"); p.add_argument("--run-id", required=True); p.add_argument("--post")
    p.add_argument("--all", action="store_true"); p.add_argument("--approver", required=True)
    p.add_argument("--reject", action="store_true"); p.add_argument("--note", default="")
    p = sub.add_parser("publish"); p.add_argument("--run-id", required=True)
    p.add_argument("--live", action="store_true"); p.add_argument("--post"); p.add_argument("--force", action="store_true")
    p = sub.add_parser("report"); p.add_argument("--metrics", required=True)
    p = sub.add_parser("weekly"); p.add_argument("--metrics", required=True)
    p.add_argument("--client", default="client.json"); p.add_argument("--week-of")
    sub.add_parser("selftest")
    a = ap.parse_args()
    store = None if a.cmd == "selftest" else Store(SETTINGS.db_path)
    {"plan": cmd_plan, "review": cmd_review, "approve": cmd_approve, "publish": cmd_publish,
     "report": cmd_report, "weekly": cmd_weekly, "selftest": cmd_selftest}[a.cmd](a, store)

if __name__ == "__main__":
    main()
