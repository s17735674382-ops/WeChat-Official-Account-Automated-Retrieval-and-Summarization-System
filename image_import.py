"""本地图片校验、不可覆盖的原图副本与增量建表；本模块不联网。"""

import hashlib
import json
import sqlite3
import struct
import zlib
import os
import threading
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wechat import DB_PATH, open_database


MAX_IMAGES = 6
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 15 * 1024 * 1024
EXTENSIONS = {".png", ".jpg", ".jpeg"}
_IMAGE_THREAD_LOCK = threading.Lock()


@contextmanager
def image_store_lock(db_path=DB_PATH):
    """导入和管理图片共用锁，防止清理尚未完成数据库登记的副本。"""
    if not _IMAGE_THREAD_LOCK.acquire(blocking=False):
        raise ValueError("另一个操作正在导入或管理图片，请稍后重试。")
    stream = None
    locked = False
    try:
        stream = (Path(db_path).resolve().parent / ".image_store.lock").open("a+b")
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise ValueError("另一个窗口正在导入或管理图片，请稍后重试。") from exc
        yield
    finally:
        try:
            if stream:
                try:
                    if locked:
                        stream.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                finally:
                    stream.close()
        finally:
            _IMAGE_THREAD_LOCK.release()


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def open_ai_database(path=DB_PATH):
    connection = open_database(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS image_articles (
                article_id INTEGER PRIMARY KEY REFERENCES articles(id),
                fingerprint TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS article_images (
                id INTEGER PRIMARY KEY,
                article_id INTEGER NOT NULL REFERENCES articles(id),
                image_index INTEGER NOT NULL,
                original_name TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                size_bytes INTEGER NOT NULL,
                UNIQUE(article_id, image_index)
            );
            CREATE TABLE IF NOT EXISTS ai_analyses (
                id INTEGER PRIMARY KEY,
                article_id INTEGER NOT NULL REFERENCES articles(id),
                model TEXT NOT NULL,
                analyzed_at TEXT NOT NULL,
                result_json TEXT,
                status TEXT NOT NULL,
                error_message TEXT,
                usage_json TEXT
            );
        """)
    except sqlite3.Error:
        connection.close()
        raise
    return connection


def image_info(data, suffix):
    """进行尺寸和基础文件结构检查，不在本地完整解码像素。"""
    width = height = 0
    if suffix == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
        position = 8
        seen_end = seen_data = False
        while position + 12 <= len(data):
            size = struct.unpack(">I", data[position:position + 4])[0]
            end = position + size + 12
            if end > len(data):
                raise ValueError("PNG 文件不完整。")
            kind = data[position + 4:position + 8]
            body = data[position + 8:position + 8 + size]
            crc = struct.unpack(">I", data[end - 4:end])[0]
            if zlib.crc32(kind + body) & 0xffffffff != crc:
                raise ValueError("PNG 文件校验失败。")
            if position == 8:
                if kind != b"IHDR" or size != 13:
                    raise ValueError("PNG 缺少有效的图片头。")
                width, height = struct.unpack(">II", body[:8])
            if kind == b"acTL":
                raise ValueError("本版本只接收静态 PNG，请先保存为静态图片。")
            seen_data = seen_data or kind == b"IDAT"
            position = end
            if kind == b"IEND":
                seen_end = size == 0 and position == len(data)
                break
        if not seen_end or not seen_data:
            raise ValueError("PNG 缺少图片数据或结束标记。")
        mime = "image/png"
    elif suffix in (".jpg", ".jpeg") and data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"):
        position = 2
        while position + 4 <= len(data):
            if data[position] != 0xff:
                break
            while position < len(data) and data[position] == 0xff:
                position += 1
            if position >= len(data):
                break
            marker = data[position]
            position += 1
            if marker in (0xda, 0xd9):
                break
            size = int.from_bytes(data[position:position + 2], "big")
            if size < 2 or position + size > len(data):
                break
            if marker in (0xc0, 0xc1, 0xc2) and size >= 8:
                height = int.from_bytes(data[position + 3:position + 5], "big")
                width = int.from_bytes(data[position + 5:position + 7], "big")
                break
            position += size
        mime = "image/jpeg"
    else:
        raise ValueError("文件内容与扩展名不符，或不是支持的 PNG/JPEG。")
    if not (1 <= width <= 8192 and 1 <= height <= 8192):
        raise ValueError("图片无法识别或尺寸超过 8192 像素/边，请先裁剪或缩小。")
    return mime, width, height


def read_images(location):
    value = str(location).strip().strip('"')
    if not value or value.startswith(("\\\\", "//")) or "://" in value:
        raise ValueError("请输入本机图片文件或文件夹路径，不接收网址或网络共享路径。")
    # 先规范化路径，再由实际打开文件验证权限；某些 Windows 环境会拒绝
    # strict=True 的属性查询，即便文件本身可以正常读取。
    path = Path(value).expanduser().resolve()
    if str(path).startswith(("\\\\", "//")):
        raise ValueError("只接收本机路径。")
    if path.is_dir():
        if any(p.is_file() and p.suffix.lower() in {".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic"} for p in path.iterdir()):
            raise ValueError("文件夹含本版本不支持的图片格式，请先统一保存为静态 PNG/JPEG，避免漏读文章页。")
        paths = sorted(
            (p for p in path.iterdir() if p.is_file() and p.suffix.lower() in EXTENSIONS),
            key=lambda p: (p.name.casefold(), p.name),
        )
    else:
        paths = [path]
    if not paths or len(paths) > MAX_IMAGES:
        raise ValueError(f"一次一篇文章，需要 1–{MAX_IMAGES} 张 PNG/JPEG；不会自动拆分文件夹。")
    images = []
    total = 0
    for source in paths:
        if source.suffix.lower() not in EXTENSIONS:
            raise ValueError("本版本仅支持 .png、.jpg、.jpeg。")
        with source.open("rb") as stream:
            data = stream.read(MAX_IMAGE_BYTES + 1)
        total += len(data)
        if len(data) > MAX_IMAGE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("图片超过限制：单张 5 MiB，一篇合计 15 MiB。")
        mime, width, height = image_info(data, source.suffix.lower())
        images.append({"name": source.name, "data": data, "sha256": hashlib.sha256(data).hexdigest(),
                       "mime_type": mime, "width": width, "height": height})
    return path, images


def import_images(location, db_path=DB_PATH):
    with image_store_lock(db_path):
        return _import_images(location, db_path)


def _import_images(location, db_path=DB_PATH):
    db_path = Path(db_path).resolve()
    source, images = read_images(location)
    fingerprint = hashlib.sha256(json.dumps([i["sha256"] for i in images]).encode("utf-8")).hexdigest()
    store = db_path.parent / "image_store" / fingerprint
    store.mkdir(parents=True, exist_ok=True)
    for index, image in enumerate(images, 1):
        extension = ".png" if image["mime_type"] == "image/png" else ".jpg"
        target = store / f"{index:02d}_{image['sha256']}{extension}"
        # 已有副本只核对，不覆盖。即使数据库或后续 API 失败，副本也保留。
        if target.exists():
            with target.open("rb") as stream:
                existing = stream.read(MAX_IMAGE_BYTES + 1)
            if hashlib.sha256(existing).hexdigest() != image["sha256"]:
                raise ValueError("已保存的原图副本校验不一致，已停止；不会覆盖原图。")
        else:
            with target.open("xb") as stream:
                stream.write(image["data"])
        image["stored_path"] = target.relative_to(db_path.parent).as_posix()

    with closing(open_ai_database(db_path)) as connection:
        with connection:
            existing = connection.execute(
                "SELECT article_id FROM image_articles WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            if existing:
                return existing[0], False
            title = f"【图片待核对】{source.name}"
            content = "本地图片已保存。识别结果请在 AI 分析菜单中查看，原文图片是核对依据。"
            digest = hashlib.sha256(("local_images:" + fingerprint).encode("utf-8")).hexdigest()
            cursor = connection.execute("""
                INSERT INTO articles (title, source_name, source_url, published_at, content,
                    import_method, imported_at, content_hash) VALUES (?, '', '', NULL, ?, 'local_images', ?, ?)
            """, (title, content, now(), digest))
            article_id = cursor.lastrowid
            connection.execute("INSERT INTO image_articles VALUES (?, ?, ?)", (article_id, fingerprint, now()))
            for index, image in enumerate(images, 1):
                connection.execute("""
                    INSERT INTO article_images (article_id, image_index, original_name, stored_path,
                        sha256, mime_type, width, height, size_bytes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (article_id, index, image["name"], image["stored_path"], image["sha256"],
                      image["mime_type"], image["width"], image["height"], len(image["data"])))
            connection.execute("""
                INSERT INTO ai_analyses (article_id, model, analyzed_at, status, error_message)
                VALUES (?, 'deepseek-flash', ?, 'pending', '仅本地保存；等待首次付费调用批准。')
            """, (article_id, now()))
    return article_id, True


def stored_images(article_id, db_path=DB_PATH):
    db_path = Path(db_path).resolve()
    with closing(open_ai_database(db_path)) as connection:
        rows = connection.execute(
            "SELECT * FROM article_images WHERE article_id = ? ORDER BY image_index", (article_id,)
        ).fetchall()
    if not 1 <= len(rows) <= MAX_IMAGES:
        raise ValueError("没有有效的已保存图片。")
    images = []
    total = 0
    root = (db_path.parent / "image_store").resolve()
    for row in rows:
        path = (db_path.parent / row["stored_path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("数据库中的图片路径超出原图目录。")
        with path.open("rb") as stream:
            data = stream.read(MAX_IMAGE_BYTES + 1)
        total += len(data)
        if len(data) > MAX_IMAGE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("原图副本超过大小限制。")
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ValueError("原图副本已改变，停止分析，请人工检查。")
        image_info(data, path.suffix)
        images.append(dict(row, data=data))
    return images
