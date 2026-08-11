"""
Random Image API — 主应用入口。

路由概览
────────
公开
  GET  /random              返回随机图片（支持 json/download/latest 参数）
  GET  /getImage/{hash}     按 SHA-256 哈希获取图片（永久链接）
  GET  /stats               图库概览统计

管理（需要 X-Admin-Key 请求头）
  POST   /admin/scan                    手动触发目录扫描
  POST   /admin/compute-hashes          批量计算缺失哈希
  GET    /admin/whitelist               获取白名单
  POST   /admin/whitelist               添加 IP 到白名单
  DELETE /admin/whitelist/{ip}          从白名单移除 IP
  GET    /admin/blacklist               获取黑名单
  POST   /admin/blacklist               添加 IP 到黑名单
  DELETE /admin/blacklist/{ip}          从黑名单移除 IP
  GET    /admin/folder-weights          获取所有目录权重
  PUT    /admin/folder-weights          设置目录权重
  DELETE /admin/folder-weights          删除目录权重
  GET    /admin/rate-limits             查看各 IP 限速状态
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from config import settings
from database import (
    add_ip_to_list,
    check_rate_limit,
    compute_all_missing_hashes,
    compute_image_hash,
    delete_folder_weight,
    get_db_dep,
    get_image_by_hash,
    get_folder_weight_preview,
    get_random_image,
    init_db,
    is_blacklisted,
    is_whitelisted,
    list_folder_weights,
    list_ips,
    record_access,
    remove_ip_from_list,
    set_folder_weight,
)
from models import IPRateLimit, Image
from scanner import scan_and_sync

# ──────────────────────────────────────────────
# 日志
# ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG if settings.app.debug else logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 应用生命周期
# ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 建表
    init_db()
    logger.info("数据库初始化完成")

    # 启动时扫描图库
    if settings.images.scan_on_startup:
        loop = asyncio.get_event_loop()
        from database import get_db as _get_db

        def _scan():
            with _get_db() as db:
                return scan_and_sync(db)

        result = await loop.run_in_executor(None, _scan)
        logger.info("启动扫描结果：%s", result)

    yield
    logger.info("应用已关闭")


# ──────────────────────────────────────────────
# FastAPI 实例
# ──────────────────────────────────────────────

app = FastAPI(
    title="Random Image API",
    description="随机图片接口，支持加权抽取、IP 限流、白/黑名单",
    version="1.0.0",
    lifespan=lifespan,
)


# ──────────────────────────────────────────────
# 中间件：IP 访问控制 + 速率限制
# ──────────────────────────────────────────────

@app.middleware("http")
async def access_control_middleware(request: Request, call_next):
    # 管理接口只做鉴权，不做限流检查
    if request.url.path.startswith("/admin"):
        return await call_next(request)

    client_ip: str = request.client.host if request.client else "unknown"
    loop = asyncio.get_event_loop()

    def _check() -> str | None:
        """同步检查，返回错误原因或 None（通过）。"""
        from database import get_db as _get_db
        with _get_db() as db:
            if is_blacklisted(db, client_ip):
                return "blacklisted"
            if is_whitelisted(db, client_ip):
                return None  # 白名单直接放行
            if settings.rate_limit.enabled:
                allowed = check_rate_limit(
                    db, client_ip, settings.rate_limit.requests_per_minute
                )
                if not allowed:
                    return "rate_limited"
        return None

    reason = await loop.run_in_executor(None, _check)

    if reason == "blacklisted":
        return JSONResponse(
            status_code=403,
            content={"detail": "您的 IP 已被封禁"},
        )
    if reason == "rate_limited":
        return JSONResponse(
            status_code=429,
            content={
                "detail": f"请求过于频繁，每分钟限 {settings.rate_limit.requests_per_minute} 次",
            },
        )

    return await call_next(request)


# ──────────────────────────────────────────────
# 管理鉴权依赖
# ──────────────────────────────────────────────

def verify_admin_key(x_admin_key: Annotated[str | None, Header()] = None):
    if x_admin_key is None or not secrets.compare_digest(
        x_admin_key, settings.security.admin_api_key
    ):
        raise HTTPException(status_code=401, detail="管理密钥无效")


# ──────────────────────────────────────────────
# Pydantic 请求 / 响应模型
# ──────────────────────────────────────────────

class IPEntry(BaseModel):
    ip: str
    note: str = ""


class FolderWeightEntry(BaseModel):
    folder_path: str
    weight: float = Field(default=1.0, gt=0)
    note: str = ""


# ──────────────────────────────────────────────
# 公开路由
# ──────────────────────────────────────────────

@app.get(
    "/random",
    summary="获取随机图片",
    responses={
        200: {"description": "图片文件或 JSON 信息"},
        404: {"description": "图库为空"},
    },
)
def random_image(
    latest: bool = Query(default=False, description="为 true 时只从 latest_dir 中抽取"),
    json: bool = Query(default=False, description="为 true 时返回 JSON（含哈希和永久链接）而非图片文件"),
    download: bool = Query(default=False, description="为 true 时触发浏览器下载；默认直接在浏览器内显示"),
    cache: bool = Query(default=False, description="是否允许浏览器缓存，默认为 false (no-cache)"),
    db: Session = Depends(get_db_dep),
):
    """随机返回图库中的一张图片。json=true 时返回哈希与永久链接。"""
    import os
    image: Image | None = get_random_image(db, latest=latest)
    if image is None:
        raise HTTPException(status_code=404, detail="图库为空或 latest 目录中没有图片")

    if not os.path.isfile(image.path):
        raise HTTPException(status_code=404, detail="文件已不存在于磁盘，请重新扫描")

    record_access(db, image)

    headers = {}
    if not cache:
        headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        headers["Pragma"] = "no-cache"

    if json:
        # 懒计算哈希
        sha = compute_image_hash(db, image)
        permalink = f"{settings.app.base_url.rstrip('/')}/getImage/{sha}"
        return JSONResponse(
            content={**_public_image_metadata(image), "hash": sha, "url": permalink},
            headers=headers
        )

    media_type = _guess_media_type(image.filename)
    disposition = "attachment" if download else "inline"
    headers["Content-Disposition"] = f'{disposition}; filename="{image.filename}"'

    return FileResponse(
        path=image.path,
        filename=image.filename,
        media_type=media_type,
        headers=headers,
    )


@app.get(
    "/getImage/{sha256}",
    summary="按哈希获取图片（永久链接）",
    responses={
        200: {"description": "图片文件"},
        404: {"description": "哈希不存在或文件已删除"},
    },
)
def get_image(
    sha256: str,
    download: bool = Query(default=False, description="为 true 时触发浏览器下载"),
    db: Session = Depends(get_db_dep),
):
    """通过 SHA-256 哈希直接获取图片，此为永久链接。"""
    import os
    image = get_image_by_hash(db, sha256)
    if image is None:
        raise HTTPException(status_code=404, detail="未找到对应哈希的图片")
    if not os.path.isfile(image.path):
        raise HTTPException(status_code=404, detail="图片文件已从磁盘删除")

    record_access(db, image)
    disposition = "attachment" if download else "inline"
    return FileResponse(
        path=image.path,
        filename=image.filename,
        media_type=_guess_media_type(image.filename),
        headers={"Content-Disposition": f'{disposition}; filename="{image.filename}"'},
    )


@app.get("/stats", summary="图库统计概览")
def stats(db: Session = Depends(get_db_dep)):
    from sqlalchemy import func as sqlfunc
    total_images = db.query(sqlfunc.count(Image.id)).scalar() or 0
    total_accesses = db.query(sqlfunc.sum(Image.access_count)).scalar() or 0
    folder_count = db.query(Image.folder).distinct().count()
    top5 = (
        db.query(Image)
        .order_by(Image.access_count.desc())
        .limit(5)
        .all()
    )
    return {
        "total_images": total_images,
        "total_folders": folder_count,
        "total_accesses": total_accesses,
        "top5_most_accessed": [_public_image_metadata(img) for img in top5],
    }


# ──────────────────────────────────────────────
# 管理路由
# ──────────────────────────────────────────────

@app.post(
    "/admin/scan",
    summary="手动触发图库扫描",
    dependencies=[Depends(verify_admin_key)],
)
def admin_scan(db: Session = Depends(get_db_dep)):
    result = scan_and_sync(db)
    return {"message": "扫描完成", **result}


@app.post(
    "/admin/compute-hashes",
    summary="批量计算缺失的 SHA-256 哈希",
    dependencies=[Depends(verify_admin_key)],
)
def admin_compute_hashes(db: Session = Depends(get_db_dep)):
    """对数据库中 sha256 为空的图片逐一计算哈希（可能较慢）。"""
    result = compute_all_missing_hashes(db)
    return {"message": "哈希计算完成", **result}


# ── 白名单 ──

@app.get(
    "/admin/whitelist",
    summary="查看白名单",
    dependencies=[Depends(verify_admin_key)],
)
def get_whitelist(db: Session = Depends(get_db_dep)):
    return [e.to_dict() for e in list_ips(db, "whitelist")]


@app.post(
    "/admin/whitelist",
    summary="添加 IP 到白名单",
    dependencies=[Depends(verify_admin_key)],
)
def add_whitelist(entry: IPEntry, db: Session = Depends(get_db_dep)):
    rec = add_ip_to_list(db, entry.ip, "whitelist", entry.note)
    return {"message": f"{entry.ip} 已加入白名单", "entry": rec.to_dict()}


@app.delete(
    "/admin/whitelist/{ip}",
    summary="从白名单移除 IP",
    dependencies=[Depends(verify_admin_key)],
)
def remove_whitelist(ip: str, db: Session = Depends(get_db_dep)):
    if not remove_ip_from_list(db, ip, "whitelist"):
        raise HTTPException(status_code=404, detail="IP 不在白名单中")
    return {"message": f"{ip} 已从白名单移除"}


# ── 黑名单 ──

@app.get(
    "/admin/blacklist",
    summary="查看黑名单",
    dependencies=[Depends(verify_admin_key)],
)
def get_blacklist(db: Session = Depends(get_db_dep)):
    return [e.to_dict() for e in list_ips(db, "blacklist")]


@app.post(
    "/admin/blacklist",
    summary="添加 IP 到黑名单",
    dependencies=[Depends(verify_admin_key)],
)
def add_blacklist(entry: IPEntry, db: Session = Depends(get_db_dep)):
    rec = add_ip_to_list(db, entry.ip, "blacklist", entry.note)
    return {"message": f"{entry.ip} 已加入黑名单", "entry": rec.to_dict()}


@app.delete(
    "/admin/blacklist/{ip}",
    summary="从黑名单移除 IP",
    dependencies=[Depends(verify_admin_key)],
)
def remove_blacklist(ip: str, db: Session = Depends(get_db_dep)):
    if not remove_ip_from_list(db, ip, "blacklist"):
        raise HTTPException(status_code=404, detail="IP 不在黑名单中")
    return {"message": f"{ip} 已从黑名单移除"}


# ── 目录权重 ──

@app.get(
    "/admin/folder-weights",
    summary="查看所有目录权重",
    dependencies=[Depends(verify_admin_key)],
)
def get_folder_weights(db: Session = Depends(get_db_dep)):
    return {
        "rules": [r.to_dict() for r in list_folder_weights(db)],
        "preview": get_folder_weight_preview(db),
        "semantics": "先按目录权重选择目录，再在目录内均匀抽图；未配置目录继承最近祖先，默认 1",
    }


@app.put(
    "/admin/folder-weights",
    summary="设置目录权重",
    dependencies=[Depends(verify_admin_key)],
)
def put_folder_weight(entry: FolderWeightEntry, db: Session = Depends(get_db_dep)):
    rec = set_folder_weight(db, entry.folder_path, entry.weight, entry.note)
    return {"message": "权重已更新", "entry": rec.to_dict()}


@app.delete(
    "/admin/folder-weights",
    summary="删除目录权重（恢复默认 1.0）",
    dependencies=[Depends(verify_admin_key)],
)
def del_folder_weight(
    folder_path: str = Query(..., description="目录绝对路径"),
    db: Session = Depends(get_db_dep),
):
    if not delete_folder_weight(db, folder_path):
        raise HTTPException(status_code=404, detail="未找到该目录的权重记录")
    return {"message": f"{folder_path} 的权重已删除，将使用默认值 1.0"}


# ── 限速状态 ──

@app.get(
    "/admin/rate-limits",
    summary="查看各 IP 的限速计数状态",
    dependencies=[Depends(verify_admin_key)],
)
def get_rate_limits(db: Session = Depends(get_db_dep)):
    records = db.query(IPRateLimit).order_by(IPRateLimit.request_count.desc()).all()
    return [
        {
            "ip": r.ip,
            "request_count": r.request_count,
            "window_start": r.window_start.isoformat() if r.window_start else None,
        }
        for r in records
    ]


# ──────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────

_MIME_MAP: dict[str, str] = {
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".bmp":  "image/bmp",
    ".tiff": "image/tiff",
    ".avif": "image/avif",
}


def _guess_media_type(filename: str) -> str:
    from pathlib import Path as _Path
    return _MIME_MAP.get(_Path(filename).suffix.lower(), "application/octet-stream")


def _public_image_metadata(image: Image) -> dict:
    """返回适合公开接口的字段，避免泄露服务器绝对路径。"""
    from pathlib import Path as _Path

    folder = _Path(image.folder)
    try:
        relative_folder = str(folder.relative_to(_Path(settings.images.image_dir)))
    except ValueError:
        relative_folder = folder.name
    return {
        "filename": image.filename,
        "folder": relative_folder,
        "file_size": image.file_size,
        "access_count": image.access_count,
    }


# ──────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────

def run():
    uvicorn.run(
        "main:app",
        host=settings.app.host,
        port=settings.app.port,
        reload=settings.app.debug,
    )


if __name__ == "__main__":
    run()
