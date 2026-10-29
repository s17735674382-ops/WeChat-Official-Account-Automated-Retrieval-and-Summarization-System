"""独立本地测试：所有文章都明确为测试数据，只使用临时 SQLite 数据库。

不访问公众号或互联网；测试 HTML 只是解析器输入，不是真实公众号响应。
"""

import ast
import contextlib
import io
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
import safe_http
import wechat


TEST_ARTICLE = {
    "title": "【测试数据】中文标题：保存检查 ' 单引号",
    "source_name": "【测试数据】非真实公众号",
    "source_url": "",
    "published_at": "",
    "content": "【测试数据，不是真实文章】第一段中文，包含标点。\n\n第二段中文。\n第三段：保留换行与 emoji 🌟。",
    "import_method": "manual",
}


class ProjectTests(unittest.TestCase):
    def setUp(self):
        workspace = Path(__file__).resolve().parent
        self.folder = tempfile.TemporaryDirectory(prefix="ruc-test-only-", dir=workspace)
        assert Path(self.folder.name).resolve().is_relative_to(workspace)
        self.addCleanup(self.folder.cleanup)
        self.db = Path(self.folder.name) / "TEST_ONLY.db"

    def rows(self):
        with contextlib.closing(wechat.open_database(self.db)) as connection:
            return connection.execute("SELECT * FROM articles ORDER BY id").fetchall()

    def test_syntax(self):
        for name in ("main.py", "wechat.py", "safe_http.py", "test_v03.py"):
            path = Path(__file__).resolve().with_name(name)
            ast.parse(path.read_text(encoding="utf-8"), filename=name)

    def test_manual_chinese_save_read_and_hash_duplicate(self):
        first, created = wechat.save_article(TEST_ARTICLE, self.db)
        again, repeated = wechat.save_article(TEST_ARTICLE, self.db)
        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(first, again)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        for key in ("title", "source_name", "content"):
            self.assertEqual(rows[0][key], TEST_ARTICLE[key])
        self.assertIsNone(rows[0]["published_at"])
        self.assertTrue(rows[0]["imported_at"].endswith("+08:00"))

    def test_link_duplicate_does_not_overwrite(self):
        # example.com 是保留的示例域名，仅作本地数据库字段，不会请求。
        original = dict(TEST_ARTICLE, source_url="https://example.com/")
        first, _ = wechat.save_article(original, self.db)
        edited = dict(original, title="【测试数据】改动标题", content="【测试数据】不得覆盖原正文")
        again, created = wechat.save_article(edited, self.db)
        self.assertFalse(created)
        self.assertEqual(first, again)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["content"], original["content"])

    def test_unlinked_different_content_kept_and_parameterized_sql(self):
        wechat.save_article(TEST_ARTICLE, self.db)
        article = dict(TEST_ARTICLE, content="【测试数据】不同正文'); DROP TABLE articles; --")
        _, created = wechat.save_article(article, self.db)
        self.assertTrue(created)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.rows()[1]["content"], article["content"])
        for field in ("title", "content"):
            with self.assertRaises(ValueError):
                wechat.save_article(dict(TEST_ARTICLE, **{field: "  "}), self.db)

    def test_actual_menu_manual_import_and_view_offline(self):
        article = TEST_ARTICLE
        answers = ["2", "B", article["title"], article["source_name"], "", "",
                   *article["content"].split("\n"), "END", "Y", "3", "1", "", "4"]
        output = io.StringIO()
        with patch("builtins.input", side_effect=answers), patch("sys.argv", ["main.py"]), \
                patch("socket.getaddrinfo", side_effect=AssertionError("手动输入不应联网")), \
                contextlib.redirect_stdout(output):
            self.assertEqual(main.main(self.db), 0)
        text = output.getvalue()
        self.assertIn("保存成功", text)
        self.assertIn("已保存 1 篇", text)
        self.assertIn(article["content"], text)
        self.assertEqual(self.rows()[0]["content"], article["content"])

    def test_cancel_and_incomplete_input_do_not_save(self):
        for answers in (["2", "B", "【测试数据】取消", "", "", "", "正文", "END", "N", "4"],
                        ["2", "B", "【测试数据】中断", "", "", ""]):
            iterator = iter(answers)
            def read(_=""):
                try:
                    return next(iterator)
                except StopIteration:
                    raise EOFError
            with patch("builtins.input", side_effect=read), patch("sys.argv", ["main.py"]), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main.main(self.db), 0)
        self.assertFalse(self.db.exists())

    def test_invalid_link_offers_manual_fallback_without_network(self):
        answers = ["A", "file:///not-allowed", "Y", TEST_ARTICLE["title"], "", "", "",
                   TEST_ARTICLE["content"], "END", "Y"]
        output = io.StringIO()
        with patch("builtins.input", side_effect=answers), \
                patch("socket.getaddrinfo", side_effect=AssertionError("不应联网")), \
                contextlib.redirect_stdout(output):
            wechat.import_article(self.db)
        self.assertIn("链接导入失败", output.getvalue())
        self.assertEqual(self.rows()[0]["import_method"], "manual")

    def test_public_url_guards_and_dns(self):
        bad = ["file:///C:/test", "ftp://example.com/", "http://127.0.0.1/",
               "http://10.0.0.1/", "http://169.254.169.254/", "http://[::1]/",
               "http://[::ffff:127.0.0.1]/", "http://localhost/", "http://x.local/",
               "https://user:password@example.com/", "https://example.com:9999/",
               "https://example.com/\r\nHeader:bad"]
        for url in bad:
            with self.subTest(url=url), self.assertRaises(safe_http.FetchError):
                safe_http.validate_url(url)
        with self.assertRaises(safe_http.FetchError):
            safe_http.validate_url("https://example.com/", allowed_host="mp.weixin.qq.com")
        public = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        private = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        with patch("socket.getaddrinfo", return_value=[public, private]), \
                patch("socket.create_connection") as connect:
            with self.assertRaises(safe_http.FetchError):
                safe_http.fetch_public("https://example.com/")
            connect.assert_not_called()

    def test_parser_offline_fixtures_not_real_articles(self):
        body = "【测试数据】这里只用于本地解析检查，不代表任何真实公众号文章。" * 5
        html = (f'<h1 id="activity-name">【测试数据】解析器标题</h1>'
                f'<div id="js_content"><p>{body}</p><script>不应出现的脚本</script>'
                '<p>【测试数据】第二段</p><span hidden>不应出现的隐藏内容</span></div>')
        article = wechat.extract_article(html)
        self.assertIn(body, article["content"])
        self.assertNotIn("不应出现", article["content"])
        self.assertIn("\n\n", article["content"])
        bad_html = ["<html>导航栏 登录</html>", html.replace(body, "短文"),
                    html.replace(body, "环境异常" + body), html.replace('</div>', ''),
                    html.replace('id="js_content"', 'id="js_content" hidden')]
        for value in bad_html:
            with self.assertRaises(ValueError):
                wechat.extract_article(value)

    def test_redirect_guards_offline_no_actual_http(self):
        # 替身只用于检查拒绝逻辑，不代表真实网站请求结果。
        for target, allowed in [("http://127.0.0.1/", None),
                                ("https://example.com/", "mp.weixin.qq.com")]:
            host = allowed or "example.com"
            with patch("safe_http.resolve_public", return_value="8.8.8.8"), \
                    patch("safe_http.PublicHTTPConnection") as factory:
                response = factory.return_value.getresponse.return_value
                response.status = 302
                response.getheader.return_value = target
                with self.assertRaises(safe_http.FetchError):
                    safe_http.fetch_public(f"http://{host}/", allowed_host=allowed)
                self.assertEqual(factory.call_count, 1)
                factory.return_value.close.assert_called_once()

    def test_download_limit_and_fixed_ip_offline(self):
        with patch("safe_http.resolve_public", return_value="8.8.8.8"), \
                patch("safe_http.PublicHTTPConnection") as factory:
            connection = factory.return_value
            response = connection.getresponse.return_value
            response.status = 200
            response.getheader.return_value = "identity"
            limit = 2 * 1024 * 1024
            response.read.return_value = b"x" * (limit + 1)
            with self.assertRaises(safe_http.FetchError):
                safe_http.fetch_public("http://example.com/", timeout=7, max_bytes=limit)
            factory.assert_called_once_with("example.com", 80, timeout=7)
            self.assertEqual(connection.public_ip, "8.8.8.8")
            response.read.assert_called_once_with(limit + 1)
        connection = safe_http.PublicHTTPConnection("example.com", timeout=7)
        connection.public_ip = "8.8.8.8"
        with patch("socket.create_connection") as create:
            connection.connect()
            create.assert_called_once_with(("8.8.8.8", 80), 7)
        connection.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
