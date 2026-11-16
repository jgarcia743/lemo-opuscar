from __future__ import annotations
import time
from datetime import datetime, timezone
from pathlib import Path
import requests
from urllib3.exceptions import NameResolutionError, NewConnectionError
from .config import SETTINGS
from .schemas import Post, LIVE_CAPABLE, lint_post
from .store import Store, PublishBlocked, PUBLISHED, RETRYABLE, FATAL, UNKNOWN

class AmbiguousSend(RuntimeError):
    """Request may have reached the platform. Never auto-retry; reconcile manually."""

class FatalSend(RuntimeError): pass
class RetryableSend(RuntimeError): pass

def _never_connected(e: requests.ConnectionError) -> bool:
    """True only when we can prove no bytes reached the server (DNS failure, refused, connect timeout)."""
    if isinstance(e, (requests.ConnectTimeout, requests.exceptions.ProxyError, requests.exceptions.SSLError)):
        return True
    reason = getattr(e.args[0], "reason", None) if e.args else None
    return isinstance(reason, (NewConnectionError, NameResolutionError))

def _post(url: str, **kw) -> requests.Response:
    """Non-idempotent write. Anything that might have created the post is AmbiguousSend."""
    try:
        r = requests.post(url, timeout=(5, 45), **kw)
    except requests.ConnectionError as e:   # includes ConnectTimeout
        if _never_connected(e):
            raise RetryableSend(f"connection failed before send: {e}") from e
        raise AmbiguousSend(f"connection dropped after send may have begun: {e}") from e
    except requests.Timeout as e:           # ReadTimeout: request was sent
        raise AmbiguousSend(f"timeout after send: {e}") from e
    if r.status_code < 300:
        return r
    body = r.text[:400]
    if r.status_code == 429:
        raise RetryableSend(f"429 rate limited: {body}")
    if r.status_code >= 500:
        # A 5xx on a create can still have created it (X and Meta both do this). Don't guess.
        raise AmbiguousSend(f"{r.status_code} on create, outcome unknown: {body}")
    raise FatalSend(f"{r.status_code}: {body}")

def _json(r: requests.Response) -> dict:
    try:
        return r.json() if r.content else {}
    except ValueError as e:
        raise AmbiguousSend(f"2xx with unparseable body; post may exist: {r.text[:200]}") from e

class Adapter:
    def check(self, post: Post) -> None:
        """Raise FatalSend if this adapter can't send this post. Called before claiming."""
    def send(self, post: Post) -> str: raise NotImplementedError

class DryRun(Adapter):
    def send(self, post: Post) -> str:
        print(f"[dry-run] {post.platform} {post.id}: {post.render_text()[:120]!r}")
        return f"dryrun:{post.content_hash()[:12]}"

class Instagram(Adapter):
    """Graph API two-step: create container -> poll -> publish. Verify version against current docs."""
    def __init__(self, s=SETTINGS): self.s = s
    @property
    def base(self): return f"https://graph.facebook.com/{self.s.graph_version}"

    def check(self, post: Post) -> None:
        if not (self.s.ig_user_id and self.s.meta_token and self.s.public_asset_base):
            raise FatalSend("Instagram requires META_IG_USER_ID, META_ACCESS_TOKEN, SMM_PUBLIC_ASSET_BASE")
        if post.format not in ("Image", "Infographic", "Reel/Short"):
            raise FatalSend(f"{post.format} not automated here; route to manual queue")
        if not post.asset_path:
            raise FatalSend("Instagram requires a hosted asset")

    def send(self, post: Post) -> str:
        self.check(post)
        url = f"{self.s.public_asset_base.rstrip('/')}/{Path(post.asset_path).name}"
        params = {"caption": post.render_text(), "access_token": self.s.meta_token}
        if post.format == "Reel/Short":
            params |= {"media_type": "REELS", "video_url": url, "share_to_feed": "true"}
        else:
            params |= {"image_url": url}
        # Creating a container publishes nothing, so any failure up to media_publish is retryable.
        try:
            cid = _json(_post(f"{self.base}/{self.s.ig_user_id}/media", data=params)).get("id")
            if not cid: raise FatalSend("no container id returned")
            for _ in range(20):
                time.sleep(3)
                st = requests.get(f"{self.base}/{cid}", params={"fields": "status_code",
                                  "access_token": self.s.meta_token}, timeout=(5, 20)).json().get("status_code")
                if st == "FINISHED": break
                if st == "ERROR": raise FatalSend("container processing failed")
            else:
                raise RetryableSend("container never reached FINISHED; nothing was published")
        except AmbiguousSend as e:
            raise RetryableSend(f"container step (nothing published): {e}") from e
        except (requests.RequestException, ValueError) as e:
            raise RetryableSend(f"container status poll failed (nothing published): {e}") from e
        r = _post(f"{self.base}/{self.s.ig_user_id}/media_publish",
                  data={"creation_id": cid, "access_token": self.s.meta_token})
        rid = _json(r).get("id")
        if not rid: raise AmbiguousSend("media_publish returned 2xx without an id")
        return rid

class XText(Adapter):
    def check(self, post: Post) -> None:
        if not SETTINGS.x_token: raise FatalSend("X_USER_ACCESS_TOKEN missing (OAuth2 user context)")
        if post.format != "Text" or post.asset_path:
            raise FatalSend("X adapter is text-only; media and polls need the v2 media/poll flows")

    def send(self, post: Post) -> str:
        self.check(post)
        r = _post("https://api.x.com/2/tweets", json={"text": post.render_text()},
                  headers={"Authorization": f"Bearer {SETTINGS.x_token}"})
        rid = _json(r).get("data", {}).get("id")
        if not rid: raise AmbiguousSend("tweet create returned 2xx without an id")
        return rid

class LinkedInText(Adapter):
    def check(self, post: Post) -> None:
        if not (SETTINGS.li_token and SETTINGS.li_author):
            raise FatalSend("LINKEDIN_ACCESS_TOKEN and LINKEDIN_AUTHOR_URN required")
        if post.format != "Text" or post.asset_path:
            raise FatalSend("LinkedIn adapter is text-only; media requires the assets upload flow")

    def send(self, post: Post) -> str:
        self.check(post)
        body = {"author": SETTINGS.li_author, "lifecycleState": "PUBLISHED",
                "specificContent": {"com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": post.render_text()}, "shareMediaCategory": "NONE"}},
                "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}}
        r = _post("https://api.linkedin.com/v2/ugcPosts", json=body,
                  headers={"Authorization": f"Bearer {SETTINGS.li_token}",
                           "X-Restli-Protocol-Version": "2.0.0"})
        # 201 often has an empty body; the URN comes back in X-RestLi-Id.
        return _json(r).get("id") or r.headers.get("x-restli-id") or "unknown"

ADAPTERS = {"Instagram": Instagram, "X": XText, "LinkedIn": LinkedInText}

def publish_run(store: Store, run_id: str, live: bool = False, only: str | None = None,
                force: bool = False) -> list[dict]:
    mode = "live" if (live and SETTINGS.publish_mode == "live") else "dry_run"
    if live and mode != "live":
        raise PublishBlocked("--live requested but SMM_PUBLISH_MODE is not 'live'. Refusing.")
    results = []
    for row in store.posts(run_id):
        uid = row["post_uid"]
        if only and row["post_id"] != only: continue
        post = Post.model_validate_json(row["payload_json"])
        out = {"post": row["post_id"], "platform": post.platform}

        errs = lint_post(post, SETTINGS.banned_claims)
        if errs:
            out |= {"state": "blocked", "why": errs}; results.append(out); continue
        # Gate 1: hash-bound human approval.
        if store.approved_hash(uid) != post.content_hash():
            out |= {"state": "blocked", "why": "no approval for this exact content hash"}
            results.append(out); continue
        # Gate 2: capability (platform and format), checked before anything is claimed.
        if mode == "live":
            if post.platform not in LIVE_CAPABLE:
                out |= {"state": "manual_required", "why": f"no adapter for {post.platform}"}
                results.append(out); continue
            adapter = ADAPTERS[post.platform]()
            try:
                adapter.check(post)
            except FatalSend as e:
                out |= {"state": "manual_required", "why": str(e)}; results.append(out); continue
        else:
            adapter = DryRun()
        # Gate 3: schedule.
        due = datetime.fromisoformat(row["scheduled_at"]) <= datetime.now(timezone.utc)
        if mode == "live" and not due and not force:
            out |= {"state": "not_due", "why": row["scheduled_at"]}; results.append(out); continue

        ok, wait = store.throttle_ok(post.platform, SETTINGS.min_interval_s.get(post.platform, 600))
        if mode == "live" and not ok and not force:
            out |= {"state": "throttled", "why": f"retry in {wait:.0f}s"}; results.append(out); continue

        key, may_send = store.claim(uid, post.content_hash(), mode)
        if not may_send:
            prev = store.attempt(key)
            out |= {"state": f"skipped:{prev['state']}", "why": "idempotency key already used"}
            results.append(out); continue

        try:
            remote = adapter.send(post)
        except RetryableSend as e:
            store.settle(key, RETRYABLE, error=str(e))
            out |= {"state": "failed_retryable", "why": str(e)}
        except AmbiguousSend as e:
            store.settle(key, UNKNOWN, error=str(e))
            out |= {"state": "unknown", "why": f"{e} — RECONCILE MANUALLY, do not re-send"}
        except Exception as e:  # FatalSend and anything unexpected
            store.settle(key, FATAL, error=f"{type(e).__name__}: {e}")
            out |= {"state": "failed_fatal", "why": str(e)}
        else:
            store.settle(key, PUBLISHED, remote_id=remote)
            if mode == "live":
                store.touch_throttle(post.platform)
            out |= {"state": "published" if mode == "live" else "dry_run_ok",
                    "remote_id": remote, "mode": mode}
        results.append(out)
    return results
