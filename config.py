"""
应用配置模块，从 config.toml 读取，缺失字段自动回退到默认值。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = Path(os.environ.get("RANDOM_IMAGE_CONFIG", PROJECT_DIR / "config.toml"))


@dataclass
class AppConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    # 用于构造永久链接，例如 https://img.example.com
    base_url: str = "http://localhost:8000"


@dataclass
class ImagesConfig:
    image_dir: str = "/mnt/Data/Nekotail/ImgBed/"
    latest_dir: str = "/mnt/Data/Nekotail/ImgBed/latest/"
    supported_extensions: list[str] = field(
        default_factory=lambda: [
            ".jpg", ".jpeg", ".png", ".gif",
            ".webp", ".bmp", ".tiff", ".avif",
        ]
    )
    scan_on_startup: bool = True


@dataclass
class DatabaseConfig:
    path: str = "data/imageapi.db"


@dataclass
class RateLimitConfig:
    enabled: bool = True
    requests_per_minute: int = 60


@dataclass
class SecurityConfig:
    admin_api_key: str = "change-me-please"


@dataclass
class Settings:
    app: AppConfig = field(default_factory=AppConfig)
    images: ImagesConfig = field(default_factory=ImagesConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)


def _apply(target: object, data: dict) -> None:
    """将字典中的值赋给 dataclass 实例（只更新已有字段）。"""
    for k, v in data.items():
        if hasattr(target, k):
            setattr(target, k, v)


def load_settings() -> Settings:
    s = Settings()
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "rb") as f:
            data = tomllib.load(f)

        _apply(s.app, data.get("app", {}))
        _apply(s.images, data.get("images", {}))
        _apply(s.database, data.get("database", {}))
        _apply(s.rate_limit, data.get("rate_limit", {}))
        _apply(s.security, data.get("security", {}))
    # 密钥适合通过部署环境注入，避免写入配置文件或 Git。
    if admin_key := os.environ.get("RANDOM_IMAGE_ADMIN_KEY"):
        s.security.admin_api_key = admin_key
    if database_path := os.environ.get("RANDOM_IMAGE_DATABASE"):
        s.database.path = database_path
    db_path = Path(s.database.path).expanduser()
    if not db_path.is_absolute():
        db_path = PROJECT_DIR / db_path
    s.database.path = str(db_path.resolve())
    return s


# 全局单例
settings: Settings = load_settings()
