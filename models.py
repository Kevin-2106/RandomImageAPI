"""
SQLAlchemy ORM 模型定义。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Image(Base):
    """图片记录表。"""

    __tablename__ = "images"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 磁盘上的绝对路径（唯一键）
    path = Column(String, unique=True, nullable=False, index=True)
    # 所在目录（用于分组加权）
    folder = Column(String, nullable=False, index=True)
    # 文件名
    filename = Column(String, nullable=False)
    # 文件大小（字节）
    file_size = Column(Integer, default=0)
    # 文件内容 SHA-256（懒计算，初始为 NULL）
    # 相同内容的文件拥有相同哈希，因此这里不能加唯一约束。
    sha256 = Column(String, nullable=True, index=True)
    # 累计被访问次数
    access_count = Column(Integer, default=0, nullable=False)
    # 首次入库时间
    first_seen = Column(DateTime, default=datetime.utcnow, nullable=False)
    # 最近一次被访问时间
    last_accessed = Column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "path": self.path,
            "folder": self.folder,
            "filename": self.filename,
            "file_size": self.file_size,
            "sha256": self.sha256,
            "access_count": self.access_count,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_accessed": self.last_accessed.isoformat() if self.last_accessed else None,
        }


class FolderWeight(Base):
    """目录抽取权重；未配置的目录继承最近祖先，默认值为 1。"""

    __tablename__ = "folder_weights"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 目录绝对路径
    folder_path = Column(String, unique=True, nullable=False, index=True)
    # 权重（正数，默认 1.0）
    weight = Column(Float, default=1.0, nullable=False)
    # 备注
    note = Column(String, default="")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "folder_path": self.folder_path,
            "weight": self.weight,
            "note": self.note,
        }


class IPRateLimit(Base):
    """每个 IP 的速率限制计数器（滑动时间窗口）。"""

    __tablename__ = "ip_rate_limits"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ip = Column(String, unique=True, nullable=False, index=True)
    # 当前窗口内的请求数
    request_count = Column(Integer, default=0, nullable=False)
    # 当前计数窗口的起始时间
    window_start = Column(DateTime, nullable=False)


class IPList(Base):
    """IP 白名单 / 黑名单。list_type: 'whitelist' | 'blacklist'。"""

    __tablename__ = "ip_lists"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ip = Column(String, nullable=False, index=True)
    list_type = Column(String, nullable=False)  # 'whitelist' or 'blacklist'
    note = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("ip", "list_type", name="uq_ip_list_type"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ip": self.ip,
            "list_type": self.list_type,
            "note": self.note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
