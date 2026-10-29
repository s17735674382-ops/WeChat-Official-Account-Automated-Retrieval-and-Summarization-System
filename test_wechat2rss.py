"""Wechat2RSS 免费回归：临时数据库、假 RSS，无外网或 DeepSeek 请求。"""

import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import data_management
import wechat2rss
import main
from image_import import open_ai_database
from safe_http import FetchError
from wechat import save_article


FEED = b'''<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:media="http://search.yahoo.com/mrss/">
<channel><title>\xe6\xb5\x8b\xe8\xaf\x95\xe5\x85\xac\xe4\xbc\x97\xe5\x8f\xb7</title>
<item><title>\xe6\xb5\x8b\xe8\xaf\x95\xe8\xae\xb2\xe5\xba\xa7</title>
<guid>stable-guid-1</guid><link>https://mp.weixin.qq.com/s/test-article</link>
<pubDate>Wed, 28 Oct 2026 10:00:00 GMT</pubDate>
<content:encoded><![CDATA[<p>\xe7\xac\xac\xe4\xb8\x80\xe6\xae\xb5\xe4\xb8\xad\xe6\x96\x87</p><p>\xe7\xac\xac\xe4\xba\x8c\xe6\xae\xb5\xe4\xb8\xad\xe6\x96\x87</p><img data-src="https://example.com/poster.png"/>]]></content:encoded>
<media:thumbnail url="https://example.com/thumb.jpg"/>
</item></channel></rss>'''
EMPTY = b'<rss version="2.0"><channel><title></title></channel></rss>'
URL1 = "https://example.com/rss/first-secret"
URL2 = "https://example.com/rss/second-secret"


class Wechat2RSSTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ruc-test-only-wechat2rss-",
                                                dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.db = self.base / "articles.db"
        self.opml = self.base / "subscriptions.opml"
        self.opml.write_text(f'''<?xml version="1.0"?><opml version="2.0"><body>
            <outline xmlUrl="{URL1}" text="" />
            <outline xmlUrl="{URL1}" text="" />
            <outline xmlUrl="{URL2}" text="备选公众号" />
            </body></opml>''', encoding="utf-8")

    def connection(self):
        c = wechat2rss.open_sync_database(self.db)
        c.row_factory = sqlite3.Row
        return c

    def test_opml_import_dedup_and_no_token_in_listing(self):
        self.assertEqual(wechat2rss.import_opml(self.opml, self.db), (2, 2))
        self.assertEqual(wechat2rss.import_opml(self.opml, self.db), (2, 0))
        output = []
        wechat2rss.list_sources(self.db, report=output.append)
        self.assertNotIn("first-secret", "\n".join(output))
        self.assertIn("公众号订阅 1", "\n".join(output))
        with contextlib.closing(self.connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM wechat2rss_sources").fetchone()[0], 2)

    def test_one_feed_real_parser_save_chinese_body_images_and_repeat(self):
        wechat2rss.import_opml(self.opml, self.db)
        calls = []
        def fake_fetch(url, **kwargs):
            calls.append(url)
            return FEED, None, url
        self.assertEqual(wechat2rss.sync_sources(self.db, first_only=True, fetch=fake_fetch, report=lambda _: None), (1, 0, 1))
        self.assertEqual(wechat2rss.sync_sources(self.db, first_only=True, fetch=fake_fetch, report=lambda _: None), (1, 0, 0))
        self.assertEqual(calls, [URL1, URL1])
        with contextlib.closing(self.connection()) as c:
            row = c.execute("SELECT * FROM articles").fetchone()
            self.assertEqual(row["title"], "测试讲座")
            self.assertEqual(row["source_name"], "测试公众号")
            self.assertIn("第一段中文\n第二段中文", row["content"])
            self.assertEqual(row["source_url"], "https://mp.weixin.qq.com/s/test-article")
            self.assertEqual(row["import_method"], "wechat2rss")
            self.assertEqual(c.execute("SELECT COUNT(*) FROM wechat2rss_entries").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM wechat2rss_images").fetchone()[0], 2)
            self.assertEqual(c.execute("SELECT name FROM wechat2rss_sources WHERE id=1").fetchone()[0], "测试公众号")
            self.assertEqual(c.execute("SELECT COUNT(*) FROM ai_analyses").fetchone()[0] if c.execute(
                "SELECT 1 FROM sqlite_master WHERE name='ai_analyses'").fetchone() else 0, 0)

    def test_bad_feed_does_not_stop_second(self):
        wechat2rss.import_opml(self.opml, self.db)
        def fake_fetch(url, **kwargs):
            if url == URL1:
                raise FetchError("timeout")
            return FEED, None, url
        logs = []
        self.assertEqual(wechat2rss.sync_sources(self.db, fetch=fake_fetch, report=logs.append), (1, 1, 1))
        self.assertIn("成功 1 个，失败 1 个，新增文章 1 篇", "\n".join(logs))
        self.assertNotIn("first-secret", "\n".join(logs))
        with contextlib.closing(self.connection()) as c:
            self.assertEqual([r[0] for r in c.execute("SELECT last_status FROM wechat2rss_sources ORDER BY id")],
                             ["failed", "success"])

    def test_existing_url_is_not_overwritten_and_delete_cascades(self):
        wechat2rss.import_opml(self.opml, self.db)
        with contextlib.closing(open_ai_database(self.db)):
            pass
        existing_id, _ = save_article({"title": "原手工标题", "content": "原手工正文",
                                       "source_url": "https://mp.weixin.qq.com/s/test-article"}, self.db)
        self.assertEqual(wechat2rss.sync_sources(self.db, first_only=True,
            fetch=lambda u, **k: (FEED, None, u), report=lambda _: None), (1, 0, 0))
        with contextlib.closing(self.connection()) as c:
            row = c.execute("SELECT title,content FROM articles WHERE id=?", (existing_id,)).fetchone()
            self.assertEqual(tuple(row), ("原手工标题", "原手工正文"))
            self.assertEqual(c.execute("SELECT article_id FROM wechat2rss_entries").fetchone()[0], existing_id)
        data_management.delete_article(existing_id, db_path=self.db, confirmation="DELETE")
        with contextlib.closing(self.connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM wechat2rss_entries").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM wechat2rss_images").fetchone()[0], 0)
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_empty_valid_feed_and_invalid_xml(self):
        wechat2rss.import_opml(self.opml, self.db)
        logs = []
        self.assertEqual(wechat2rss.sync_sources(self.db, first_only=True,
            fetch=lambda u, **k: (EMPTY, None, u), report=logs.append), (1, 0, 0))
        self.assertIn("没有可入库文章", "\n".join(logs))
        self.assertEqual(wechat2rss.sync_sources(self.db, first_only=True,
            fetch=lambda u, **k: (b'<html>bad', None, u), report=lambda _: None), (0, 1, 0))

    def test_sync_one_selected_source_without_touching_others(self):
        wechat2rss.import_opml(self.opml, self.db)
        calls = []
        def fake_fetch(url, **kwargs):
            calls.append(url)
            return FEED, None, url
        self.assertEqual(wechat2rss.sync_sources(self.db, source_id=2, fetch=fake_fetch,
                                                  report=lambda _: None), (1, 0, 1))
        self.assertEqual(calls, [URL2])
        with self.assertRaises(wechat2rss.SubscriptionError):
            wechat2rss.sync_sources(self.db, source_id=99, fetch=fake_fetch, report=lambda _: None)

    def test_opml_rejects_unsafe_targets_and_dtd(self):
        self.opml.write_text('<opml><body><outline xmlUrl="file:///etc/passwd"/></body></opml>', encoding='utf-8')
        with self.assertRaises(wechat2rss.SubscriptionError):
            wechat2rss.import_opml(self.opml, self.db)
        self.opml.write_text('<!DOCTYPE x><opml/>', encoding='utf-8')
        with self.assertRaises(wechat2rss.SubscriptionError):
            wechat2rss.import_opml(self.opml, self.db)

    def test_timer_invokes_sync_once_then_stops_without_ai(self):
        with patch.object(wechat2rss, "sync_sources", return_value=(1, 0, 0)) as sync, \
             patch.object(wechat2rss.time, "sleep", side_effect=KeyboardInterrupt) as sleeping:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(wechat2rss.cli(["--sync-every", "30"], self.db), 0)
        sync.assert_called_once_with(self.db, first_only=False, source_id=None)
        sleeping.assert_called_once_with(1800)

    def test_main_menu_eight_is_reachable_without_network(self):
        wechat2rss.import_opml(self.opml, self.db)
        with patch.object(main.sys, 'argv', ['main.py']), \
             patch('builtins.input', side_effect=['8', '4', '0', '4']), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main.main(self.db), 0)
        self.assertIn('已导入 2 个订阅', output.getvalue())


if __name__ == '__main__':
    unittest.main()
