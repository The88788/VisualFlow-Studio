"""Configuration manager for ImageAPI."""

import os
from pathlib import Path
from typing import Any, Optional

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "image_setting.toml"
DATA_DIR = Path(__file__).parent.parent.parent / "data" / "imageapi"
DATA_DIR.mkdir(parents=True, exist_ok=True)


class Config:
    """Singleton configuration with TOML + ENV override."""

    _instance: Optional["Config"] = None
    _config: dict[str, Any] = {}

    def __new__(cls) -> "Config":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load_config()
        return cls._instance

    def _load_config(self) -> None:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "rb") as f:
                self._config = tomllib.load(f)

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self._config.get(section, {}).get(key, default)

    def _env_or_toml(self, env_key: str, section: str, key: str, default: Any = None) -> Any:
        env_val = os.environ.get(env_key)
        if env_val is not None:
            return env_val
        return self.get(section, key, default)

    # --- Server ---
    @property
    def server_host(self) -> str:
        return self._env_or_toml("IMAGEAPI_HOST", "server", "host", "0.0.0.0")

    @property
    def server_port(self) -> int:
        return int(self._env_or_toml("IMAGEAPI_PORT", "server", "port", 4100))

    # --- Generation ---
    @property
    def max_concurrent_tasks(self) -> int:
        return int(self.get("generation", "max_concurrent_tasks", 3))

    @property
    def max_queue_size(self) -> int:
        return int(self.get("generation", "max_queue_size", 100))

    @property
    def generation_timeout(self) -> int:
        return int(self.get("generation", "timeout", 120))

    # --- Proxy ---
    @property
    def proxy_enabled(self) -> bool:
        val = self._env_or_toml("IMAGEAPI_PROXY_ENABLED", "proxy", "enabled", False)
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes")
        return bool(val)

    @property
    def proxy_url(self) -> str:
        return self._env_or_toml("IMAGEAPI_PROXY_URL", "proxy", "url", "")

    # --- Capsolver ---
    @property
    def capsolver_api_key(self) -> str:
        return self._env_or_toml("IMAGEAPI_CAPSOLVER_API_KEY", "capsolver", "api_key", "")

    # --- Cleanup ---
    @property
    def cleanup_max_age_days(self) -> int:
        return int(self.get("cleanup", "max_age_days", 1))

    @property
    def results_ttl_minutes(self) -> int:
        return int(self.get("cleanup", "results_ttl_minutes", 30))

    # --- S3 ---
    @property
    def s3_enabled(self) -> bool:
        val = self._env_or_toml("IMAGEAPI_S3_ENABLED", "s3", "enabled", False)
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes")
        return bool(val)

    @property
    def s3_bucket(self) -> str:
        return self._env_or_toml("IMAGEAPI_S3_BUCKET", "s3", "bucket", "")

    @property
    def s3_endpoint_url(self) -> str:
        return self._env_or_toml("IMAGEAPI_S3_ENDPOINT_URL", "s3", "endpoint_url", "")

    @property
    def s3_access_key(self) -> str:
        return self._env_or_toml("IMAGEAPI_S3_ACCESS_KEY", "s3", "access_key", "")

    @property
    def s3_secret_key(self) -> str:
        return self._env_or_toml("IMAGEAPI_S3_SECRET_KEY", "s3", "secret_key", "")

    @property
    def s3_region(self) -> str:
        return self._env_or_toml("IMAGEAPI_S3_REGION", "s3", "region", "")

    @property
    def s3_public_url_base(self) -> str:
        """Optional override for public URL base, e.g. https://cdn.example.com/images."""
        return self._env_or_toml("IMAGEAPI_S3_PUBLIC_URL_BASE", "s3", "public_url_base", "")

    # --- IP Whitelist ---
    @property
    def ip_whitelist_enabled(self) -> bool:
        val = self.get("ip_whitelist", "enabled", False)
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes")
        return bool(val)

    @property
    def ip_whitelist_ips(self) -> list[str]:
        return self.get("ip_whitelist", "ips", [])

    # --- Yandex Translate ---
    @property
    def yandex_translate_api_key(self) -> str:
        return self._env_or_toml(
            "IMAGEAPI_YANDEX_TRANSLATE_API_KEY", "yandex_translate", "api_key", ""
        )

    @property
    def yandex_translate_folder_id(self) -> str:
        return self._env_or_toml(
            "IMAGEAPI_YANDEX_TRANSLATE_FOLDER_ID", "yandex_translate", "folder_id", ""
        )


config = Config()
