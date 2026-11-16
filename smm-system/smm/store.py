from __future__ import annotations
import hashlib, json, sqlite3, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .schemas import Post

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY, kind TEXT, week_of TEXT, created_at TEXT,
  inputs_json TEXT, tokens_json TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS artifacts(
  run_id TEXT, name TEXT, json TEXT, PRIMARY KEY(run_id, name));
CREATE TABLE IF NOT EXISTS posts(
  post_uid TEXT PRIMARY KEY, run_id TEXT, post_id TEXT, platform TEXT,
  payload_json TEXT, content_hash TEXT, scheduled_at TEXT, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS approvals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, post_uid TEXT, approver TEXT,
  decision TEXT, content_hash TEXT, note TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS publish_attempts(
  idem_key TEXT PRIMARY KEY, post_uid TEXT, content_hash TEXT, mode TEXT,
  state TEXT, remote_id TEXT, error TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS rate_limits(platform TEXT PRIMARY KEY, last_post_at TEXT);
CREATE TABLE IF NOT EXISTS snapshots(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, rule_id TEXT, fired INTEGER,
  observed REAL, action TEXT, created_at TEXT);
"""

# Attempt states. Only RETRYABLE may be re-claimed; UNKNOWN never is (reconcile by hand).
SENDING, PUBLISHED, RETRYABLE, FATAL, UNKNOWN = "sending", "published", "retryable", "failed", "unknown"

def now() -> str: return datetime.now(timezone.utc).isoformat(timespec="seconds")

class PublishBlocked(RuntimeError): pass

class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.cx = sqlite3.connect(path, isolation_level=None)
        self.cx.row_factory = sqlite3.Row
        self.cx.executescript("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;" + SCHEMA)

    # ---- runs / artifacts ----
    def new_run(self, kind: str, week_of: str, inputs: dict) -> str:
        rid = f"{kind}-{week_of}-{uuid.uuid4().hex[:6]}"
        self.cx.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?)",
                        (rid, kind, week_of, now(), json.dumps(inputs), None, "running"))
        return rid

    def finish_run(self, rid: str, tokens: dict | None, status: str = "ok") -> None:
        self.cx.execute("UPDATE runs SET tokens_json=?, status=? WHERE run_id=?",
                        (json.dumps(tokens) if tokens else None, status, rid))

    def save_artifact(self, rid: str, name: str, obj) -> None:
        payload = obj.model_dump_json() if hasattr(obj, "model_dump_json") else json.dumps(obj)
        self.cx.execute("INSERT OR REPLACE INTO artifacts VALUES(?,?,?)", (rid, name, payload))

    def artifact(self, rid: str, name: str) -> dict | None:
        r = self.cx.execute("SELECT json FROM artifacts WHERE run_id=? AND name=?", (rid, name)).fetchone()
        return json.loads(r["json"]) if r else None

    def latest_run(self, kind: str) -> sqlite3.Row | None:
        return self.cx.execute(
            "SELECT * FROM runs WHERE kind=? AND status='ok' ORDER BY created_at DESC LIMIT 1",
            (kind,)).fetchone()

    # ---- posts ----
    def add_posts(self, rid: str, week_of: str, posts: list[Post]) -> None:
        start = datetime.fromisoformat(week_of).replace(tzinfo=timezone.utc)
        for p in posts:
            hh, mm = map(int, p.time_utc.split(":"))
            when = (start + timedelta(days=p.day - 1)).replace(hour=hh, minute=mm)
            self.cx.execute(
                "INSERT OR IGNORE INTO posts VALUES(?,?,?,?,?,?,?,?,?)",
                (f"{rid}:{p.id}", rid, p.id, p.platform, p.model_dump_json(),
                 p.content_hash(), when.isoformat(timespec="seconds"), "pending_review", now()))

    def posts(self, rid: str) -> list[sqlite3.Row]:
        return list(self.cx.execute("SELECT * FROM posts WHERE run_id=? ORDER BY scheduled_at", (rid,)))

    def post(self, uid: str) -> sqlite3.Row | None:
        return self.cx.execute("SELECT * FROM posts WHERE post_uid=?", (uid,)).fetchone()

    # ---- approval gate: bound to the exact content hash ----
    def decide(self, uid: str, approver: str, decision: str, note: str = "") -> str:
        row = self.post(uid)
        if not row: raise PublishBlocked(f"unknown post {uid}")
        if decision not in ("approved", "rejected"): raise ValueError("bad decision")
        # Bind to the hash of the payload as it is now, not the stored column, so an
        # out-of-band payload edit can't ride on a stale hash.
        chash = Post.model_validate_json(row["payload_json"]).content_hash()
        self.cx.execute("INSERT INTO approvals(post_uid,approver,decision,content_hash,note,created_at)"
                        " VALUES(?,?,?,?,?,?)", (uid, approver, decision, chash, note, now()))
        self.cx.execute("UPDATE posts SET status=? WHERE post_uid=?", (decision, uid))
        return chash

    def approved_hash(self, uid: str) -> str | None:
        # The latest decision wins: a later rejection revokes an earlier approval.
        r = self.cx.execute("SELECT decision, content_hash FROM approvals WHERE post_uid=?"
                            " ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        return r["content_hash"] if r and r["decision"] == "approved" else None

    # ---- idempotent publish claim ----
    @staticmethod
    def idem_key(uid: str, chash: str, scheduled_at: str, mode: str) -> str:
        # mode is part of the key: a dry run must not consume the live key.
        return hashlib.sha256(f"{uid}|{chash}|{scheduled_at}|{mode}".encode()).hexdigest()

    def claim(self, uid: str, chash: str, mode: str) -> tuple[str, bool]:
        """Returns (idempotency_key, may_send). False means sent, in flight, failed fatally, or unknown."""
        row = self.post(uid)
        key = self.idem_key(uid, chash, row["scheduled_at"], mode)
        try:
            self.cx.execute("INSERT INTO publish_attempts VALUES(?,?,?,?,?,?,?,?,?)",
                            (key, uid, chash, mode, SENDING, None, None, now(), now()))
            return key, True
        except sqlite3.IntegrityError:
            # Only a failure we know never reached the platform may be re-claimed. Atomic.
            cur = self.cx.execute("UPDATE publish_attempts SET state=?, error=NULL, updated_at=?"
                                  " WHERE idem_key=? AND state=?", (SENDING, now(), key, RETRYABLE))
            return key, cur.rowcount == 1

    def settle(self, key: str, state: str, remote_id: str | None = None, error: str | None = None) -> None:
        self.cx.execute("UPDATE publish_attempts SET state=?, remote_id=?, error=?, updated_at=? WHERE idem_key=?",
                        (state, remote_id, error, now(), key))
        a = self.attempt(key)
        status = state
        if state == PUBLISHED:
            status = "published" if a["mode"] == "live" else "dry_run_ok"
        self.cx.execute("UPDATE posts SET status=? WHERE post_uid=?", (status, a["post_uid"]))

    def attempt(self, key: str) -> sqlite3.Row:
        return self.cx.execute("SELECT * FROM publish_attempts WHERE idem_key=?", (key,)).fetchone()

    # ---- platform throttle ----
    def throttle_ok(self, platform: str, min_interval_s: float) -> tuple[bool, float]:
        r = self.cx.execute("SELECT last_post_at FROM rate_limits WHERE platform=?", (platform,)).fetchone()
        if not r: return True, 0.0
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(r["last_post_at"])).total_seconds()
        return elapsed >= min_interval_s, max(0.0, min_interval_s - elapsed)

    def touch_throttle(self, platform: str) -> None:
        self.cx.execute("INSERT OR REPLACE INTO rate_limits VALUES(?,?)", (platform, now()))

    def record_snapshot(self, rid: str, snap) -> None:
        self.cx.execute("INSERT INTO snapshots(run_id,json,created_at) VALUES(?,?,?)",
                        (rid, snap.model_dump_json(), now()))

    def record_decision(self, rid: str, rule_id: str, fired: bool, observed, action: str) -> None:
        self.cx.execute("INSERT INTO decisions(run_id,rule_id,fired,observed,action,created_at)"
                        " VALUES(?,?,?,?,?,?)", (rid, rule_id, int(fired), observed, action, now()))
