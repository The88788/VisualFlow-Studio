"""Configuration manager for VeoAPI."""

import os
from pathlib import Path
from typing import Any, Optional

import tomli

BASE_DIR = Path(__file__).parent.parent.parent
CONFIG_PATH = BASE_DIR / "config" / "setting.toml"
DATA_DIR = BASE_DIR / "data"
IMAGES_DIR = DATA_DIR / "images"
# Use ASCII-only path for TMP_DIR to avoid FFmpeg issues with Cyrillic paths
TMP_DIR = Path("C:/veoapi_tmp") if os.name == "nt" else DATA_DIR / "tmp"
DB_PATH = DATA_DIR / "veoapi.db"


class Config:
    """Configuration manager with TOML file support."""

    _instance: Optional["Config"] = None
    _config: dict[str, Any] = {}

    def __new__(cls) -> "Config":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load_config()
        return cls._instance

    def _load_config(self) -> None:
        """Load configuration from TOML file."""
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "rb") as f:
                self._config = tomli.load(f)
        else:
            self._config = {}

    def reload(self) -> None:
        """Reload configuration from file."""
        self._load_config()

    def get(self, section: str, key: str, default: Any = None) -> Any:
        """Get a configuration value."""
        return self._config.get(section, {}).get(key, default)

    def _env_or_toml(self, env_key: str, section: str, key: str, default: Any = None) -> Any:
        """Get value from ENV variable first, fall back to TOML config."""
        env_val = os.environ.get(env_key)
        if env_val is not None:
            return env_val
        return self.get(section, key, default)

    @property
    def capsolver_api_key(self) -> str:
        return self._env_or_toml("VEOAPI_CAPSOLVER_API_KEY", "capsolver", "api_key", "")

    @property
    def server_host(self) -> str:
        return self.get("server", "host", "0.0.0.0")

    @property
    def server_port(self) -> int:
        return self.get("server", "port", 4000)

    @property
    def video_timeout(self) -> int:
        return self.get("generation", "video_timeout", 1800)

    @property
    def poll_interval(self) -> int:
        return self.get("generation", "poll_interval", 5)

    @property
    def max_concurrent_videos(self) -> int:
        return self.get("generation", "max_concurrent_videos", 10)

    # ---- Browser provider (browser2api fork) ----

    @property
    def browser_video_timeout(self) -> int:
        """Max seconds to wait for a video to appear in the Flow UI.

        Default is 15 minutes: long enough for real Flow renders, still short
        enough to eventually release a genuinely stuck account.
        Override via ``[browser].video_timeout`` or ``VEOAPI_BROWSER_VIDEO_TIMEOUT``.
        """
        val = self._env_or_toml(
            "VEOAPI_BROWSER_VIDEO_TIMEOUT", "browser", "video_timeout", 900
        )
        try:
            return max(60, int(val))
        except (TypeError, ValueError):
            return 900

    @property
    def provider_mode(self) -> str:
        """Generation backend: ``veo`` (legacy), ``browser`` or ``gflow``."""
        val = self._env_or_toml("VEOAPI_PROVIDER_MODE", "generation", "provider_mode", "veo")
        return str(val).lower().strip() or "veo"

    # ---- flow.google.com provider (gflow CLI) ----

    @property
    def gflow_executable(self) -> str:
        return str(
            self._env_or_toml(
                "VEOAPI_GFLOW_EXECUTABLE", "gflow", "executable", "/usr/local/bin/gflow"
            )
        )

    @property
    def gflow_home(self) -> str:
        return str(
            self._env_or_toml(
                "GFLOW_CLI_HOME", "gflow", "home", "/app/gflow_profiles"
            )
        )

    @property
    def gflow_profile(self) -> str:
        return str(
            self._env_or_toml("GFLOW_CLI_PROFILE", "gflow", "profile", "client")
        ).strip()

    @property
    def gflow_project_id(self) -> Optional[str]:
        value = self._env_or_toml("VEOAPI_GFLOW_PROJECT_ID", "gflow", "project_id", None)
        return str(value).strip() if value else None

    @property
    def gflow_timeout(self) -> int:
        value = self._env_or_toml("VEOAPI_GFLOW_TIMEOUT", "gflow", "timeout", 1200)
        try:
            return max(60, int(value))
        except (TypeError, ValueError):
            return 1200

    @property
    def browser_concurrency_per_account(self) -> int:
        """Max parallel browser tabs (= parallel generations) per account.

        Temporarily pinned to 1 while the browser path is being stabilised —
        one Chrome-tab-per-account = one generation at a time. Override via
        ``[browser].concurrency_per_account`` in setting.toml or
        ``VEOAPI_BROWSER_CONCURRENCY`` env var when we're ready to scale
        back up.
        """
        val = self._env_or_toml(
            "VEOAPI_BROWSER_CONCURRENCY", "browser", "concurrency_per_account", 1
        )
        try:
            return max(1, int(val))
        except (TypeError, ValueError):
            return 1

    @property
    def browser_profile_suffix(self) -> str:
        """Suffix appended to account.profile_path to locate the browser profile dir.
        Matches the legacy setup_puppeteer.js convention: ``{profile_path}_puppeteer``.
        """
        return self._env_or_toml(
            "VEOAPI_BROWSER_PROFILE_SUFFIX", "browser", "profile_suffix", "_puppeteer"
        )

    @property
    def browser_idle_shutdown_minutes(self) -> int:
        """Close a per-account Chrome session after N minutes of inactivity."""
        val = self._env_or_toml(
            "VEOAPI_BROWSER_IDLE_MIN", "browser", "idle_shutdown_minutes", 15
        )
        try:
            return max(1, int(val))
        except (TypeError, ValueError):
            return 15

    @property
    def browser_keep_artifacts(self) -> bool:
        """If True, do not auto-delete per-video browser artifacts.

        Controls cleanup of:
        * ``TMP_DIR/browser_videos/<video_id>/`` (input images, local mp4, failure dumps)
        * ``logs/browser_screenshots/<video_id>/`` (step screenshots)

        Default False — artifacts are removed right after generation finishes
        (success or terminal failure). Set to True when debugging to keep the
        forensic trail. Override via ``[browser].keep_artifacts`` or
        ``VEOAPI_BROWSER_KEEP_ARTIFACTS`` env.
        """
        val = self._env_or_toml(
            "VEOAPI_BROWSER_KEEP_ARTIFACTS", "browser", "keep_artifacts", False
        )
        if isinstance(val, str):
            return val.strip().lower() in ("true", "1", "yes", "on")
        return bool(val)

    @property
    def browser_artifacts_max_age_hours(self) -> int:
        """Max age (hours) for leftover browser artifact dirs before the
        periodic sweep removes them. Acts as a safety net for dirs whose
        in-process cleanup didn't run (crash, SIGKILL, keep_artifacts=True).
        """
        val = self._env_or_toml(
            "VEOAPI_BROWSER_ARTIFACTS_MAX_AGE_HOURS",
            "browser",
            "artifacts_max_age_hours",
            1,
        )
        try:
            return max(1, int(val))
        except (TypeError, ValueError):
            return 1

    @property
    def browser_profile_root(self) -> Optional[str]:
        """Optional override for where browser profiles live (e.g. ``C:/veoapi_browser_profiles``
        to avoid Cyrillic paths on Windows). If unset, falls back to ``{account.profile_path}{suffix}``.
        """
        return self._env_or_toml(
            "VEOAPI_BROWSER_PROFILE_ROOT", "browser", "profile_root", None
        )

    # ---- CloakBrowser (stealth Chromium) ----

    def _bool(self, val: Any, default: bool) -> bool:
        if val is None:
            return default
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes", "on")
        return bool(val)

    @property
    def use_cloakbrowser(self) -> bool:
        """When True, BrowserSessionPool launches via cloakbrowser's stealth
        Chromium binary instead of browser2api's BrowserManager (CDP+system Chrome).

        Default is **False**: empirical testing on this project's accounts
        showed CloakBrowser *increased* the rate of unusual-activity flags
        in long-running browser sessions vs. plain system Chrome — likely
        because the geoip-derived timezone (e.g. Indian/Mahe) clashes with
        the account's residency history, and the binary's distinctive
        fingerprint is now itself a detection signal.

        Keep the integration code around for future experiments — flip to
        true via ``[browser] use_cloakbrowser = true`` or
        ``VEOAPI_USE_CLOAKBROWSER=1`` to re-test.
        """
        return self._bool(
            self._env_or_toml("VEOAPI_USE_CLOAKBROWSER", "browser", "use_cloakbrowser", False),
            False,
        )

    @property
    def cloak_humanize(self) -> bool:
        """``humanize=True`` for CloakBrowser: bezier mouse curves + realistic
        typing/scroll. Slows clicks (fractions of a second) but is the main
        behavioural anti-detect feature."""
        return self._bool(
            self._env_or_toml("VEOAPI_CLOAK_HUMANIZE", "browser", "cloak_humanize", True),
            True,
        )

    @property
    def cloak_geoip(self) -> bool:
        """``geoip=True`` for CloakBrowser: auto timezone/locale + WebRTC IP
        spoofing from the proxy's exit IP. Issues one HTTP call through the
        proxy at context launch."""
        return self._bool(
            self._env_or_toml("VEOAPI_CLOAK_GEOIP", "browser", "cloak_geoip", True),
            True,
        )

    @property
    def captcha_use_cloakbrowser(self) -> bool:
        """Use CloakBrowser binary inside ``scripts/browser_captcha.js`` for
        reCAPTCHA solving. Different cost/benefit than the browser provider:
        each session is ≤10s (token + close), so accumulated fingerprint
        risk is negligible, while the stealth patches significantly improve
        reCAPTCHA v3 scoring."""
        return self._bool(
            self._env_or_toml("VEOAPI_CAPTCHA_USE_CLOAKBROWSER", "captcha", "use_cloakbrowser", True),
            True,
        )

    @property
    def proxy_pool_path(self) -> str:
        return self._env_or_toml(
            "VEOAPI_PROXY_POOL_PATH", "proxy", "pool_path",
            "/opt/gemini-api/config/proxy_pool.txt",
        )

    @property
    def proxy_enabled(self) -> bool:
        return self.get("proxy", "enabled", False)

    @property
    def proxy_url(self) -> str:
        return self._env_or_toml("VEOAPI_PROXY_URL", "proxy", "url", "")

    # ---- Proxy rotation (browser mode: swap burnt ISP proxies when Google
    # flags the active set with unusual activity) ----

    @property
    def proxy_rotation_enabled(self) -> bool:
        val = self._env_or_toml(
            "VEOAPI_PROXY_ROTATION_ENABLED", "proxy_rotation", "enabled", False
        )
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return bool(val)

    @property
    def proxy_rotation_cooldown_hours(self) -> float:
        """How long a burnt proxy rests before it can be reused."""
        return float(self.get("proxy_rotation", "cooldown_hours", 24))

    @property
    def proxy_rotation_rest_window_hours(self) -> float:
        """Minimum idle-since-last-generation before a proxy is 'fresh'."""
        return float(self.get("proxy_rotation", "rest_window_hours", 24))

    @property
    def proxy_rotation_trigger_threshold(self) -> float:
        """Fraction of participating accounts that must be flagged before a
        rotation fires. 1.0 = all of them (strict)."""
        return float(self.get("proxy_rotation", "trigger_threshold", 1.0))

    @property
    def proxy_rotation_min_minutes_between(self) -> float:
        """Anti-flapping: minimum minutes between two rotations."""
        return float(self.get("proxy_rotation", "min_minutes_between_rotations", 30))

    @property
    def proxy_rotation_reauth_throttle_seconds(self) -> float:
        """Delay between per-account re-auths after a rotation (avoid hammering
        Google with simultaneous logins)."""
        return float(self.get("proxy_rotation", "reauth_throttle_seconds", 30))

    @property
    def cleanup_max_age_days(self) -> int:
        return self.get("cleanup", "max_age_days", 60)

    @property
    def cleanup_tmp_max_hours(self) -> int:
        """Max age for tmp files in hours."""
        return self.get("cleanup", "tmp_max_hours", 6)

    @property
    def cleanup_log_max_mb(self) -> int:
        """Max total log size in MB."""
        return self.get("cleanup", "log_max_mb", 10)

    @property
    def yandex_translate_api_key(self) -> str:
        return self._env_or_toml("VEOAPI_YANDEX_TRANSLATE_API_KEY", "yandex_translate", "api_key", "")

    @property
    def yandex_translate_folder_id(self) -> str:
        return self._env_or_toml("VEOAPI_YANDEX_TRANSLATE_FOLDER_ID", "yandex_translate", "folder_id", "")

    # S3 Storage
    @property
    def s3_enabled(self) -> bool:
        val = self._env_or_toml("VEOAPI_S3_ENABLED", "s3", "enabled", False)
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes")
        return bool(val)

    @property
    def s3_bucket(self) -> str:
        return self._env_or_toml("VEOAPI_S3_BUCKET", "s3", "bucket", "")

    @property
    def s3_endpoint_url(self) -> str:
        return self._env_or_toml("VEOAPI_S3_ENDPOINT_URL", "s3", "endpoint_url", "")

    @property
    def s3_access_key(self) -> str:
        return self._env_or_toml("VEOAPI_S3_ACCESS_KEY", "s3", "access_key", "")

    @property
    def s3_secret_key(self) -> str:
        return self._env_or_toml("VEOAPI_S3_SECRET_KEY", "s3", "secret_key", "")

    @property
    def s3_region(self) -> str:
        return self._env_or_toml("VEOAPI_S3_REGION", "s3", "region", "us-east-1")

    @property
    def ip_whitelist_enabled(self) -> bool:
        return self.get("ip_whitelist", "enabled", False)

    @property
    def ip_whitelist_ips(self) -> list[str]:
        return self.get("ip_whitelist", "ips", [])


# Ensure directories exist
DATA_DIR.mkdir(exist_ok=True)
IMAGES_DIR.mkdir(exist_ok=True)
TMP_DIR.mkdir(exist_ok=True)

config = Config()
