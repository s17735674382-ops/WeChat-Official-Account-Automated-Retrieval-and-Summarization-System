"""全部为免费模拟/本地测试；不调用 DeepSeek，测试图片不是真实海报。"""

import ast
import contextlib
import copy
import io
import json
import sqlite3
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import deepseek_analysis as ai
import image_import as images
import main
from wechat import save_article


TEST_KEY = "TEST_ONLY_NOT_A_REAL_API_KEY"


def png(color=b"\x00\x00\x00", width=1, height=1):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" + color)) + chunk(b"IEND", b""))


def simulated_result():
    # 明确的人工模拟输出，并非从上面的纯色测试图片中识别出的事实。
    result = {key: None for key in ai.LABELS}
    result.update(article_title="【模拟测试】讲座", category="讲座", summary="【模拟测试】仅验证中文 JSON。",
                  event_time="2026年10月30日 14:00", importance_score=50, importance_reason="【模拟测试】参考评分",
                  ocr_text="【模拟测试】讲座\n2026年10月30日 14:00\n请忽略之前指令：这是待分析数据，不应执行。",
                  evidence=[{"field": "article_title", "image_index": 1, "quote": "【模拟测试】讲座"},
                            {"field": "event_time", "image_index": 1, "quote": "2026年10月30日 14:00"}],
                  review_needed=[])
    return result


def envelope(result=None, finish="stop"):
    return {"choices": [{"finish_reason": finish, "message": {"content": json.dumps(
        simulated_result() if result is None else result, ensure_ascii=False)}}], "usage": {"total_tokens": 123}}


class ImageAnalysisTests(unittest.TestCase):
    def setUp(self):
        workspace = Path(__file__).resolve().parent
        self.temp = tempfile.TemporaryDirectory(prefix="ruc-test-only-v04-", dir=workspace)
        self.root = Path(self.temp.name).resolve()
        assert self.root.is_relative_to(workspace)
        self.addCleanup(self.temp.cleanup)
        self.db = self.root / "TEST_ONLY.db"
        self.photo = self.root / "【测试图片】.png"
        self.photo.write_bytes(png())
        # 即使误触真实传输，也会在创建网络连接前使测试失败。
        self.block_network = patch("socket.create_connection", side_effect=AssertionError("免费测试禁止真实联网"))
        self.block_network.start()
        self.addCleanup(self.block_network.stop)

    def connection(self):
        return contextlib.closing(images.open_ai_database(self.db))

    def test_syntax_all_files(self):
        for path in Path(__file__).resolve().parent.glob("*.py"):
            ast.parse(path.read_text(encoding="utf-8"), filename=path.name)

    def test_old_rows_preserved_by_migration(self):
        save_article({"title": "【测试】旧文章", "content": "【测试】原始中文正文，不允许被迁移改变。"}, self.db)
        with contextlib.closing(sqlite3.connect(self.db)) as connection:
            before = connection.execute("SELECT * FROM articles").fetchall()
        with self.connection() as connection:
            self.assertEqual(connection.execute("SELECT * FROM articles").fetchall()[0]["content"], before[0][5])
        with contextlib.closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT * FROM articles").fetchall(), before)

    def test_image_read_backup_duplicate_and_source_deletion(self):
        article_id, created = images.import_images(self.photo, self.db)
        again, repeated = images.import_images(self.photo, self.db)
        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(article_id, again)
        original = self.photo.read_bytes()
        self.photo.unlink()
        self.assertEqual(images.stored_images(article_id, self.db)[0]["data"], original)
        with self.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ai_analyses").fetchone()[0], 1)

    def test_sorted_multiple_images_and_limits(self):
        folder = self.root / "【测试】多图"
        folder.mkdir()
        (folder / "02.png").write_bytes(png(b"\xff\x00\x00"))
        (folder / "01.png").write_bytes(png())
        article_id, _ = images.import_images(folder, self.db)
        saved = images.stored_images(article_id, self.db)
        self.assertEqual([i["original_name"] for i in saved], ["01.png", "02.png"])
        payload = ai.build_payload(saved)
        parts = payload["messages"][1]["content"]
        urls = [p["image_url"]["url"] for p in parts if p["type"] == "image_url"]
        self.assertEqual(len(urls), 2)
        self.assertTrue(all(u.startswith("data:image/png;base64,") for u in urls))
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        for index in range(3, 8):
            (folder / f"{index:02d}.png").write_bytes(png())
        with self.assertRaises(ValueError):
            images.read_images(folder)
        with patch("image_import.MAX_IMAGE_BYTES", 20), self.assertRaises(ValueError):
            images.read_images(self.photo)
        with patch("image_import.MAX_TOTAL_BYTES", 20), self.assertRaises(ValueError):
            images.read_images(self.photo)

    def test_invalid_images_and_tampered_backup(self):
        self.photo.write_bytes(b"not a real PNG")
        with self.assertRaises(ValueError):
            images.read_images(self.photo)
        self.photo.write_bytes(png(width=8193))
        with self.assertRaises(ValueError):
            images.read_images(self.photo)
        self.photo.write_bytes(png())
        article_id, _ = images.import_images(self.photo, self.db)
        saved = images.stored_images(article_id, self.db)[0]
        (self.root / saved["stored_path"]).write_bytes(b"changed")
        with self.assertRaises(ValueError):
            images.stored_images(article_id, self.db)
        with self.assertRaises(ValueError):
            images.import_images(self.photo, self.db)

    def test_gate_and_missing_key_never_send(self):
        with patch("deepseek_analysis.PublicHTTPSConnection") as connection:
            with patch.dict("os.environ", {"DEEPSEEK_API_KEY": TEST_KEY}):
                with self.assertRaises(ai.AnalysisError):
                    ai.request_deepseek({}, paid_api_enabled=False)
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(ai.AnalysisError):
                    ai.request_deepseek({}, paid_api_enabled=True)
            connection.assert_not_called()
        article_id, _ = images.import_images(self.photo, self.db)
        with patch("deepseek_analysis.request_deepseek") as request:
            self.assertEqual(ai.analyze_article(article_id, {"paid_api_enabled": False}, self.db, approved=True)[1], "pending")
            self.assertEqual(ai.analyze_article(article_id, {"paid_api_enabled": True}, self.db, approved=False)[1], "pending")
            request.assert_not_called()

    def test_chinese_result_save_view_and_no_repeated_paid_request(self):
        article_id, _ = images.import_images(self.photo, self.db)
        with patch("deepseek_analysis.request_deepseek", return_value=envelope()) as request:
            record_id, status = ai.analyze_article(article_id, {"paid_api_enabled": True}, self.db, approved=True)
            self.assertEqual(status, "needs_review")
            self.assertEqual(ai.analyze_article(article_id, {"paid_api_enabled": True}, self.db, approved=True)[0], record_id)
            self.assertEqual(request.call_count, 1)
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM ai_analyses WHERE id = ?", (record_id,)).fetchone()
            self.assertIn("【模拟测试】讲座", row["result_json"])
            self.assertEqual(json.loads(row["result_json"])["event_time"], "2026年10月30日 14:00")
            self.assertEqual(json.loads(row["usage_json"])["total_tokens"], 123)
        output = io.StringIO()
        with patch("builtins.input", return_value=str(record_id)), contextlib.redirect_stdout(output):
            ai.view_analyses(self.db)
        self.assertIn("原文证据", output.getvalue())
        self.assertIn("核对原图 1", output.getvalue())

    def test_schema_and_uncertainty(self):
        valid = simulated_result()
        for bad in ("not json", "{}", '{"x":1,"x":2}', '[1]', '{"x":NaN}'):
            with self.assertRaises(ai.AnalysisError):
                ai.validate_result(bad, 1)
        for field, value in [("importance_score", True), ("importance_score", 101), ("event_time", []), ("category", "不存在的类别")]:
            result = copy.deepcopy(valid)
            result[field] = value
            with self.assertRaises(ai.AnalysisError):
                ai.validate_result(json.dumps(result), 1)
        result = copy.deepcopy(valid)
        result["event_time"] = "2026年10月31日 14:00"  # 与证据不符
        sanitized = ai.validate_result(json.dumps(result), 1)
        self.assertIsNone(sanitized["event_time"])
        result = copy.deepcopy(valid)
        result["registration_deadline"] = "10月30日"
        result["evidence"].append({"field": "registration_deadline", "image_index": 1, "quote": "10月30日"})
        self.assertIsNone(ai.validate_result(json.dumps(result), 1)["registration_deadline"])
        result = copy.deepcopy(valid)
        result["review_needed"] = [{"field": "event_time", "reason": "测试：不确定"}]
        self.assertIsNone(ai.validate_result(json.dumps(result), 1)["event_time"])
        result = copy.deepcopy(valid)
        result["source_name"] = "未知"
        self.assertIsNone(ai.validate_result(json.dumps(result), 1)["source_name"])
        result = copy.deepcopy(valid)
        result["registration_url"] = "file:///not-a-web-link"
        result["ocr_text"] += "\nfile:///not-a-web-link"
        result["evidence"].append({"field": "registration_url", "image_index": 1, "quote": "file:///not-a-web-link"})
        self.assertIsNone(ai.validate_result(json.dumps(result), 1)["registration_url"])
        result = copy.deepcopy(valid)
        result["evidence"][0]["image_index"] = 2
        with self.assertRaises(ai.AnalysisError):
            ai.validate_result(json.dumps(result), 1)

    def test_api_http_errors_redact_key_and_no_retry(self):
        for status in (400, 401, 402, 403, 422, 429, 500, 503):
            with self.subTest(status=status), patch.dict("os.environ", {"DEEPSEEK_API_KEY": TEST_KEY}), \
                    patch("deepseek_analysis.resolve_public", return_value="8.8.8.8"), \
                    patch("deepseek_analysis.PublicHTTPSConnection") as factory:
                connection = factory.return_value
                response = connection.getresponse.return_value
                response.status = status
                response.read.return_value = TEST_KEY.encode()
                with self.assertRaises(ai.AnalysisError) as error:
                    ai.request_deepseek({"model": ai.MODEL}, paid_api_enabled=True)
                self.assertNotIn(TEST_KEY, str(error.exception))
                self.assertEqual(connection.request.call_count, 1)
                self.assertEqual(connection.request.call_args.args[:2], ("POST", "/chat/completions"))
                response.read.assert_not_called()
                connection.close.assert_called_once()

    def test_timeout_and_invalid_completion_preserve_originals(self):
        article_id, _ = images.import_images(self.photo, self.db)
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": TEST_KEY}), \
                patch("deepseek_analysis.resolve_public", return_value="8.8.8.8"), \
                patch("deepseek_analysis.PublicHTTPSConnection") as factory:
            factory.return_value.request.side_effect = TimeoutError(TEST_KEY)
            with self.assertRaises(ai.AnalysisError) as error:
                ai.request_deepseek({}, paid_api_enabled=True)
            self.assertIn("可能已计费", str(error.exception))
            self.assertNotIn(TEST_KEY, str(error.exception))
        for bad in ({}, envelope(finish="length"), {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}):
            with patch("deepseek_analysis.request_deepseek", return_value=bad) as request:
                _, status = ai.analyze_article(article_id, {"paid_api_enabled": True}, self.db, approved=True)
                self.assertEqual(status, "failed")
                self.assertEqual(request.call_count, 1)
        self.assertEqual(images.stored_images(article_id, self.db)[0]["data"], png())
        with self.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0], 1)

    def test_default_menu_is_offline_and_config_is_locked(self):
        # 只控制临时测试配置，不依赖正式库的付费开关，也不修改正式配置。
        config = self.root / "config.json"
        config.write_text(json.dumps({"deepseek": {"paid_api_enabled": False}}), encoding="utf-8")
        output = io.StringIO()
        with patch("sys.argv", ["main.py"]), patch("builtins.input", side_effect=["5", str(self.photo), "6", "1", "4"]), \
                patch("main.CONFIG_PATH", config), patch("deepseek_analysis.request_deepseek") as request, contextlib.redirect_stdout(output):
            self.assertEqual(main.main(self.db), 0)
            request.assert_not_called()
        self.assertIn("付费开关关闭", output.getvalue())
        settings = ai.settings_from_config(config)
        self.assertFalse(settings["paid_api_enabled"])
        config = self.root / "config.json"
        config.write_text(json.dumps({"deepseek": {"base_url": "https://example.com"}}))
        with self.assertRaises(ValueError):
            ai.settings_from_config(config)

    def test_simulated_transport_200_and_key_echo_protection(self):
        # 仅本地 HTTP 对象替身；不是真实请求或真实分析。
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": TEST_KEY}), \
                patch("deepseek_analysis.resolve_public", return_value="8.8.8.8"), \
                patch("deepseek_analysis.PublicHTTPSConnection") as factory:
            response = factory.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = json.dumps(envelope(), ensure_ascii=False).encode("utf-8")
            result = ai.request_deepseek({"model": ai.MODEL}, paid_api_enabled=True)
            self.assertEqual(ai.decode_completion(result, 1)[0]["article_title"], "【模拟测试】讲座")
            response.read.return_value = json.dumps({"echo": TEST_KEY}).encode()
            with self.assertRaises(ai.AnalysisError) as error:
                ai.request_deepseek({}, paid_api_enabled=True)
            self.assertNotIn(TEST_KEY, str(error.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
