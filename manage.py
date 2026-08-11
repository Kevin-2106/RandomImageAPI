#!/usr/bin/env python3
"""
RandomImageAPI 管理脚本
用法：python manage.py <command> [options]

命令列表
────────
  scan                    扫描图库目录，同步数据库
  hash-compute            批量计算缺失的 SHA-256 哈希
  stats                   显示图库统计信息

  whitelist list          列出白名单
  whitelist add <ip>      添加 IP 到白名单
  whitelist del <ip>      从白名单移除 IP

  blacklist list          列出黑名单
  blacklist add <ip>      添加 IP 到黑名单
  blacklist del <ip>      从黑名单移除 IP

  weight list             列出所有目录权重
  weight set <dir> <w>    设置目录权重（浮点数）
  weight del <dir>        删除目录权重（恢复默认 1.0）

  rate list               列出各 IP 限速状态
  rate reset <ip>         重置指定 IP 的限速计数器
  rate reset-all          重置所有 IP 的限速计数器

  image info <hash>       查看指定哈希对应的图片信息
  image top [N]           显示访问次数前 N 名（默认 10）
"""

from __future__ import annotations

import sys
from textwrap import indent

# 确保当前目录在 path 里
sys.path.insert(0, __file__.rsplit("/", 1)[0] if "/" in __file__ else ".")

from database import (
    compute_all_missing_hashes,
    get_db,
    get_image_by_hash,
    init_db,
    set_folder_weight,
    delete_folder_weight,
    list_folder_weights,
    add_ip_to_list,
    remove_ip_from_list,
    list_ips,
    get_folder_weight_preview,
)
from models import Image, IPRateLimit
from scanner import scan_and_sync


# ──────────────────────────────────────────────
# 工具
# ──────────────────────────────────────────────

def _hr(char="─", width=60) -> str:
    return char * width


def _ok(msg: str):
    print(f"  ✓  {msg}")


def _err(msg: str):
    print(f"  ✗  {msg}", file=sys.stderr)


# ──────────────────────────────────────────────
# 命令实现
# ──────────────────────────────────────────────

def cmd_scan(_args: list[str]):
    print("扫描图库目录中…")
    with get_db() as db:
        result = scan_and_sync(db)
    print(_hr())
    print(f"  新增：{result['added']}")
    print(f"  删除：{result['removed']}")
    print(f"  不变：{result['unchanged']}")
    print(f"  共计：{result['total']}")
    print(_hr())


def cmd_hash_compute(_args: list[str]):
    print("批量计算缺失哈希（可能较慢）…")
    with get_db() as db:
        result = compute_all_missing_hashes(db)
    print(_hr())
    print(f"  待计算：{result['total']}")
    print(f"  成功：  {result['computed']}")
    print(f"  失败：  {result['errors']}")
    print(_hr())


def cmd_stats(_args: list[str]):
    from sqlalchemy import func as sqlfunc
    with get_db() as db:
        total_images   = db.query(sqlfunc.count(Image.id)).scalar() or 0
        total_accesses = db.query(sqlfunc.sum(Image.access_count)).scalar() or 0
        folder_count   = db.query(Image.folder).distinct().count()
        hashed_count   = db.query(Image).filter(Image.sha256.isnot(None)).count()
        top5 = (
            db.query(Image)
            .order_by(Image.access_count.desc())
            .limit(5)
            .all()
        )
        print(_hr())
        print(f"  总图片数：{total_images}（已哈希 {hashed_count}，未哈希 {total_images - hashed_count}）")
        print(f"  总目录数：{folder_count}")
        print(f"  总访问数：{total_accesses}")
        print("  访问次数 Top 5：")
        for img in top5:
            print(f"    [{img.access_count:>6}] {img.filename}  ({img.sha256 or 'no-hash'})")
        print(_hr())


def cmd_whitelist(args: list[str]):
    _ip_list_cmd("whitelist", args)


def cmd_blacklist(args: list[str]):
    _ip_list_cmd("blacklist", args)


def _ip_list_cmd(list_type: str, args: list[str]):
    sub = args[0] if args else "list"
    if sub == "list":
        with get_db() as db:
            entries = list_ips(db, list_type)
            if not entries:
                print(f"  （{list_type} 为空）")
                return
            for e in entries:
                note = f"  # {e.note}" if e.note else ""
                print(f"  {e.ip}{note}")
    elif sub == "add":
        if len(args) < 2:
            _err("用法：add <ip> [备注]")
            return
        ip = args[1]
        note = " ".join(args[2:])
        with get_db() as db:
            add_ip_to_list(db, ip, list_type, note)
        _ok(f"{ip} 已加入 {list_type}")
    elif sub == "del":
        if len(args) < 2:
            _err("用法：del <ip>")
            return
        ip = args[1]
        with get_db() as db:
            ok = remove_ip_from_list(db, ip, list_type)
        if ok:
            _ok(f"{ip} 已从 {list_type} 移除")
        else:
            _err(f"{ip} 不在 {list_type} 中")
    else:
        _err(f"未知子命令：{sub}")


def cmd_weight(args: list[str]):
    sub = args[0] if args else "list"
    if sub == "list":
        with get_db() as db:
            preview = get_folder_weight_preview(db)
            print(f"  {'配置':>7} {'有效':>7} {'目录概率':>9} {'图片数':>7} 目录路径")
            print("  " + "─" * 90)
            for row in preview:
                configured = row["configured_weight"]
                configured_text = f"{configured:.2f}" if configured is not None else "继承"
                print(
                    f"  {configured_text:>7} {row['effective_weight']:>7.2f} "
                    f"{row['folder_probability']:>8.2%} {row['image_count']:>7} "
                    f"{row['folder_path']}"
                )

    elif sub == "set":
        if len(args) < 3:
            _err("用法：weight set <目录路径> <权重>")
            return
        folder_raw, w_str = args[1], args[2]
        try:
            w = float(w_str)
            assert w > 0
        except (ValueError, AssertionError):
            _err("权重必须为正数")
            return

        # 路径归一化：去除末尾斜杠，展开 ~，确保与 scanner.py 中存储的格式一致
        from pathlib import Path
        folder = str(Path(folder_raw).resolve())

        note = " ".join(args[3:])
        with get_db() as db:
            set_folder_weight(db, folder, w, note)
        _ok(f"已设置 {folder} 权重为 {w}")
    elif sub == "del":
        if len(args) < 2:
            _err("用法：weight del <目录路径>")
            return
        folder_raw = args[1]
        from pathlib import Path
        folder = str(Path(folder_raw).resolve())
        with get_db() as db:
            ok = delete_folder_weight(db, folder)
        if ok:
            _ok(f"已删除 {folder} 的权重配置")
        else:
            _err(f"未找到 {folder} 的权重配置")
    else:
        _err(f"未知子命令：{sub}")


def cmd_rate(args: list[str]):
    sub = args[0] if args else "list"
    if sub == "list":
        with get_db() as db:
            records = (
                db.query(IPRateLimit)
                .order_by(IPRateLimit.request_count.desc())
                .all()
            )
            if not records:
                print("  （无限速记录）")
                return
            print(f"  {'IP':<20} {'请求数':>8}  窗口开始")
            print("  " + "─" * 50)
            for r in records:
                ws = r.window_start.strftime("%Y-%m-%d %H:%M:%S") if r.window_start else "—"
                print(f"  {r.ip:<20} {r.request_count:>8}  {ws}")
    elif sub == "reset":
        if len(args) < 2:
            _err("用法：rate reset <ip>")
            return
        ip = args[1]
        with get_db() as db:
            rec = db.query(IPRateLimit).filter(IPRateLimit.ip == ip).first()
            if rec:
                db.delete(rec)
                db.commit()
                _ok(f"{ip} 的限速计数已重置")
            else:
                _err(f"未找到 {ip} 的限速记录")
    elif sub == "reset-all":
        with get_db() as db:
            n = db.query(IPRateLimit).delete()
            db.commit()
        _ok(f"已重置 {n} 条限速记录")
    else:
        _err(f"未知子命令：{sub}")


def cmd_image(args: list[str]):
    sub = args[0] if args else "help"
    if sub == "info":
        if len(args) < 2:
            _err("用法：image info <sha256>")
            return
        sha = args[1]
        with get_db() as db:
            img = get_image_by_hash(db, sha)
            if img is None:
                _err(f"未找到哈希 {sha}")
                return
            d = img.to_dict()

        print(_hr())
        for k, v in d.items():
            print(f"  {k:<16} {v}")
        print(_hr())
    elif sub == "top":
        n = int(args[1]) if len(args) > 1 else 10
        with get_db() as db:
            rows = (
                db.query(Image)
                .order_by(Image.access_count.desc())
                .limit(n)
                .all()
            )
            print(_hr())
            print(f"  {'#':>4}  {'访问数':>8}  {'文件名':<40}  哈希（前16位）")
            print("  " + "─" * 75)
            for i, img in enumerate(rows, 1):
                h = (img.sha256 or "no-hash")[:16]
                print(f"  {i:>4}  {img.access_count:>8}  {img.filename:<40}  {h}")
            print(_hr())
    else:
        _err(f"未知子命令：{sub}")


# ──────────────────────────────────────────────
# 入口分发
# ──────────────────────────────────────────────

_COMMANDS = {
    "scan":          cmd_scan,
    "hash-compute":  cmd_hash_compute,
    "stats":         cmd_stats,
    "whitelist":     cmd_whitelist,
    "blacklist":     cmd_blacklist,
    "weight":        cmd_weight,
    "rate":          cmd_rate,
    "image":         cmd_image,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return

    # 只有实际执行命令时才初始化数据库，help 不应产生运行时写入。
    init_db()

    cmd = sys.argv[1]
    rest = sys.argv[2:]

    handler = _COMMANDS.get(cmd)
    if handler is None:
        _err(f"未知命令：{cmd}")
        print("运行 `python manage.py help` 查看帮助")
        sys.exit(1)

    handler(rest)


if __name__ == "__main__":
    main()
