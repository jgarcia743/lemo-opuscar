from __future__ import annotations
import hashlib, json, re
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator

Platform = Literal["Instagram", "Facebook", "LinkedIn", "X",
                   "YouTube", "Pinterest", "Snapchat", "Threads"]
PostFormat = Literal["Text", "Image", "Video", "Reel/Short", "Story",
                     "Carousel", "Infographic", "Live", "Poll/Quiz"]
MeasuredMetric = Literal["impressions", "reach", "engagements", "clicks",
                         "conversions", "spend_usd", "follower_delta",
                         "engagement_rate", "ctr", "conversion_rate", "cpa_usd"]

LIVE_CAPABLE = {"Instagram", "X", "LinkedIn"}
CAPTION_LIMIT = {"X": 280, "Instagram": 2200, "LinkedIn": 3000, "Facebook": 5000,
                 "YouTube": 5000, "Pinterest": 500, "Snapchat": 250, "Threads": 500}
MAX_HASHTAGS = 8

# ---------- strategy ----------
class Goal(BaseModel):
    id: str = Field(pattern=r"^G\d{1,2}$")
    objective: Literal["awareness", "traffic", "leads", "engagement", "sales", "community"]
    metric: MeasuredMetric          # must be computable from the metrics export
    target_value: float = Field(gt=0)
    deadline_days: int = Field(ge=7, le=365)

class PlatformPlan(BaseModel):
    platform: Platform
    rationale: str
    funnel_role: str
    posts_per_week: int = Field(ge=0, le=21)
    budget_share_pct: float = Field(ge=0, le=100)

class StrategyBrief(BaseModel):
    goals: list[Goal] = Field(min_length=1)
    audience: str
    platforms: list[PlatformPlan] = Field(min_length=1)
    organic_pct: float = Field(ge=0, le=100)
    paid_pct: float = Field(ge=0, le=100)
    assumptions: list[str] = []
    open_questions: list[str] = []

    @model_validator(mode="after")
    def _check(self):
        if abs(self.organic_pct + self.paid_pct - 100) > 0.5:
            raise ValueError("organic_pct + paid_pct must equal 100")
        if abs(sum(p.budget_share_pct for p in self.platforms) - 100) > 0.5:
            raise ValueError("platform budget shares must sum to 100")
        if len({g.id for g in self.goals}) != len(self.goals):
            raise ValueError("duplicate goal ids")
        if len({p.platform for p in self.platforms}) != len(self.platforms):
            raise ValueError("duplicate platforms")
        return self

    @property
    def platform_names(self) -> set[str]: return {p.platform for p in self.platforms}
    @property
    def goal_ids(self) -> set[str]: return {g.id for g in self.goals}

# ---------- content ----------
class Post(BaseModel):
    id: str = Field(pattern=r"^P\d{1,3}$")
    day: int = Field(ge=1, le=7)
    time_utc: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    platform: Platform
    format: PostFormat
    goal_id: str
    hook: str
    caption: str
    cta: str
    hashtags: list[str] = []
    visual_brief: str
    asset_path: str | None = None
    paid_boost: bool = False

    @field_validator("hashtags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        out = []
        for t in v:
            t = t.strip()
            if not t.startswith("#"):
                t = "#" + t
            if not re.fullmatch(r"#[A-Za-z0-9_]{2,30}", t):
                raise ValueError(f"invalid hashtag {t!r}")
            out.append(t.lower())
        if len(set(out)) != len(out):
            raise ValueError("duplicate hashtags")
        return out

    def render_text(self) -> str:
        tags = " ".join(self.hashtags)
        return f"{self.caption}\n\n{tags}".strip() if tags else self.caption

    def content_hash(self) -> str:
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

def lint_post(p: Post, banned: tuple[str, ...] = ()) -> list[str]:
    """Structural + compliance checks. Deterministic, runs before any human sees it."""
    errs: list[str] = []
    text = p.render_text()
    limit = CAPTION_LIMIT[p.platform]
    if len(text) > limit:
        errs.append(f"{p.id}: rendered text {len(text)} > {limit} for {p.platform}")
    if not p.caption.strip():
        errs.append(f"{p.id}: empty caption")
    if len(p.hashtags) > MAX_HASHTAGS:
        errs.append(f"{p.id}: {len(p.hashtags)} hashtags > {MAX_HASHTAGS}")
    if p.paid_boost and not any(t in ("#ad", "#sponsored") for t in p.hashtags):
        errs.append(f"{p.id}: paid_boost requires #ad or #sponsored disclosure")
    low = text.lower()
    for phrase in banned:
        if phrase in low:
            errs.append(f"{p.id}: flagged claim {phrase!r} needs legal review")
    if p.platform == "Instagram" and p.format in ("Image", "Reel/Short", "Carousel") and not p.asset_path:
        errs.append(f"{p.id}: Instagram {p.format} requires asset_path")
    return errs

class ContentCalendar(BaseModel):
    posts: list[Post] = Field(min_length=7)
    engagement_playbook: list[str] = Field(min_length=3)
    escalation_rules: list[str] = Field(min_length=2)

    @model_validator(mode="after")
    def _check(self):
        if {p.day for p in self.posts} != set(range(1, 8)):
            raise ValueError("need at least one post on each day 1-7")
        if len({p.id for p in self.posts}) != len(self.posts):
            raise ValueError("duplicate post ids")
        return self

# ---------- measurement (no LLM-authored numbers except decision thresholds) ----------
class KPI(BaseModel):
    metric: MeasuredMetric
    scope: Literal["account", "platform"]
    platform: Platform | None = None
    source_tool: str
    goal_id: str
    review_cadence: Literal["daily", "weekly", "monthly"]

    @model_validator(mode="after")
    def _check(self):
        if self.scope == "platform" and not self.platform:
            raise ValueError("platform scope requires a platform")
        return self

class DecisionRule(BaseModel):
    """Pre-committed, machine-evaluable. loop.py fires these; the LLM never decides."""
    id: str = Field(pattern=r"^R\d{1,2}$")
    metric: MeasuredMetric
    scope: Literal["account", "platform"]
    platform: Platform | None = None
    comparator: Literal["<", "<=", ">", ">="]
    threshold: float
    min_impressions: int = Field(ge=0, default=500)
    action: str
    severity: Literal["info", "adjust", "halt"]

    @model_validator(mode="after")
    def _check(self):
        # Without this, a platform-scoped rule with no platform silently evaluates to "no data" forever.
        if self.scope == "platform" and not self.platform:
            raise ValueError("platform scope requires a platform")
        return self

class MeasurementPlan(BaseModel):
    kpis: list[KPI] = Field(min_length=3)
    decision_rules: list[DecisionRule] = Field(min_length=2)
    experiments: list[str] = []
    data_gaps: list[str] = []

    @model_validator(mode="after")
    def _check(self):
        if len({r.id for r in self.decision_rules}) != len(self.decision_rules):
            raise ValueError("duplicate rule ids")
        return self

# ---------- computed facts ----------
class Totals(BaseModel):
    posts: int = 0
    impressions: float = 0
    reach: float = 0
    engagements: float = 0
    clicks: float = 0
    conversions: float = 0
    spend_usd: float = 0
    follower_delta: float = 0

    def derived(self) -> dict[str, float | None]:
        return {
            "engagement_rate": self.engagements / self.impressions if self.impressions else None,
            "ctr": self.clicks / self.impressions if self.impressions else None,
            "conversion_rate": self.conversions / self.clicks if self.clicks else None,
            "cpa_usd": self.spend_usd / self.conversions if self.conversions else None,
        }

    def value(self, metric: str) -> float | None:
        d = self.derived()
        return d[metric] if metric in d else getattr(self, metric, None)

class KpiSnapshot(BaseModel):
    measured: bool
    source: str
    sha256: str
    row_count: int
    period_start: str | None = None
    period_end: str | None = None
    account: Totals = Totals()
    by_platform: dict[str, Totals] = {}
    warnings: list[str] = []

    def value(self, metric: str, scope: str, platform: str | None) -> float | None:
        if not self.measured:
            return None
        t = self.account if scope == "account" else self.by_platform.get(platform or "")
        return t.value(metric) if t else None
