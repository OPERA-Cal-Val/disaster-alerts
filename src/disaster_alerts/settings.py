from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

__all__ = [
    "EmailConfig",
    "ProvidersConfig",
    "RoutingConfig",
    "GlobalThresholds",
    "EarthquakeThresholds",
    "WeatherThresholds",
    "Thresholds",
    "Recipients",
    "Paths",
    "AppConfig",
    "Settings",
]

# ---------- tiny .env loader (opt-in, no external dependency) ----------


def _load_dotenv(dotenv_path: Path) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ if not already set."""
    if not dotenv_path.exists():
        return
    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        os.environ.setdefault(key, val)


# ---------- helpers ----------

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _env_expand(value: Any) -> Any:
    """Expand ${VAR} using environment variables within YAML scalar strings."""
    if isinstance(value, str):

        def repl(match: re.Match[str]) -> str:
            var = match.group(1)
            return os.environ.get(var, match.group(0))

        return _ENV_VAR_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _env_expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_env_expand(v) for v in value]
    return value


def _read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _env_expand(data)  # type: ignore[return-value]


# ---------- domain validation utils ----------


def _is_number_pair(x: Any) -> bool:
    try:
        return (
            isinstance(x, (list, tuple))
            and len(x) == 2
            and all(isinstance(v, (int, float)) for v in x)
        )
    except Exception:
        return False


def _validate_geojson_polygon(coords: Any) -> bool:
    # Polygon: [ [ [x,y], [x,y], ... ] , [ ...hole... ]? ]
    if not isinstance(coords, list) or not coords:
        return False
    outer = coords[0]
    if not isinstance(outer, list) or len(outer) < 4:
        return False
    if not all(_is_number_pair(pt) for pt in outer):
        return False
    # Optionally ensure ring closure; most APIs give closed rings already.
    return True


def _validate_geojson_multipolygon(coords: Any) -> bool:
    # MultiPolygon: [ [ [ [x,y], ... ] , ...holes ] , ...polygons ]
    if not isinstance(coords, list) or not coords:
        return False
    for poly in coords:
        if not isinstance(poly, list) or not poly:
            return False
        if not _validate_geojson_polygon(poly):
            return False
    return True


# ---------- Pydantic models ----------


class EmailConfig(BaseModel):
    user: Optional[str] = Field(default=None, description="yagmail username (email)")
    app_password: Optional[str] = Field(
        default=None, description="yagmail app password / token"
    )

    @field_validator("user")
    @classmethod
    def _validate_user(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not _EMAIL_RE.match(v):
            raise ValueError("YAGMAIL_USER looks invalid; expected an email address")
        return v

    @property
    def is_configured(self) -> bool:
        return bool(self.user and self.app_password)


class ProvidersConfig(BaseModel):
    nws: bool = True
    usgs: bool = True


class RoutingConfig(BaseModel):
    force_group: Optional[str] = None
    fallback_to_default: bool = True
    merge: Dict[str, str] = Field(default_factory=dict)
    drop_groups: List[str] = Field(default_factory=list)


class GlobalThresholds(BaseModel):
    min_severity: Optional[str] = None  # "Minor"|"Moderate"|"Severe"|"Extreme"


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    log_level: str = Field(default="INFO", description="Python logging level")
    display_timezone: Optional[str] = Field(
        default=None,
        description=(
            "IANA tz name used when rendering email times (e.g., 'America/Los_Angeles')"
        ),
    )
    aoi: Optional[Dict[str, Any]] = Field(
        default=None, description="GeoJSON Polygon/MultiPolygon"
    )
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    no_html: bool = Field(
        default=False,
        description="Generate an HTML file with geometries of events AOIs",
    )
    lookback_days: float = Field(
        default=0.0,
        description=(
            "Web map only: days of past (ended) NWS warnings to include; "
            "0 = active only. E-mail alerts always use active alerts."
        ),
    )

    @field_validator("lookback_days")
    @classmethod
    def _check_lookback(cls, v: float) -> float:
        if v < 0 or v > 30:
            raise ValueError("lookback_days must be between 0 and 30")
        return v

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, v: str) -> str:
        lv = v.upper().strip()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if lv not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return lv

    @field_validator("aoi")
    @classmethod
    def _validate_aoi(cls, v: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if v is None:
            return v
        if not isinstance(v, dict):
            raise ValueError("aoi must be a GeoJSON object")
        gj_type = v.get("type")
        coords = v.get("coordinates")
        if gj_type not in {"Polygon", "MultiPolygon"}:
            raise ValueError("aoi.type must be 'Polygon' or 'MultiPolygon'")
        ok = (
            _validate_geojson_polygon(coords)
            if gj_type == "Polygon"
            else _validate_geojson_multipolygon(coords)
        )
        if not ok:
            raise ValueError("aoi.coordinates is not a valid GeoJSON ring structure")
        return v


# ---- Threshold schemas (extensible, extra allowed for future hazards) ----


class EarthquakeThresholds(BaseModel):
    min_magnitude: float = 4.5
    max_depth_km: float = 700.0

    @field_validator("min_magnitude")
    @classmethod
    def _check_mag(cls, v: float) -> float:
        if v < 0 or v > 10:
            raise ValueError("min_magnitude must be between 0 and 10")
        return v

    @field_validator("max_depth_km")
    @classmethod
    def _check_depth(cls, v: float) -> float:
        if v <= 0 or v > 1000:
            raise ValueError("max_depth_km must be in (0, 1000]")
        return v


class WeatherThresholds(BaseModel):
    wind_gust_mps: Optional[float] = None
    rainfall_mm_hr: Optional[float] = None
    include_events: List[str] = Field(default_factory=list)
    exclude_events: List[str] = Field(default_factory=list)

    @field_validator("wind_gust_mps", "rainfall_mm_hr")
    @classmethod
    def _non_negative(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return v
        if v < 0:
            raise ValueError("threshold values must be non-negative")
        return v


class Thresholds(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    global_: GlobalThresholds = Field(default_factory=GlobalThresholds, alias="global")
    earthquake: Optional[EarthquakeThresholds] = None
    weather: Optional[WeatherThresholds] = None


class Recipients(BaseModel):
    """Mapping of routing keys → recipient lists (emails)."""

    model_config = ConfigDict(extra="allow")

    @classmethod
    def from_raw(cls, raw: Dict[str, Any]) -> "Recipients":
        # Validate each list contains valid emails
        for key, val in raw.items():
            if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
                raise ValidationError.from_exception_data(
                    "Recipients",
                    [
                        {
                            "type": "value_error",
                            "loc": (key,),
                            "msg": (
                                "each recipients list must be a list of strings"
                                " (emails)"
                            ),
                            "input": val,
                        }
                    ],
                )
            bad = [e for e in val if not _EMAIL_RE.match(e)]
            if bad:
                raise ValidationError.from_exception_data(
                    "Recipients",
                    [
                        {
                            "type": "value_error.email",
                            "loc": (key,),
                            "msg": f"invalid email(s): {bad}",
                            "input": val,
                        }
                    ],
                )
        obj = cls()
        for k, v in raw.items():
            setattr(obj, k, v)
        return obj

    def get(self, key: str, default: Optional[List[str]] = None) -> List[str]:
        return getattr(self, key, default or [])


class Paths(BaseModel):
    root: Path
    config_dir: Path
    data_dir: Path
    logs_dir: Path
    state_file: Path

    @field_validator(
        "root", "config_dir", "data_dir", "logs_dir", "state_file", mode="before"
    )
    @classmethod
    def _expanduser(cls, v: Union[str, Path]) -> Path:
        return Path(v).expanduser()


class Settings(BaseModel):
    """Single source of truth for runtime configuration."""

    paths: Paths
    app: AppConfig
    thresholds: Thresholds = Field(default_factory=Thresholds)
    recipients: Recipients = Field(default_factory=Recipients)
    email: EmailConfig

    # ----------------- loader -----------------

    @classmethod
    def load(
        cls,
        root: Optional[Path] = None,
        dotenv: Optional[Path] = None,
    ) -> "Settings":
        """
        Load settings from environment + YAML files.

        Precedence:
          1) Environment variables (including those loaded from `.env`)
          2) YAML files (app.yaml, thresholds.yaml, recipients.yaml)
          3) Defaults in the models
        """
        # Determine repo root
        inferred_root = Path(os.environ.get("DISASTER_ALERTS_ROOT", "")).expanduser()
        if not str(inferred_root):
            # src/disaster_alerts/settings.py -> project root is parents[2]
            inferred_root = Path(__file__).resolve().parents[2]
        base_root = root or inferred_root

        # Config directory override
        cfg_override = os.environ.get("DISASTER_ALERTS_CONFIG_DIR")
        config_dir = (
            Path(cfg_override).expanduser() if cfg_override else (base_root / "config")
        )

        # Optionally load .env from repo root (not config dir)
        dotenv_path = dotenv or base_root / ".env"
        _load_dotenv(dotenv_path)

        # Paths
        data_dir = base_root / "data"
        logs_dir = base_root / "logs"
        state_file = data_dir / "state.json"

        paths = Paths(
            root=base_root,
            config_dir=config_dir,
            data_dir=data_dir,
            logs_dir=logs_dir,
            state_file=state_file,
        )

        # YAMLs (explicit error if app.yaml missing; others optional)
        app_path = config_dir / "app.yaml"
        if not app_path.exists():
            raise RuntimeError(
                f"Missing required config: {app_path}. "
                "Copy the template under config/app.yaml."
            )
        app_yaml = _read_yaml(app_path)
        thresholds_yaml = _read_yaml(config_dir / "thresholds.yaml")
        recipients_yaml = _read_yaml(config_dir / "recipients.yaml")

        # Build AppConfig
        try:
            app_cfg = AppConfig(**app_yaml)
        except ValidationError as e:
            raise RuntimeError(f"Invalid app.yaml configuration: {e}") from e

        # Email from environment only (no secrets in YAML)
        email_cfg = EmailConfig(
            user=os.environ.get("YAGMAIL_USER"),
            app_password=os.environ.get("YAGMAIL_APP_PASSWORD"),
        )

        # Thresholds (typed; allows extra hazard keys)
        try:
            thresholds_cfg = Thresholds(**(thresholds_yaml or {}))
        except ValidationError as e:
            raise RuntimeError(f"Invalid thresholds.yaml: {e}") from e

        # Recipients (validate lists + emails)
        try:
            recipients_cfg = Recipients.from_raw(recipients_yaml or {})
        except ValidationError as e:
            raise RuntimeError(f"Invalid recipients.yaml: {e}") from e

        return cls(
            paths=paths,
            app=app_cfg,
            thresholds=thresholds_cfg,
            recipients=recipients_cfg,
            email=email_cfg,
        )

    # ----------------- conveniences -----------------

    @property
    def enabled_providers(self) -> List[str]:
        enabled: List[str] = []
        if self.app.providers.nws:
            enabled.append("nws")
        if self.app.providers.usgs:
            enabled.append("usgs")
        return enabled

    def require_email(self) -> None:
        """Raise if email is not configured. Call before sending notifications."""
        if not self.email.is_configured:
            raise RuntimeError(
                "Email is not configured. Set YAGMAIL_USER and YAGMAIL_APP_PASSWORD "
                "(e.g., in .env or your environment)."
            )
