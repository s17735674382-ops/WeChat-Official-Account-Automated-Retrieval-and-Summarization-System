"""公众号文章的保守提取、交互导入与本地 SQLite 保存。"""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

from safe_http import FetchError, fetch_public, validate_url


DB_PATH = Path(__file__).resolve().with_name("articles.db")


def open_database(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                source_name TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                published_at TEXT,
                content TEXT NOT NULL,
                import_method TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                content_hash TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS unique_article_url
                ON articles(source_url) WHERE source_url <> '';
            CREATE UNIQUE INDEX IF NOT EXISTS unique_unlinked_content
                ON articles(content_hash) WHERE source_url = '';
        """)
    except sqlite3.Error:
        connection.close()
        raise
    return connection


def save_article(article, path=DB_PATH):
    title = article["title"].strip()
    content = article["content"].replace("\r\n", "\n").replace("\r", "\n").strip()
    source = article.get("source_name", "").strip()
    url = article.get("source_url", "").strip()
    published = (article.get("published_at") or "").strip() or None
    method = article.get("import_method", "manual")
    if not title or not content:
        raise ValueError("标题和正文都不能为空。")
    if method not in ("manual", "public_url"):
        raise ValueError("未知的导入方式。")
    if url:
        validate_url(url)  # 手动粘贴只校验格式，不访问网页或查询 DNS。
    digest = hashlib.sha256(json.dumps(
        [title, source, content], ensure_ascii=False
    ).encode("utf-8")).hexdigest()
    imported = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    with closing(open_database(path)) as connection:
        with connection:
            try:
                cursor = connection.execute("""
                    INSERT INTO articles
                    (title, source_name, source_url, published_at, content,
                     import_method, imported_at, content_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (title, source, url, published, content, method, imported, digest))
                return cursor.lastrowid, True
            except sqlite3.IntegrityError:
                if url:
                    row = connection.execute(
                        "SELECT id FROM articles WHERE source_url = ?", (url,)
                    ).fetchone()
                else:
                    row = connection.execute(
                        "SELECT id FROM articles WHERE source_url = '' AND content_hash = ?", (digest,)
                    ).fetchone()
                if row is None:
                    raise
                return row["id"], False


class ArticleParser(HTMLParser):
    """只提取明确的正文节点，不回退到整个网页，不执行脚本。"""
    TARGETS = {"activity-name", "js_content", "js_name", "publish_time"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    BLOCK = {"p", "div", "section", "li", "h1", "h2", "h3", "blockquote", "br"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.text = {key: [] for key in self.TARGETS}
        self.count = {key: 0 for key in self.TARGETS}
        self.closed = set()

    def append(self, value):
        if self.stack and not self.stack[-1][2]:
            for key in self.stack[-1][1]:
                self.text[key].append(value)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        targets = set(self.stack[-1][1]) if self.stack else set()
        hidden = self.stack[-1][2] if self.stack else False
        style = "".join((attrs.get("style") or "").lower().split())
        hidden = hidden or tag in {"script", "style", "noscript", "iframe", "svg", "form"}
        hidden = hidden or "hidden" in attrs or attrs.get("aria-hidden") == "true"
        hidden = hidden or "display:none" in style or "visibility:hidden" in style
        key = attrs.get("id")
        if key in self.TARGETS:
            self.count[key] += 1
            targets.add(key)
        self.stack.append((tag, targets, hidden, key))
        if tag in self.BLOCK:
            self.append("\n")
        if tag in self.VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        if tag in self.BLOCK:
            self.append("\n")
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                key = self.stack[index][3]
                if key in self.TARGETS:
                    self.closed.add(key)
                del self.stack[index:]
                break

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_data(self, data):
        self.append(data)

    def value(self, key):
        return "\n\n".join(
            line.strip() for line in "".join(self.text[key]).splitlines() if line.strip()
        )


def extract_article(html):
    parser = ArticleParser()
    parser.feed(html)
    parser.close()
    title = " ".join(parser.value("activity-name").split())
    content = parser.value("js_content")
    markers = ("访问过于频繁", "环境异常", "安全验证", "请完成验证", "文章已被删除",
               "该内容已被发布者删除", "此内容因违规无法查看", "请在微信客户端打开", "登录后查看")
    if (parser.count["activity-name"] != 1 or parser.count["js_content"] != 1
            or not {"activity-name", "js_content"}.issubset(parser.closed)
            or not title or len(title) > 300 or len(content) < 80
            or sum(c.isalnum() for c in content) < 40
            or any(marker in title + content for marker in markers)):
        raise ValueError("未找到可靠的标题和正文，可能是限制页面、短文、图片文章或需要脚本加载。")
    return {"title": title, "content": content, "source_name": parser.value("js_name"),
            "published_at": parser.value("publish_time"), "import_method": "public_url"}


def fetch_article(url):
    data, headers, _ = fetch_public(
        url, timeout=20, max_bytes=2 * 1024 * 1024, allowed_host="mp.weixin.qq.com"
    )
    if headers.get_content_type() not in ("text/html", "application/xhtml+xml"):
        raise ValueError("返回内容不是 HTML 文章网页。")
    charset = headers.get_content_charset() or "utf-8"
    try:
        html = data.decode(charset)
    except (UnicodeError, LookupError) as exc:
        raise ValueError("网页编码无法可靠识别，请改用手动粘贴。") from exc
    article = extract_article(html)
    article["source_url"] = url.strip()
    return article


def manual_article():
    title = input("文章标题（必填）：").strip()
    source = input("公众号名称（可留空）：").strip()
    published = input("发布时间（未知请直接回车）：").strip()
    url = input("原文链接（可留空）：").strip()
    print("请粘贴正文，可包含多段和空行。结束时另起一行输入 END，再按回车。")
    lines = []
    while True:
        line = input()
        if line == "END":
            break
        lines.append(line)
    return {"title": title, "source_name": source, "published_at": published,
            "source_url": url, "content": "\n".join(lines), "import_method": "manual"}


def import_article(path=DB_PATH):
    choice = input("A. 从公开文章链接尝试读取\nB. 手动输入标题和正文\n请选择 A 或 B（回车返回）：").strip().upper()
    if choice not in ("A", "B"):
        return
    if choice == "A":
        url = input("请粘贴 mp.weixin.qq.com 文章链接：").strip()
        try:
            article = fetch_article(url)
        except (FetchError, ValueError) as exc:
            print(f"链接导入失败：{exc}\n没有保存文章，也不会尝试绕过访问限制。")
            if input("改用 B 手动粘贴？输入 Y 继续，其他输入返回：").strip().upper() != "Y":
                return
            article = manual_article()
    else:
        article = manual_article()

    print(f"\n标题：{article['title']}\n来源：{article['source_name'] or '未提供'}")
    print(f"正文预览：\n{article['content'][:400]}\n（正文共 {len(article['content'])} 个字符）")
    if input("确认标题和正文正确并保存？输入 Y 保存，其他输入取消：").strip().upper() != "Y":
        print("已取消，没有保存。")
        return
    number, created = save_article(article, path)
    if created:
        print(f"保存成功，文章编号 {number}。数据库：{path}")
    else:
        print(f"文章已存在，编号 {number}；没有新增记录，也没有覆盖已有正文。")


def view_articles(path=DB_PATH):
    with closing(open_database(path)) as connection:
        total = connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        print(f"\n已保存 {total} 篇文章。数据库：{path}")
        if not total:
            return
        offset = 0
        while True:
            rows = connection.execute(
                "SELECT id, title, source_name, published_at FROM articles ORDER BY id DESC LIMIT ? OFFSET ?",
                (20, offset),
            ).fetchall()
            for row in rows:
                print(f"{row['id']}. {row['title']} | {row['source_name'] or '未提供'} | {row['published_at'] or '未提供'}")
            choice = input("输入编号查看全文，N 下一页，回车返回：").strip()
            if not choice:
                return
            if choice.upper() == "N":
                offset = offset + 20 if offset + 20 < total else 0
                continue
            if not choice.isascii() or not choice.isdecimal():
                print("请输入列表中的文章编号。")
                continue
            if len(choice) > 18:
                print("编号超出范围。")
                continue
            row = connection.execute("SELECT * FROM articles WHERE id = ?", (int(choice),)).fetchone()
            if row is None:
                print("没有找到这个编号。")
                continue
            print(f"\n标题：{row['title']}\n公众号：{row['source_name'] or '未提供'}")
            print(f"发布时间：{row['published_at'] or '未提供'}\n原文链接：{row['source_url'] or '未提供'}")
            print(f"导入方式：{row['import_method']}\n导入时间（北京时间）：{row['imported_at']}\n\n{row['content']}\n")
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wechat2rss_images'").fetchone():
                images = connection.execute(
                    "SELECT image_url FROM wechat2rss_images WHERE article_id = ? ORDER BY id", (row["id"],)
                ).fetchall()
                if images:
                    print(f"RSS 中的图片地址（{len(images)} 个，只保存地址，未下载图片）：")
                    for image in images:
                        print(image["image_url"])
