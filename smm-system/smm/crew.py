from __future__ import annotations
from pathlib import Path
from pydantic import BaseModel, Field
from .config import SETTINGS
from .schemas import StrategyBrief, ContentCalendar, MeasurementPlan, lint_post
from .store import Store

try:
    from crewai.tools import BaseTool
except ImportError:  # older layouts
    from crewai_tools import BaseTool  # type: ignore
from crewai import Agent, Crew, LLM, Process, Task

# Every {placeholder} in the task descriptions. Checked before kickoff so a missing key
# fails fast instead of reaching the model as a literal "{offer}".
CLIENT_KEYS = ("business", "offer", "market", "audience_hint", "monthly_budget_usd", "primary_kpi",
               "horizon_days", "constraints", "brand_voice", "metrics_csv")

class _CSVArgs(BaseModel):
    path: str = Field(description="Path to a metrics CSV export")

class MetricsCSVTool(BaseTool):
    name: str = "read_metrics_csv"
    description: str = ("Reads a real metrics CSV for drill-down. Aggregate KPIs are already computed "
                        "and supplied in your task inputs; do not recompute or estimate them.")
    args_schema: type[BaseModel] = _CSVArgs
    def _run(self, path: str) -> str:
        p = Path(path)
        return p.read_text(encoding="utf-8")[:20_000] if p.is_file() else f"NO DATA: {path}"

def build_crew():
    strong = LLM(model=SETTINGS.strong_model, temperature=0.3)
    cheap = LLM(model=SETTINGS.cheap_model, temperature=0.7)

    strategist = Agent(
        role="SMM Lead Strategist",
        goal="Produce a goal-, audience-, and budget-grounded platform strategy for the client in the inputs.",
        backstory=("You choose platforms by where the stated audience is, not by popularity. Every goal "
                   "metric must be one the metrics export can actually produce. You obey binding directives "
                   "from prior periods. You list assumptions and open questions instead of guessing. "
                   "You never state a performance number that was not given to you."),
        llm=strong, allow_delegation=False, max_iter=6, max_execution_time=600, verbose=True)

    creator = Agent(
        role="Social Media Content Specialist",
        goal="Turn the approved strategy into a 7-day calendar of publish-ready posts.",
        backstory=("You write in the client's brand voice. One objective and one CTA per post. Visual briefs "
                   "are executable in Canva without a follow-up call. You respect platform character limits, "
                   "keep hashtags few and relevant, and add #ad to anything paid. You avoid unverifiable claims."),
        llm=cheap, allow_delegation=False, max_iter=6, max_execution_time=600, verbose=True)

    analyst = Agent(
        role="SMM Data Analyst",
        goal="Design a measurement plan whose decision rules a Python function can evaluate unattended.",
        backstory=("You never author performance numbers. Baselines come from the computed facts in your inputs; "
                   "if a value is null it is unknown and belongs in data_gaps. Your rules are pre-committed "
                   "thresholds with minimum sample sizes, phrased so a machine can fire them without you."),
        llm=strong, tools=[MetricsCSVTool()], allow_delegation=False, max_iter=8, verbose=True)

    strategy_task = Task(
        description=(
            "Client: {business}\nOffer: {offer}\nMarket: {market}\nAudience notes: {audience_hint}\n"
            "Monthly budget USD: {monthly_budget_usd}\nPrimary KPI: {primary_kpi}\n"
            "Horizon days: {horizon_days}\nConstraints: {constraints}\n\n"
            "PRIOR-PERIOD DIRECTIVES:\n{prior_directives}\n\n"
            "COMPUTED FACTS (authoritative, null means unknown):\n{prior_kpis}\n\n"
            "Set 3-5 goals with ids G1..G5, each using a metric from the allowed enum. Define the audience. "
            "Choose 2-4 platforms with rationale and posts_per_week. Budget splits must each total 100. "
            "Record every assumption and every question the client must answer."),
        expected_output="A StrategyBrief object.", agent=strategist, output_pydantic=StrategyBrief)

    def _brief() -> StrategyBrief | None:
        return getattr(strategy_task.output, "pydantic", None)

    def calendar_guardrail(output):
        cal = getattr(output, "pydantic", None)
        if cal is None: return False, "Return a ContentCalendar matching the schema."
        brief = _brief()
        errs: list[str] = []
        for p in cal.posts:
            errs += lint_post(p, SETTINGS.banned_claims)
            if brief and p.platform not in brief.platform_names:
                errs.append(f"{p.id}: {p.platform} not in approved strategy")
            if brief and p.goal_id not in brief.goal_ids:
                errs.append(f"{p.id}: unknown goal_id {p.goal_id}")
        return (False, "Fix these:\n" + "\n".join(errs[:15])) if errs else (True, output)

    def measurement_guardrail(output):
        plan = getattr(output, "pydantic", None)
        if plan is None: return False, "Return a MeasurementPlan matching the schema."
        brief = _brief()
        errs = []
        if brief:
            covered = {k.goal_id for k in plan.kpis}
            for gid in sorted(brief.goal_ids - covered):
                errs.append(f"goal {gid} has no KPI")
            for r in plan.decision_rules:
                if r.scope == "platform" and r.platform not in brief.platform_names:
                    errs.append(f"{r.id}: platform {r.platform} not in strategy")
        return (False, "Fix these:\n" + "\n".join(errs)) if errs else (True, output)

    # Guardrails go in the constructor: some CrewAI versions only wire them up in a validator.
    content_task = Task(
        description=(
            "Build a 7-day calendar, at least one post per day, ids P01..Pnn, only on platforms the brief "
            "selected, each tagged to a goal_id from the brief. Brand voice: {brand_voice}. "
            "Set time_utc as HH:MM. Draft the real caption, hook, CTA, at most 8 hashtags, and a Canva-ready "
            "visual brief. Use format Text for text-only X and LinkedIn posts. Instagram Image/Reel posts "
            "need an asset_path filename. Paid posts need #ad. "
            "Then write the engagement playbook with response SLA and tone, and escalation rules for "
            "complaints and PR risk."),
        expected_output="A ContentCalendar object.", agent=creator,
        context=[strategy_task], output_pydantic=ContentCalendar, guardrail=calendar_guardrail)

    analytics_task = Task(
        description=(
            "Design the measurement plan. Metrics export path: {metrics_csv}. "
            "Computed facts:\n{prior_kpis}\n\n"
            "Map at least one KPI to every goal id in the brief, naming the tool that will supply it. "
            "Write 2-5 DecisionRules with ids R1..R5, each with metric, scope, comparator, threshold, "
            "min_impressions, a concrete action, and severity. Platform-scoped rules must name the platform. "
            "Rules must be evaluable from the export columns alone. List every metric you cannot yet "
            "source in data_gaps. Output no baselines."),
        expected_output="A MeasurementPlan object.", agent=analyst,
        context=[strategy_task, content_task], output_pydantic=MeasurementPlan,
        guardrail=measurement_guardrail)

    crew = Crew(agents=[strategist, creator, analyst],
                tasks=[strategy_task, content_task, analytics_task],
                process=Process.sequential, verbose=True, cache=True, max_rpm=SETTINGS.max_rpm)
    return crew, (strategy_task, content_task, analytics_task)

def plan_week(store: Store, client: dict, week_of: str, directives: str = "",
              kpis_json: str = "{}") -> tuple[str, dict]:
    missing = [k for k in CLIENT_KEYS if k not in client]
    if missing:
        raise SystemExit(f"client.json missing keys: {missing}")
    inputs = dict(client, prior_directives=directives or "First run. No prior data.",
                  prior_kpis=kpis_json)
    crew, tasks = build_crew()
    rid = store.new_run("plan", week_of, inputs)
    try:
        result = crew.kickoff(inputs=inputs)
    except Exception:
        store.finish_run(rid, None, "error"); raise
    tokens = getattr(result, "token_usage", None)
    tokens = tokens.model_dump() if hasattr(tokens, "model_dump") else (tokens or {})
    if tokens.get("total_tokens", 0) > SETTINGS.token_budget:
        store.finish_run(rid, tokens, "over_budget")
        raise RuntimeError(f"token budget exceeded: {tokens}")
    names = ("strategy", "calendar", "measurement")
    out = {}
    for name, task in zip(names, tasks):
        obj = getattr(task.output, "pydantic", None)
        if obj is None:
            store.finish_run(rid, tokens, "schema_failure")
            raw = getattr(task.output, "raw", "") or ""
            raise RuntimeError(f"{name} did not validate after guardrail retries: {raw[:400]}")
        store.save_artifact(rid, name, obj); out[name] = obj
    store.add_posts(rid, week_of, out["calendar"].posts)
    store.finish_run(rid, tokens, "ok")
    return rid, out
