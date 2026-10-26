"""Config: commented YAML in ``conf/`` plus environment settings. Deliberately not Hydra.

- ``load_config(name)`` parses ``conf/<name>.yaml`` once per process (``lru_cache``), so a web
  request never re-parses YAML (moodrift fix #6).
- ``Settings`` holds what varies per deployment and every secret; secrets never live in YAML.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from fabloom.contracts import ModelRole, Platform, RunBudget

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FABLOOM_", env_file=".env", extra="ignore")

    token_factory_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("FABLOOM_TOKEN_FACTORY_API_KEY", "NEBIUS_API_KEY"),
    )
    token_factory_base_url: str = "https://api.tokenfactory.nebius.com/v1/"  # noqa: S105
    conf_dir: Path = REPO_ROOT / "conf"
    runs_dir: Path = Path("runs")
    verifier: Literal["local"] = "local"
    batfish_host: str = "localhost"
    tavily_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("FABLOOM_TAVILY_API_KEY", "TAVILY_API_KEY")
    )
    grounding_cache_dir: Path = Path(".cache/grounding")
    # Web demo (`fabloom serve`). The spend cap is per UTC day across all visitors; at the cap
    # the app turns read-only and serves the gallery instead of failing.
    # A conservative default for a public demo, which a deployment raises in its .env; a demo
    # run costs ~$0.003-0.02 (benchmarks v0.1 and vendors-v0.1).
    web_spend_cap_usd: float = Field(default=0.40, ge=0)
    # Per-run budget for web runs. Each run reserves this against the daily cap before it
    # starts, so with the CLI's $0.25 no run could start once $0.15 was spent. The costliest
    # v0.1 benchmark run was $0.021.
    web_run_max_usd: float = Field(default=0.05, gt=0)
    web_runs_per_ip: int = Field(default=3, ge=1)  # token bucket size
    web_refill_s: float = Field(default=600.0, gt=0)  # one more run per IP this often
    web_max_active_runs: int = Field(default=2, ge=1)
    # Only behind our own reverse proxy (Caddy) is X-Forwarded-For the visitor's address.
    web_trust_proxy: bool = False
    gallery_dir: Path = REPO_ROOT / "gallery"
    log_level: str = "INFO"
    log_json: bool = True
    # SQLite store plus ./mlruns artifacts; both are backed up off the VM.
    mlflow_tracking_uri: str = "sqlite:///mlflow.db"
    mlflow_artifact_root: Path = Path("mlruns")

    @field_validator("token_factory_api_key", "tavily_api_key", mode="before")
    @classmethod
    def _blank_key_is_missing(cls, v: object) -> object:
        # GitHub Actions expands an unset secret to "", which would otherwise pass the
        # `is None` guard in build_client and fail later as a 401 from Token Factory.
        return None if isinstance(v, str) and not v.strip() else v


def resolve(path: str | Path, base: Path = REPO_ROOT) -> Path:
    """Resolve a config-relative path against the repo root, leaving absolute paths alone."""
    p = Path(path).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


def ensure_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@lru_cache(maxsize=32)
def load_config(name: str, conf_dir: Path | None = None) -> dict[str, Any]:
    path = (conf_dir or Settings().conf_dir) / f"{name}.yaml"
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise TypeError(f"{path} must contain a YAML mapping")
    return data


# --- typed views over conf/*.yaml ------------------------------------------------------


class ModelSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    usd_per_m_input: float = Field(ge=0)
    usd_per_m_output: float = Field(ge=0)
    max_output_tokens: int = Field(default=8192, ge=1)
    temperature: float = Field(default=0.2, ge=0, le=2)
    price_verified: bool = False  # False until spikes/nemotron_probe.py confirms it
    extra_body: dict[str, Any] = Field(default_factory=dict)

    def cost_usd(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            prompt_tokens * self.usd_per_m_input + completion_tokens * self.usd_per_m_output
        ) / 1_000_000


class EscalationRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    from_round: int = Field(default=3, ge=1)
    critical_from_round: int = Field(default=2, ge=1)


class ModelsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    models: dict[str, ModelSpec]
    roles: dict[ModelRole, str]
    escalation: EscalationRule = EscalationRule()

    def for_role(self, role: ModelRole) -> ModelSpec:
        return self.models[self.roles[role]]


class GroundingConfig(BaseModel):
    """Doc-grounded repair (ADR-0008): what to search, and how hard."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Off by default: it ships only if its benchmark arm passes the grounding gate.
    enabled: bool = False
    # Search only vendor documentation, so an excerpt is the vendor's syntax, not a forum guess.
    domains: dict[Platform, list[str]] = Field(default_factory=dict)
    max_results: int = Field(default=3, ge=1, le=10)
    max_excerpt_chars: int = Field(default=800, ge=100)
    min_interval_s: float = Field(default=1.0, ge=0)  # between uncached searches, per process
    timeout_s: float = Field(default=15.0, gt=0)


class DocsConfig(BaseModel):
    """Vendor documentation for what a run built, linked beside the result.

    Searched after verification with the grounding domains. The links never reach a prompt,
    a check or a report, so they cannot change a verdict (ADR-0001).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    max_queries: int = Field(default=3, ge=1, le=6)  # Tavily searches per run, cache misses only
    max_links: int = Field(default=6, ge=1, le=20)
    # Docs change; a cached answer older than this is searched again.
    cache_ttl_days: float = Field(default=7.0, gt=0)


class RunConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    budget: RunBudget = RunBudget()
    repair_candidates: int = Field(default=1, ge=1, le=8)
    # The benchmark turns this off: nothing grades the explanation, so it would only add cost.
    explain_with_model: bool = True
    grounding: GroundingConfig = GroundingConfig()
    docs: DocsConfig = DocsConfig()


class ArmSpec(BaseModel):
    """One benchmark arm: a routing override plus run-limit overrides."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str
    roles: dict[ModelRole, str] = Field(default_factory=dict)
    max_repair_rounds: int | None = Field(default=None, ge=0)  # None: conf/fabloom.yaml's
    grounding: bool = False  # doc-grounded repair; only the grounding ablation arm sets it

    def models(self, base: ModelsConfig) -> ModelsConfig:
        unknown = sorted(set(self.roles.values()) - set(base.models))
        if unknown:
            raise ValueError(f"arm routes to models not in conf/models.yaml: {unknown}")
        return base.model_copy(update={"roles": {**base.roles, **self.roles}})

    def run_config(self, base: RunConfig) -> RunConfig:
        budget = base.budget
        if self.max_repair_rounds is not None:
            budget = budget.model_copy(update={"max_repair_rounds": self.max_repair_rounds})
        grounding = base.grounding.model_copy(update={"enabled": self.grounding})
        # Doc links are for a reader; nothing grades them, so the benchmark never searches.
        docs = base.docs.model_copy(update={"enabled": False})
        return base.model_copy(
            update={
                "budget": budget,
                "explain_with_model": False,
                "grounding": grounding,
                "docs": docs,
            }
        )


class RegressionGateSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metrics: list[str] = Field(default_factory=lambda: ["final_pass_rate"])
    max_drop: float = Field(default=0.10, ge=0, le=1)


class GroundingGateSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    arm: str = "super-repair-grounding"
    control: str = "super-repair"
    min_gain: float = Field(default=0.05, ge=0, le=1)
    max_cost_ratio: float = Field(default=1.5, gt=0)


class GatesSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    regression: RegressionGateSpec = RegressionGateSpec()
    grounding: GroundingGateSpec = GroundingGateSpec()


class NightlySpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    arms: list[str] = Field(default_factory=lambda: ["routed"])
    scenarios: int = Field(default=10, ge=1)


class BenchConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    arms: dict[str, ArmSpec]
    gates: GatesSpec = GatesSpec()
    nightly: NightlySpec = NightlySpec()


def models_config(conf_dir: Path | None = None) -> ModelsConfig:
    return ModelsConfig.model_validate(load_config("models", conf_dir))


def run_config(conf_dir: Path | None = None) -> RunConfig:
    return RunConfig.model_validate(load_config("fabloom", conf_dir).get("run", {}))


def bench_config(conf_dir: Path | None = None) -> BenchConfig:
    return BenchConfig.model_validate(load_config("bench", conf_dir))
