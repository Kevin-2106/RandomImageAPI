"""
图库目录扫描器：递归扫描指定目录，将图片路径同步到数据库。
- 新增  → 插入数据库
- 已存在 → 保持不变（保留访问计数）
- 已删除 → 从数据库移除
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from config import settings
from models import Image

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    settings.images.supported_extensions
)


def _collect_disk_images(image_dir: str) -> dict[str, Path]:
    """递归收集目录下所有支持格式的图片，返回 {str(path): Path} 字典。"""
    root = Path(image_dir)
    if not root.exists():
        logger.warning("图库目录不存在：%s", image_dir)
        return {}

    result: dict[str, Path] = {}
    for dirpath, _dirs, files in os.walk(root):
        for filename in files:
            ext = Path(filename).suffix.lower()
            if ext in SUPPORTED_EXTENSIONS:
                full = Path(dirpath) / filename
                result[str(full)] = full
    return result


def scan_and_sync(db: Session) -> dict:
    """
    扫描图库目录并与数据库同步。
    返回统计信息 dict：added / removed / unchanged / total。
    """
    image_dir = settings.images.image_dir
    logger.info("开始扫描图库目录：%s", image_dir)

    disk_images = _collect_disk_images(image_dir)
    disk_paths: set[str] = set(disk_images.keys())

    # 查询数据库中现有记录
    db_records: list[Image] = db.query(Image).all()
    db_path_map: dict[str, Image] = {img.path: img for img in db_records}
    db_paths: set[str] = set(db_path_map.keys())

    to_add: set[str] = disk_paths - db_paths
    to_remove: set[str] = db_paths - disk_paths
    unchanged_count: int = len(disk_paths & db_paths)

    # ── 新增 ──
    added_count = 0
    for path_str in to_add:
        p = disk_images[path_str]
        try:
            file_size = p.stat().st_size
        except OSError:
            file_size = 0

        img = Image(
            path=path_str,
            folder=str(p.parent),
            filename=p.name,
            file_size=file_size,
            access_count=0,
            first_seen=datetime.utcnow(),
        )
        db.add(img)
        added_count += 1

    # ── 删除 ──
    removed_count = 0
    for path_str in to_remove:
        db.delete(db_path_map[path_str])
        removed_count += 1

    db.commit()

    result = {
        "added": added_count,
        "removed": removed_count,
        "unchanged": unchanged_count,
        "total": len(disk_paths),
    }
    logger.info(
        "扫描完成 → 新增 %d，删除 %d，unchanged %d，共 %d 张",
        added_count,
        removed_count,
        unchanged_count,
        len(disk_paths),
    )
    return result
