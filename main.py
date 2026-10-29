"""大学信息雷达 v0.4.1：RSS、公众号导入、AI 分析与数据管理。"""

import json
import sqlite3
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from safe_http import FetchError, fetch_public, validate_url
from wechat import DB_PATH, import_article, view_articles
from deepseek_analysis import AnalysisError, image_analysis_menu, view_analyses
from data_management import data_management_menu
from wechat2rss import menu as wechat2rss_menu, cli as wechat2rss_cli


CONFIG_PATH = Path(__file__).resolve().with_name("config.json")
MAX_FEED_BYTES = 5 * 1024 * 1024


class ReaderError(Exception):
    """可以直接向用户展示的问题。"""


def load_config(path=CONFIG_PATH):
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ReaderError(f"无法读取配置文件：{path}。详情：{exc}") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReaderError("config.json 格式无效，请使用 UTF-8 编码和正确的 JSON 格式。") from exc

    if not isinstance(config, dict):
        raise ReaderError("config.json 的最外层必须是一对花括号。")
    sources = config.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ReaderError("请在 config.json 的 sources 列表中填写至少一个信息源（name 和 url）。")

    limit = config.get("max_articles", 10)
    timeout = config.get("timeout_seconds", 20)
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ReaderError("max_articles 必须是 1 到 10 之间的整数。")
    if type(timeout) is not int or not 1 <= timeout <= 120:
        raise ReaderError("timeout_seconds 必须是 1 到 120 之间的整数。")
    return sources, limit, timeout


def source_details(source):
    if not isinstance(source, dict):
        raise ReaderError("信息源配置必须是一对花括号，包含 name 和 url。")
    name = source.get("name")
    url = source.get("url")
    if not isinstance(name, str) or not name.strip():
        raise ReaderError("信息源缺少名称，请填写 name。")
    if not isinstance(url, str) or not url.strip():
        raise ReaderError("信息源缺少网址，请填写 url。")
    url = url.strip()
    try:
        parsed_url = urllib.parse.urlsplit(url)
        valid = parsed_url.scheme in ("http", "https") and parsed_url.hostname
        parsed_url.port  # 同时检查端口是否有效。
    except ValueError as exc:
        raise ReaderError("信息源 url 不是有效的网址。") from exc
    if not valid:
        raise ReaderError("信息源 url 必须是以 http:// 或 https:// 开头的完整网址。")
    try:
        validate_url(url)
    except FetchError as exc:
        raise ReaderError(str(exc)) from exc
    return name.strip(), url


def fetch_feed(url, timeout):
    try:
        content, _, _ = fetch_public(url, timeout=timeout, max_bytes=MAX_FEED_BYTES)
    except FetchError as exc:
        raise ReaderError(str(exc)) from exc
    return content


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def child_text(element, *names):
    for name in names:
        for child in element:
            if local_name(child.tag) == name:
                text = " ".join("".join(child.itertext()).split())
                if text:
                    return text
    return "未提供"


def parse_feed(content):
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ReaderError("返回内容不是有效的 XML，可能是错误页面或损坏的 RSS。") from exc

    root_name = local_name(root.tag)
    if root_name == "rss":
        channel = next((c for c in root if local_name(c.tag) == "channel"), None)
        if channel is None:
            raise ReaderError("RSS 格式无效：缺少 channel 节点。")
        entries = [c for c in channel if local_name(c.tag) == "item"]
    elif root_name == "RDF":
        entries = [c for c in root if local_name(c.tag) == "item"]
    elif root_name == "feed":
        entries = [c for c in root if local_name(c.tag) == "entry"]
    else:
        raise ReaderError("内容虽然是 XML，但不是支持的 RSS 或 Atom 订阅源。")

    if not entries:
        raise ReaderError("订阅源中没有文章（未找到 item 或 entry），请稍后重试。")

    articles = []
    for entry in entries:
        if root_name == "feed":
            link = next(
                (
                    c.get("href").strip()
                    for c in entry
                    if local_name(c.tag) == "link"
                    and c.get("rel", "alternate") == "alternate"
                    and c.get("href", "").strip()
                ),
                "未提供",
            )
            published = child_text(entry, "published")
        else:
            link = child_text(entry, "link")
            published = child_text(entry, "pubDate", "date")
        articles.append(
            {"title": child_text(entry, "title"), "published": published, "link": link}
        )
    return sorted(articles, key=article_time, reverse=True)


def article_time(article):
    """识别 RSS / Atom 常见时间格式，缺失或无法识别的时间排在末尾。"""
    value = article["published"]
    try:
        published = parsedate_to_datetime(value)
    except (ValueError, TypeError, OverflowError):
        try:
            published = datetime.fromisoformat(value)
        except (ValueError, TypeError, OverflowError):
            return datetime.min.replace(tzinfo=timezone.utc)
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    try:
        return published.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return datetime.min.replace(tzinfo=timezone.utc)


def read_sources(sources, limit, timeout):
    """逐源读取；单个源失败后继续；去重范围为本次运行已显示的链接。"""
    seen_links = set()
    succeeded = failed = total = 0
    for index, source in enumerate(sources, 1):
        label = f"第 {index} 个信息源"
        if isinstance(source, dict) and isinstance(source.get("name"), str):
            label = source["name"].strip() or label
        print(f"\n[{index}/{len(sources)}] 来源：{label}", flush=True)
        try:
            name, url = source_details(source)
            print(f"正在读取：{url}", flush=True)
            articles = parse_feed(fetch_feed(url, timeout))
        except ReaderError as exc:
            failed += 1
            print(f"读取失败：{exc} 将继续读取其他信息源。", flush=True)
            continue

        succeeded += 1
        selected = []
        duplicates = 0
        for article in articles:
            link = article["link"]
            if link != "未提供" and link in seen_links:
                duplicates += 1
                continue
            selected.append(article)
            if link != "未提供":
                seen_links.add(link)
            if len(selected) >= limit:
                break

        print(
            f"读取到 {len(articles)} 篇文章，本源显示 {len(selected)} 篇；"
            f"筛选时跳过重复链接 {duplicates} 篇。"
        )
        if not selected:
            print("本源文章链接均已显示，本次不重复显示。")
        for number, article in enumerate(selected, 1):
            print(f"\n{number}. 来源：{name}")
            print(f"   标题：{article['title']}")
            print(f"   发布时间：{article['published']}")
            print(f"   原文链接：{article['link']}")
        total += len(selected)

    print(f"\n读取完成：成功 {succeeded} 个源，失败 {failed} 个源，共显示 {total} 篇文章。")
    if not succeeded:
        return 1
    return 2 if failed else 0


def run_rss():
    try:
        sources, limit, timeout = load_config()
    except ReaderError as exc:
        print(f"读取失败：{exc}", file=sys.stderr)
        return 1
    print(f"已配置 {len(sources)} 个信息源，每源最多显示 {limit} 篇。")
    print("每源按发布时间从新到旧显示，缺失或无法识别的时间排在最后。")
    return read_sources(sources, limit, timeout)


def main(db_path=DB_PATH):
    # 确保 Windows 终端和输出记录中的中文可正常显示。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    print("大学信息雷达（DeepSeek 每次调用前确认；支持 Wechat2RSS 增量采集）")
    if sys.argv[1:] == ["--rss"]:
        return run_rss()
    if sys.argv[1:] in (["--daily-run"], ["--generate-report"]):
        from daily_pipeline import run
        return run(db_path, CONFIG_PATH, report_only=sys.argv[1] == "--generate-report")
    if len(sys.argv) == 3 and sys.argv[1] == "--retry-analysis" and sys.argv[2].isascii() and sys.argv[2].isdecimal():
        from daily_pipeline import run
        return run(db_path, CONFIG_PATH, retry_article=int(sys.argv[2]))
    if sys.argv[1:] and sys.argv[1] in ("--import-opml", "--sync-once", "--sync-every", "--list-sources"):
        return wechat2rss_cli(sys.argv[1:], db_path)
    if sys.argv[1:]:
        print("使用 main.py 打开菜单，或使用 --rss、--import-opml、--sync-once 等命令。")
        return 1
    try:
        while True:
            choice = input("\n1. 读取 RSS\n2. 手动导入公众号文章\n3. 查看已保存文章\n4. 退出\n5. 导入本地图片并分析\n6. 查看图片分析结果\n7. 数据管理\n8. Wechat2RSS 采集\n请选择 1–8：").strip()
            try:
                if choice == "1":
                    run_rss()
                elif choice == "2":
                    import_article(db_path)
                elif choice == "3":
                    view_articles(db_path)
                elif choice == "4":
                    return 0
                elif choice == "5":
                    image_analysis_menu(db_path, CONFIG_PATH)
                elif choice == "6":
                    view_analyses(db_path)
                elif choice == "7":
                    data_management_menu(db_path)
                elif choice == "8":
                    wechat2rss_menu(db_path)
                else:
                    print("请输入 1 到 8。")
            except (AnalysisError, FetchError, ValueError, sqlite3.Error, OSError) as exc:
                print(f"操作失败：{exc}。请检查后重试，已有文章和原图不会被覆盖。")
    except (EOFError, KeyboardInterrupt):
        print("\n输入已结束，程序退出。已保存的数据保留；未确认的 API 调用不会发出。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
