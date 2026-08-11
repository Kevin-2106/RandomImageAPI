"""
数据库引擎、Session 工厂以及核心数据操作函数。
"""

from __future__ import annotations

import hashlib
import logging
import random
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, event, func
from sqlalchemy.orm import Session, sessionmaker

from config import settings
from models import Base, FolderWeight, Image, IPList, IPRateLimit

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 引擎 & Session 工厂
# ──────────────────────────────────────────────

def _build_engine():
    db_path = Path(settings.database.path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{db_path.resolve()}"
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False},
        # WAL 模式：提升并发读性能
        echo=settings.app.debug,
    )
    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    return engine


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def init_db() -> None:
    """创建所有表（如果不存在），并自动补全缺失列（迁移）。"""
    Base.metadata.create_all(bind=engine)
    _migrate()


def _migrate() -> None:
    """向已有表补充新列（幂等）。"""
    with engine.connect() as conn:
        # 检查 images 表是否缺少 sha256 列
        cols = [
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(images)").fetchall()
        ]
        if "sha256" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE images ADD COLUMN sha256 TEXT DEFAULT NULL"
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_images_sha256 ON images(sha256)"
            )
            logger.info("数据库迁移：images 表新增 sha256 列")
        # 旧版误将内容哈希设为唯一值；仅在确实需要时重建索引。
        indexes = {
            row[1]: bool(row[2])
            for row in conn.exec_driver_sql("PRAGMA index_list(images)").fetchall()
        }
        if indexes.get("ix_images_sha256") is True:
            conn.exec_driver_sql("DROP INDEX ix_images_sha256")
            conn.exec_driver_sql("CREATE INDEX ix_images_sha256 ON images(sha256)")
            conn.commit()
            logger.info("数据库迁移：sha256 索引允许重复内容")


@contextmanager
def get_db() -> Generator[Session, None, None]:
    """同步数据库 Session 上下文管理器。"""
    db: Session = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db_dep() -> Generator[Session, None, None]:
    """FastAPI Depends 专用：不自动 commit，由路由函数控制。"""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ──────────────────────────────────────────────
# IP 访问控制
# ──────────────────────────────────────────────

def is_blacklisted(db: Session, ip: str) -> bool:
    return (
        db.query(IPList)
        .filter(IPList.ip == ip, IPList.list_type == "blacklist")
        .first()
    ) is not None


def is_whitelisted(db: Session, ip: str) -> bool:
    return (
        db.query(IPList)
        .filter(IPList.ip == ip, IPList.list_type == "whitelist")
        .first()
    ) is not None


def check_rate_limit(db: Session, ip: str, limit: int) -> bool:
    """
    检查 IP 是否超出速率限制。
    返回 True 表示允许（未超限），False 表示拒绝。
    每次调用会更新计数器。
    """
    now = datetime.utcnow()
    window_seconds = 60  # 1 分钟窗口

    record: IPRateLimit | None = (
        db.query(IPRateLimit).filter(IPRateLimit.ip == ip).first()
    )

    if record is None:
        # 首次请求，建立记录
        record = IPRateLimit(ip=ip, request_count=1, window_start=now)
        db.add(record)
        db.commit()
        return True

    elapsed = (now - record.window_start).total_seconds()
    if elapsed >= window_seconds:
        # 窗口已过期，重置
        record.window_start = now
        record.request_count = 1
        db.commit()
        return True

    if record.request_count >= limit:
        # 超限
        return False

    record.request_count += 1
    db.commit()
    return True


# ──────────────────────────────────────────────
# 随机图片选取
# ──────────────────────────────────────────────

def compute_folder_effective_weight(
    folder_path: str,
    weight_map: dict[str, float],
) -> float:
    """
    返回当前目录或最近已配置祖先的权重，找不到时为 1.0。

    子目录的显式配置会覆盖继承值，而不是和祖先相乘。这使配置值可以
    直接理解为目录之间的相对抽取机会。
    """
    p = Path(folder_path)
    while True:
        key = str(p)
        if key in weight_map:
            return weight_map[key]
        if p.parent == p:
            return 1.0
        p = p.parent


def _load_weight_map(db: Session) -> dict[str, float]:
    """从数据库加载所有已配置的文件夹权重，返回 {folder_path: weight} 字典。"""
    return {r.folder_path: r.weight for r in db.query(FolderWeight).all()}


def get_random_image(db: Session, latest: bool = False) -> Image | None:
    """
    先按目录权重选择目录，再在目录内均匀抽取一张图片。
    latest=True 时只从 latest_dir 下的图片中抽取（不参与加权，直接均匀随机）。
    """
    latest_dir = settings.images.latest_dir.rstrip("/") + "/"

    if latest:
        # latest 模式：在 latest_dir 及其子目录中均匀随机
        q_sub = db.query(Image).filter(Image.folder.like(latest_dir + "%"))
        q_exact = db.query(Image).filter(Image.folder == settings.images.latest_dir.rstrip("/"))
        seen: set[int] = set()
        unique: list[Image] = []
        for img in q_sub.all() + q_exact.all():
            if img.id not in seen:
                seen.add(img.id)
                unique.append(img)
        return random.choice(unique) if unique else None

    # 获取所有目录及其图片数
    folder_rows = (
        db.query(Image.folder, func.count(Image.id).label("cnt"))
        .group_by(Image.folder)
        .all()
    )
    if not folder_rows:
        return None

    weight_map = _load_weight_map(db)

    folders: list[str] = []
    weights: list[float] = []
    for row in folder_rows:
        effective = compute_folder_effective_weight(row.folder, weight_map)
        folders.append(row.folder)
        weights.append(effective)

    # 加权随机选择目录
    (selected_folder,) = random.choices(folders, weights=weights, k=1)

    # 从该目录随机选取一张图片
    images = db.query(Image).filter(Image.folder == selected_folder).all()
    return random.choice(images) if images else None


def get_folder_weight_preview(db: Session) -> list[dict]:
    """列出每个含图目录的有效权重和理论目录命中概率。"""
    rows = (
        db.query(Image.folder, func.count(Image.id).label("image_count"))
        .group_by(Image.folder)
        .order_by(Image.folder)
        .all()
    )
    weight_map = _load_weight_map(db)
    values = [compute_folder_effective_weight(r.folder, weight_map) for r in rows]
    total = sum(values)
    return [
        {
            "folder_path": row.folder,
            "image_count": row.image_count,
            "configured_weight": weight_map.get(row.folder),
            "effective_weight": weight,
            "folder_probability": weight / total if total else 0.0,
            "image_probability": weight / total / row.image_count if total else 0.0,
        }
        for row, weight in zip(rows, values, strict=True)
    ]


def record_access(db: Session, image: Image) -> None:
    """更新图片的访问计数和最后访问时间。"""
    image.access_count = (image.access_count or 0) + 1
    image.last_accessed = datetime.utcnow()
    db.commit()


# ──────────────────────────────────────────────
# 哈希计算
# ──────────────────────────────────────────────

_CHUNK = 1 << 20  # 1 MiB


def _sha256_file(path: str) -> str:
    """计算文件的 SHA-256 十六进制摘要。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def compute_image_hash(db: Session, image: Image) -> str:
    """
    确保 image.sha256 已计算（懒计算）。相同内容会得到相同哈希。
    """
    if image.sha256:
        return image.sha256

    image.sha256 = _sha256_file(image.path)
    db.commit()
    return image.sha256


def compute_all_missing_hashes(db: Session) -> dict:
    """批量计算所有 sha256 为 NULL 的图片哈希，返回统计。"""
    images = db.query(Image).filter(Image.sha256.is_(None)).all()
    total = len(images)
    done = 0
    errors = 0
    for img in images:
        try:
            compute_image_hash(db, img)
            done += 1
        except OSError as e:
            logger.warning("计算哈希失败 %s: %s", img.path, e)
            errors += 1
    return {"total": total, "computed": done, "errors": errors}


def get_image_by_hash(db: Session, sha256: str) -> Image | None:
    """按 SHA-256 查找图片记录。"""
    return db.query(Image).filter(Image.sha256 == sha256).first()


# ──────────────────────────────────────────────
# IP 名单管理
# ──────────────────────────────────────────────

def add_ip_to_list(db: Session, ip: str, list_type: str, note: str = "") -> IPList:
    existing = (
        db.query(IPList)
        .filter(IPList.ip == ip, IPList.list_type == list_type)
        .first()
    )
    if existing:
        existing.note = note
        db.commit()
        return existing
    entry = IPList(ip=ip, list_type=list_type, note=note, created_at=datetime.utcnow())
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def remove_ip_from_list(db: Session, ip: str, list_type: str) -> bool:
    entry = (
        db.query(IPList)
        .filter(IPList.ip == ip, IPList.list_type == list_type)
        .first()
    )
    if entry:
        db.delete(entry)
        db.commit()
        return True
    return False


def list_ips(db: Session, list_type: str) -> list[IPList]:
    return db.query(IPList).filter(IPList.list_type == list_type).all()


# ──────────────────────────────────────────────
# 目录权重管理
# ──────────────────────────────────────────────

def set_folder_weight(
    db: Session, folder_path: str, weight: float, note: str = ""
) -> FolderWeight:
    folder_path = str(Path(folder_path).expanduser().resolve())
    if weight <= 0:
        raise ValueError("weight must be greater than zero")
    record = (
        db.query(FolderWeight)
        .filter(FolderWeight.folder_path == folder_path)
        .first()
    )
    if record:
        record.weight = weight
        record.note = note
        db.commit()
        return record
    record = FolderWeight(folder_path=folder_path, weight=weight, note=note)
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def list_folder_weights(db: Session) -> list[FolderWeight]:
    return db.query(FolderWeight).order_by(FolderWeight.folder_path).all()


def delete_folder_weight(db: Session, folder_path: str) -> bool:
    folder_path = str(Path(folder_path).expanduser().resolve())
    record = (
        db.query(FolderWeight)
        .filter(FolderWeight.folder_path == folder_path)
        .first()
    )
    if record:
        db.delete(record)
        db.commit()
        return True
    return False
