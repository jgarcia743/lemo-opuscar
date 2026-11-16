from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

def _f(k: str, d: float) -> float:
    try: return float(os.getenv(k, d))
    except ValueError: return d

@dataclass(frozen=True)
class Settings:
    db_path: Path = Path(os.getenv("SMM_DB", "state/smm.sqlite3"))
    out_dir: Path = Path(os.getenv("SMM_OUT", "out"))
    # Fail-closed: anything other than the literal "live" means dry run.
    publish_mode: str = os.getenv("SMM_PUBLISH_MODE", "dry_run")
    strong_model: str = os.getenv("SMM_STRONG_MODEL") or "gpt-4o"
    cheap_model: str = os.getenv("SMM_CHEAP_MODEL") or "gpt-4o-mini"
    max_rpm: int = int(_f("SMM_MAX_RPM", 60))
    token_budget: int = int(_f("SMM_TOKEN_BUDGET", 120_000))
    graph_version: str = os.getenv("META_GRAPH_VERSION") or "v21.0"
    ig_user_id: str = os.getenv("META_IG_USER_ID", "")
    meta_token: str = os.getenv("META_ACCESS_TOKEN", "")
    x_token: str = os.getenv("X_USER_ACCESS_TOKEN", "")
    li_token: str = os.getenv("LINKEDIN_ACCESS_TOKEN", "")
    li_author: str = os.getenv("LINKEDIN_AUTHOR_URN", "")
    public_asset_base: str = os.getenv("SMM_PUBLIC_ASSET_BASE", "")
    min_interval_s: dict = field(default_factory=lambda: {
        "Instagram": _f("SMM_MIN_INTERVAL_IG", 900),
        "X": _f("SMM_MIN_INTERVAL_X", 300),
        "LinkedIn": _f("SMM_MIN_INTERVAL_LI", 1800),
    })
    banned_claims: tuple = ("guaranteed", "risk-free", "cure", "miracle", "100% returns")

SETTINGS = Settings()
