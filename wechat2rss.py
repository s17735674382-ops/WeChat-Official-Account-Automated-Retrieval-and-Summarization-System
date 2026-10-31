"""从 OPML 导入 Wechat2RSS 订阅，增量同步公开 RSS；从不调用 AI。"""

import argparse
import hashlib
import html
import json
import sqlite3
import time
import xml.etree.ElementTree as ET
from contextlib import closing
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from safe_http import FetchError, fetch_public, validate_url
from wechat import DB_PATH, open_database


MAX_OPML_BYTES = 2 * 1024 * 1024
MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_ITEMS = 200
MAX_CONTENT_CHARS = 200_000
MAX_IMAGES_PER_ITEM = 30


class SubscriptionError(ValueError):
    pass


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def local(tag):
    return tag.rsplit("}", 1)[-1]


def normalized_url(value):
    """地址去重只规范协议、主机和片段；路径、查询参数原样保留。"""
    value, parts, host = validate_url(value)
    netloc = (f"[{host}]" if ":" in host else host) + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))


def open_sync_database(path=DB_PATH):
    connection = open_database(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS wechat2rss_sources (
                id INTEGER PRIMARY KEY,
                feed_url TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                last_synced_at TEXT,
                last_status TEXT,
                last_error TEXT,
                last_new_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS rss_retired (
                kind TEXT NOT NULL, digest TEXT NOT NULL,
                PRIMARY KEY(kind,digest)
            );
            CREATE TABLE IF NOT EXISTS wechat2rss_entries (
                id INTEGER PRIMARY KEY,
                source_id INTEGER NOT NULL REFERENCES wechat2rss_sources(id) ON DELETE CASCADE,
                stable_key TEXT NOT NULL,
                article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
                UNIQUE(source_id, stable_key)
            );
            CREATE TABLE IF NOT EXISTS wechat2rss_images (
                id INTEGER PRIMARY KEY,
                article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
                image_url TEXT NOT NULL,
                UNIQUE(article_id, image_url)
            );
        """)
    except BaseException:
        connection.close()
        raise
    return connection


def import_opml(opml_path, db_path=DB_PATH):
    path = Path(str(opml_path).strip().strip('"'))
    if not path.is_file() or path.stat().st_size > MAX_OPML_BYTES:
        raise SubscriptionError("OPML 文件不存在，或超过 2 MB 限制。")
    data = path.read_bytes()
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise SubscriptionError("OPML 包含不需要的 DTD/实体，已拒绝。")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise SubscriptionError("OPML 不是有效 XML。") from exc
    if local(root.tag).lower() != "opml":
        raise SubscriptionError("文件不是 OPML。")
    entries = []
    seen = set()
    for outline in root.iter():
        if local(outline.tag).lower() != "outline" or not outline.get("xmlUrl"):
            continue
        try:
            url = normalized_url(outline.get("xmlUrl"))
        except FetchError as exc:
            raise SubscriptionError(f"OPML 第 {len(entries) + 1} 个订阅网址无效：{exc}") from exc
        if url in seen:
            continue
        seen.add(url)
        name = (outline.get("title") or outline.get("text") or "").strip()
        entries.append((url, name[:100]))
    if not entries:
        raise SubscriptionError("OPML 中没有带 xmlUrl 的订阅。")
    new_count = 0
    with closing(open_sync_database(db_path)) as connection:
        with connection:
            for url, name in entries:
                existing = connection.execute(
                    "SELECT id FROM wechat2rss_sources WHERE feed_url = ?", (url,)
                ).fetchone()
                if existing is not None:
                    continue
                ordinal = connection.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM wechat2rss_sources").fetchone()[0]
                placeholder = name or f"公众号订阅 {ordinal}（名称待识别）"
                connection.execute(
                    "INSERT INTO wechat2rss_sources(feed_url,name,imported_at) VALUES (?,?,?)",
                    (url, placeholder, now()),
                )
                new_count += 1
    return len(entries), new_count


class FeedHTML(HTMLParser):
    BLOCKS = {"p", "div", "section", "article", "li", "br", "h1", "h2", "h3", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.images = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in {"script", "style", "noscript", "iframe", "form"}:
            self.hidden += 1
        if tag in self.BLOCKS and not self.hidden:
            self.parts.append("\n")
        if tag == "img":
            address = attributes.get("data-src") or attributes.get("src")
            if address:
                self.images.append(address)

    def handle_endtag(self, tag):
        if tag in self.BLOCKS and not self.hidden:
            self.parts.append("\n")
        if tag in {"script", "style", "noscript", "iframe", "form"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, value):
        if not self.hidden:
            self.parts.append(value)

    def text(self):
        return "\n".join(line.strip() for line in "".join(self.parts).splitlines() if line.strip())[:MAX_CONTENT_CHARS]


def child_value(element, names):
    for name in names:
        for child in element:
            if local(child.tag) == name:
                value = "".join(child.itertext()).strip()
                if value:
                    return value
    return ""


def safe_item_url(raw, base):
    if not raw:
        return ""
    try:
        return normalized_url(urljoin(base, html.unescape(raw.strip())))
    except (FetchError, ValueError):
        return ""


def safe_failure(exc):
    """不把可能含订阅令牌的原始异常或 URL 写入终端、数据库。"""
    if isinstance(exc, SubscriptionError):
        return str(exc)
    if isinstance(exc, FetchError):
        message = str(exc)
        for code in (401, 403, 404, 410):
            if f"HTTP {code}" in message:
                return f"订阅可能失效或无权限（HTTP {code}）。"
        if "超时" in message or "timed out" in message.lower():
            return "网络请求超时。"
        if "无法解析" in message:
            return "订阅域名解析失败。"
        if "压缩内容" in message:
            return "RSS 返回了当前读取器不支持的压缩内容。"
        if "MB 限制" in message:
            return "RSS 超过 5 MB 下载限制。"
        return "RSS 网络读取失败（连接、证书或服务端响应异常）。"
    if isinstance(exc, sqlite3.Error):
        return "数据库写入失败；本信息源的文章未提交。"
    return "读取或保存时发生错误；请检查文件和订阅状态。"


def parse_feed(data, feed_url):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise SubscriptionError("RSS 包含不需要的 DTD/实体。")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise SubscriptionError("返回内容不是有效 RSS/Atom XML。") from exc
    kind = local(root.tag)
    if kind == "rss":
        channel = next((x for x in root if local(x.tag) == "channel"), None)
        if channel is None:
            raise SubscriptionError("RSS 缺少 channel 节点。")
        nodes = [x for x in channel if local(x.tag) == "item"]
        channel_title = child_value(channel, ("title",))
    elif kind == "RDF":
        nodes = [x for x in root if local(x.tag) == "item"]
        channel_title = child_value(root, ("title",))
    elif kind == "feed":
        nodes = [x for x in root if local(x.tag) == "entry"]
        channel_title = child_value(root, ("title",))
    else:
        raise SubscriptionError("内容不是支持的 RSS 或 Atom 格式。")
    result = []
    skipped = 0
    for node in nodes[:MAX_ITEMS]:
        title = " ".join(child_value(node, ("title",)).split())[:300]
        if not title:
            skipped += 1
            continue
        guid = child_value(node, ("guid", "id"))[:2000]
        link = ""
        if kind == "feed":
            for child in node:
                if local(child.tag) == "link" and child.get("rel", "alternate") == "alternate":
                    link = child.get("href", "")
                    if link:
                        break
        else:
            link = child_value(node, ("origLink", "link"))
        link = safe_item_url(link, feed_url)
        if not link and guid.startswith(("http://", "https://")):
            link = safe_item_url(guid, feed_url)
        published = child_value(node, ("pubDate", "published", "updated", "date"))[:120] or None
        body = child_value(node, ("encoded", "content", "description", "summary"))
        parser = FeedHTML()
        parser.feed(body)
        parser.close()
        content = parser.text()
        image_urls = []
        for child in node:
            tag = local(child.tag)
            if tag in {"enclosure", "thumbnail", "content"}:
                address = child.get("url")
                media_type = child.get("type", "")
                if address and (tag == "thumbnail" or media_type.startswith("image/") or "media" in child.tag):
                    image_urls.append(address)
        image_urls += parser.images
        images = []
        for raw in image_urls:
            image = safe_item_url(raw, link or feed_url)
            if image and image not in images:
                images.append(image)
            if len(images) >= MAX_IMAGES_PER_ITEM:
                break
        if not guid and not link:
            guid = hashlib.sha256(json.dumps([title, published, content], ensure_ascii=False).encode("utf-8")).hexdigest()
        stable_key = hashlib.sha256((guid or link).encode("utf-8")).hexdigest()
        result.append({"title": title, "source_url": link, "published_at": published,
                       "content": content, "stable_key": stable_key, "image_urls": images})
    return channel_title[:100], result, skipped, len(nodes) > MAX_ITEMS


def retired_keys(feed_url, entry):
    key = hashlib.sha256(normalized_url(feed_url).encode()).hexdigest()
    pairs = [('stable',hashlib.sha256((key+':'+entry['stable_key']).encode()).hexdigest())]
    if entry.get('source_url'):
        pairs.append(('url',hashlib.sha256(entry['source_url'].encode()).hexdigest()))
    return pairs


def is_retired(connection, feed_url, entry):
    # 兼容尚未升级的只读旧库。
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='rss_retired'").fetchone():
        return False
    return any(connection.execute('SELECT 1 FROM rss_retired WHERE kind=? AND digest=?',p).fetchone()
               for p in retired_keys(feed_url,entry))


def save_entry(connection, source_id, source_name, entry):
    feed = connection.execute('SELECT feed_url FROM wechat2rss_sources WHERE id=?',(source_id,)).fetchone()
    if feed and is_retired(connection,feed[0],entry):
        return False
    if connection.execute(
        "SELECT 1 FROM wechat2rss_entries WHERE source_id = ? AND stable_key = ?",
        (source_id, entry["stable_key"]),
    ).fetchone():
        return False
    title, content, url = entry["title"], entry["content"], entry["source_url"]
    digest = hashlib.sha256(json.dumps([title, source_name, content], ensure_ascii=False).encode("utf-8")).hexdigest()
    cursor = connection.execute("""
        INSERT OR IGNORE INTO articles
        (title, source_name, source_url, published_at, content, import_method, imported_at, content_hash)
        VALUES (?, ?, ?, ?, ?, 'wechat2rss', ?, ?)
    """, (title, source_name, url, entry["published_at"], content, now(), digest))
    created = cursor.rowcount == 1
    if created:
        article_id = cursor.lastrowid
    else:
        existing = connection.execute(
            "SELECT id FROM articles WHERE source_url = ?" if url else
            "SELECT id FROM articles WHERE source_url = '' AND content_hash = ?",
            (url if url else digest,),
        ).fetchone()
        if existing is None:
            raise SubscriptionError("文章唯一性冲突，未覆盖原记录。")
        article_id = existing[0]
    connection.execute("INSERT OR IGNORE INTO wechat2rss_entries(source_id,stable_key,article_id) VALUES (?,?,?)",
                       (source_id, entry["stable_key"], article_id))
    for image in entry["image_urls"]:
        connection.execute("INSERT OR IGNORE INTO wechat2rss_images(article_id,image_url) VALUES (?,?)",
                           (article_id, image))
    return created


def sync_sources(db_path=DB_PATH, *, first_only=False, source_id=None, timeout=20,
                 fetch=fetch_public, report=print):
    """逐源同步，任一源失败记录状态后继续；返回成功、失败、新文章数。"""
    if not 1 <= timeout <= 120:
        raise SubscriptionError("超时须为 1 到 120 秒。")
    with closing(open_sync_database(db_path)) as connection:
        sources = connection.execute("SELECT id, feed_url, name FROM wechat2rss_sources ORDER BY id").fetchall()
    if first_only and source_id is not None:
        raise SubscriptionError("请只选择一种单源同步方式。")
    if source_id is not None:
        if type(source_id) is not int or source_id < 1:
            raise SubscriptionError("订阅编号必须是正整数。")
        sources = [source for source in sources if source[0] == source_id]
        if not sources:
            raise SubscriptionError("没有找到这个订阅编号。请先查看订阅列表。")
    elif first_only:
        sources = sources[:1]
    if not sources:
        raise SubscriptionError("尚无公众号订阅。请先导入 OPML。")
    success = failure = added = 0
    for position, source in enumerate(sources, 1):
        source_id, feed_url, name = source
        report(f"[{position}/{len(sources)}] 正在同步：{name}")
        try:
            data, _, _ = fetch(feed_url, timeout=timeout, max_bytes=MAX_FEED_BYTES)
            channel_name, entries, skipped, truncated = parse_feed(data, feed_url)
            chosen_name = channel_name or name
            fresh = 0
            with closing(open_sync_database(db_path)) as connection:
                with connection:
                    if channel_name:
                        connection.execute("UPDATE wechat2rss_sources SET name = ? WHERE id = ?",
                                           (channel_name, source_id))
                    for entry in entries:
                        if save_entry(connection, source_id, chosen_name, entry):
                            fresh += 1
                    connection.execute("""UPDATE wechat2rss_sources SET last_synced_at=?, last_status='success',
                        last_error=NULL, last_new_count=? WHERE id=?""", (now(), fresh, source_id))
            success += 1
            added += fresh
            suffix = "，仅处理前 200 条" if truncated else ""
            report(f"成功：RSS {len(entries)} 篇，新增 {fresh} 篇，跳过无标题 {skipped} 篇{suffix}。" if entries else
                   "成功读取 RSS，但当前没有可入库文章。")
        except (FetchError, SubscriptionError, sqlite3.Error, OSError, UnicodeError, ValueError) as exc:
            failure += 1
            safe_message = safe_failure(exc)
            report(f"失败：{safe_message}；继续下一个公众号。")
            try:
                with closing(open_sync_database(db_path)) as connection:
                    with connection:
                        connection.execute("""UPDATE wechat2rss_sources SET last_synced_at=?, last_status='failed',
                            last_error=?, last_new_count=0 WHERE id=?""", (now(), safe_message, source_id))
            except (sqlite3.Error, OSError):
                report("警告：本信息源的失败状态未能写入数据库；请检查数据库是否可写。")
    report(f"同步完成：成功 {success} 个，失败 {failure} 个，新增文章 {added} 篇。")
    return success, failure, added


def list_sources(db_path=DB_PATH, report=print):
    with closing(open_sync_database(db_path)) as connection:
        rows = connection.execute("""SELECT id,name,last_synced_at,last_status,last_error,last_new_count
            FROM wechat2rss_sources ORDER BY id""").fetchall()
    report(f"已导入 {len(rows)} 个订阅（为保护 Token，不显示完整 RSS 地址）：")
    for row in rows:
        report(f"{row['id']}. {row['name']} | 最近同步：{row['last_synced_at'] or '未同步'} | "
               f"状态：{row['last_status'] or '未同步'} | 最近新增：{row['last_new_count']}"
               + (f" | 原因：{row['last_error']}" if row['last_error'] else ""))


def menu(db_path=DB_PATH):
    while True:
        choice = input("\nWechat2RSS 采集\n1. 导入 OPML\n2. 只同步指定的一个公众号\n3. 同步全部公众号\n4. 查看订阅状态\n0. 返回\n请选择：").strip()
        try:
            if choice == "0":
                return
            if choice == "1":
                path = input("OPML 文件完整路径：").strip()
                total, added = import_opml(path, db_path)
                print(f"OPML 中有 {total} 个不同地址，本次新增 {added} 个订阅。")
            elif choice == "2":
                list_sources(db_path)
                selected = input("要同步的订阅编号（直接回车选第 1 个）：").strip() or "1"
                if not selected.isascii() or not selected.isdecimal():
                    raise SubscriptionError("请输入列表中的正整数编号。")
                sync_sources(db_path, source_id=int(selected))
            elif choice == "3":
                sync_sources(db_path)
            elif choice == "4":
                list_sources(db_path)
            else:
                print("请输入 0 到 4。")
        except (SubscriptionError, FetchError, sqlite3.Error, OSError) as exc:
            print(f"操作失败：{exc}")


def cli(argv=None, db_path=DB_PATH):
    parser = argparse.ArgumentParser(description="Wechat2RSS 订阅导入与增量同步（不会调用 DeepSeek）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--import-opml", metavar="路径")
    group.add_argument("--sync-once", action="store_true")
    group.add_argument("--sync-every", type=int, metavar="分钟")
    group.add_argument("--list-sources", action="store_true")
    parser.add_argument("--first-only", action="store_true", help="本次只同步第一个公众号")
    parser.add_argument("--source-id", type=int, metavar="编号", help="本次只同步指定编号的公众号")
    args = parser.parse_args(argv)
    if (args.first_only or args.source_id is not None) and not (args.sync_once or args.sync_every):
        parser.error("单源选项只能与同步命令一起使用。")
    if args.first_only and args.source_id is not None:
        parser.error("--first-only 和 --source-id 不能同时使用。")
    try:
        if args.import_opml:
            total, added = import_opml(args.import_opml, db_path)
            print(f"OPML 中有 {total} 个不同地址，本次新增 {added} 个订阅。")
            return 0
        if args.list_sources:
            list_sources(db_path)
            return 0
        if args.sync_once:
            _, failed, _ = sync_sources(db_path, first_only=args.first_only, source_id=args.source_id)
            return 2 if failed else 0
        if not 1 <= args.sync_every <= 1440:
            parser.error("同步间隔须为 1 到 1440 分钟。")
        print(f"每 {args.sync_every} 分钟同步一次；保持窗口打开，按 Ctrl+C 停止。不会自动调用 DeepSeek。")
        while True:
            sync_sources(db_path, first_only=args.first_only, source_id=args.source_id)
            time.sleep(args.sync_every * 60)
    except KeyboardInterrupt:
        print("\n自动同步已停止。")
        return 0
    except (SubscriptionError, FetchError, sqlite3.Error, OSError) as exc:
        print(f"操作失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
