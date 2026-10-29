"""免费数据管理测试。只操作独立临时库和测试副本，禁止真实 API。"""

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import data_management as dm
import image_import as images
import main
from test_v04 import png
from wechat import save_article


class ManagementTests(unittest.TestCase):
    def setUp(self):
        workspace = Path(__file__).resolve().parent
        self.temp = tempfile.TemporaryDirectory(prefix="ruc-test-only-v041-", dir=workspace)
        self.root = Path(self.temp.name).resolve()
        assert self.root.is_relative_to(workspace)
        self.addCleanup(self.temp.cleanup)
        self.db = self.root / "TEST_ONLY.db"
        self.source = self.root / "【测试原始图片，不许删除】.png"
        self.source.write_bytes(png())
        self.article_id, _ = images.import_images(self.source, self.db)
        self.snapshot = dm.read_snapshot(self.article_id, self.db)
        self.copy = self.root / self.snapshot["images"][0]["stored_path"]
        self.network = patch("socket.create_connection", side_effect=AssertionError("管理测试禁止联网"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.api = patch("deepseek_analysis.request_deepseek", side_effect=AssertionError("管理测试禁止 API"))
        self.api.start()
        self.addCleanup(self.api.stop)

    def connection(self):
        return contextlib.closing(dm.connect_db(self.db, writable=True))

    def remove(self, pictures=False, **kwargs):
        return dm.delete_article(self.article_id, db_path=self.db, delete_images=pictures,
                                 confirmation="DELETE", **kwargs)

    def assert_empty_relations(self):
        with self.connection() as c:
            for table in ("articles", "article_images", "image_articles", "ai_analyses"):
                self.assertEqual(c.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_confirmation_required_and_invalid_ids(self):
        for confirm in ("", "delete", "DELETE ALL"):
            with self.assertRaises(dm.ManagementError):
                dm.delete_article(self.article_id, db_path=self.db, delete_images=True, confirmation=confirm)
        for value in (True, 0, -1, "1", 2**64):
            with self.assertRaises(dm.ManagementError):
                dm.read_snapshot(value, self.db)
        self.assertEqual(dm.read_snapshot(self.article_id, self.db), self.snapshot)
        self.assertTrue(self.copy.exists())
        self.assertFalse((self.root / "backups").exists())

    def test_delete_db_only_backup_and_relations(self):
        report, journal = self.remove()
        self.assert_empty_relations()
        self.assertTrue(self.copy.exists())
        self.assertTrue(self.source.exists())
        self.assertEqual(report["database_state"], "deleted")
        self.assertEqual(report["files"][0]["state"], "kept_by_choice")
        self.assertEqual(json.loads(journal.read_text(encoding="utf-8")), report)
        with contextlib.closing(dm.connect_db(report["database_backup"])) as c:
            self.assertEqual(dm.snapshot(c, self.article_id), self.snapshot)

    def test_delete_with_image_backup_preserves_original_and_other_article(self):
        second = self.root / "【测试】第二篇.png"
        second.write_bytes(png(b"\xff\x00\x00"))
        other_id, _ = images.import_images(second, self.db)
        other = dm.read_snapshot(other_id, self.db)
        report, _ = self.remove(True)
        self.assertFalse(self.copy.exists())
        self.assertEqual(self.source.read_bytes(), png())
        self.assertEqual(dm.read_snapshot(other_id, self.db), other)
        self.assertEqual(report["files"][0]["state"], "removed_with_backup")
        self.assertEqual(Path(report["files"][0]["recovery_copy"]).read_bytes(), png())
        with self.connection() as c:
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])

    def add_reference(self, source_article_id, path=None):
        row = self.snapshot["images"][0]
        with self.connection() as c:
            c.execute("""INSERT INTO article_images
                (article_id,image_index,original_name,stored_path,sha256,mime_type,width,height,size_bytes)
                VALUES (?,1,?,?,?,?,?,?,?)""",
                (source_article_id, row["original_name"], path or row["stored_path"], row["sha256"],
                 row["mime_type"], row["width"], row["height"], row["size_bytes"]))

    def test_shared_image_and_path_alias_are_kept(self):
        other_id, _ = save_article({"title": "【测试】共享图片", "content": "【测试】正文"}, self.db)
        path = str(self.copy).upper() if os.name == "nt" else str(self.copy)
        self.add_reference(other_id, path)
        report, _ = self.remove(True)
        self.assertEqual(report["files"][0]["state"], "shared_kept")
        self.assertTrue(self.copy.exists())
        self.assertEqual(len(dm.read_snapshot(other_id, self.db)["images"]), 1)

    def test_backup_failure_does_not_delete_anything(self):
        with patch("data_management._backup_database", side_effect=OSError("【测试】磁盘满")), \
                patch("data_management.clean_candidates") as cleaner:
            with self.assertRaisesRegex(dm.ManagementError, "备份失败"):
                self.remove(True)
            cleaner.assert_not_called()
        self.assertEqual(dm.read_snapshot(self.article_id, self.db), self.snapshot)
        self.assertTrue(self.copy.exists())

    def test_database_failure_rolls_back_children_and_keeps_images(self):
        with self.connection() as c:
            c.execute("CREATE TRIGGER test_abort BEFORE DELETE ON articles BEGIN SELECT RAISE(ABORT,'测试：拒绝删除'); END")
        with patch("data_management.clean_candidates") as cleaner:
            with self.assertRaisesRegex(dm.ManagementError, "已回滚"):
                self.remove(True)
            cleaner.assert_not_called()
        self.assertEqual(dm.read_snapshot(self.article_id, self.db), self.snapshot)
        self.assertTrue(self.copy.exists())
        journals = list((self.root / "backups").glob("*/operation.json"))
        self.assertEqual(json.loads(journals[0].read_text(encoding="utf-8"))["database_state"], "not_deleted")

    def test_changed_preview_and_running_analysis_rejected(self):
        with self.connection() as c:
            c.execute("UPDATE articles SET title = ? WHERE id = ?", ("【测试】标题已改变", self.article_id))
        with self.assertRaisesRegex(dm.ManagementError, "已变化"):
            self.remove(True, expected=self.snapshot)
        with self.connection() as c:
            c.execute("UPDATE ai_analyses SET status='running'")
        with self.assertRaisesRegex(dm.ManagementError, "进行中"):
            self.remove(True)
        self.assertTrue(self.copy.exists())

    def test_change_after_backup_is_not_deleted(self):
        original_backup = dm.backup_database
        def backup_and_change(*args):
            result = original_backup(*args)
            with self.connection() as c:
                c.execute("UPDATE articles SET title='【测试】并发改动'")
            return result
        with patch("data_management.backup_database", side_effect=backup_and_change):
            with self.assertRaisesRegex(dm.ManagementError, "未执行删除"):
                self.remove(True)
        self.assertEqual(dm.read_snapshot(self.article_id, self.db)["article"]["title"], "【测试】并发改动")
        self.assertTrue(self.copy.exists())

    def test_outside_paths_and_unmanaged_files_never_deleted(self):
        with self.connection() as c:
            c.execute("UPDATE article_images SET stored_path = ?", (str(self.source),))
        report, _ = self.remove(True)
        self.assertEqual(report["files"][0]["state"], "kept_error")
        self.assertTrue(self.source.exists())
        self.assertTrue(self.copy.exists())
        unknown = self.root / "image_store" / "用户自己的文件.png"
        unknown.write_bytes(png())
        candidates, warnings = dm.find_orphans(self.db)
        self.assertEqual(len(candidates), 1)
        self.assertTrue(warnings)
        dm.cleanup_orphans(db_path=self.db, confirmation="DELETE", expected=candidates)
        self.assertTrue(unknown.exists())
        self.assertTrue(self.source.exists())

    def test_corrupt_image_is_kept_with_recovery_information(self):
        self.copy.write_bytes(b"modified test file")
        report, journal = self.remove(True)
        self.assertEqual(report["files"][0]["state"], "kept_error")
        self.assertIn("哈希", report["files"][0]["error"])
        self.assertTrue(self.copy.exists())
        self.assertTrue(journal.exists())
        self.assertTrue(Path(report["database_backup"]).exists())

    def test_image_unlink_failure_retains_backup_and_manifest(self):
        unlink = Path.unlink
        def fail_target(path, *args, **kwargs):
            if path == self.copy:
                raise PermissionError("【测试】图片正在被占用")
            return unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail_target):
            report, journal = self.remove(True)
        self.assert_empty_relations()
        self.assertTrue(self.copy.exists())
        self.assertEqual(report["files"][0]["state"], "kept_error")
        self.assertEqual(Path(report["files"][0]["recovery_copy"]).read_bytes(), png())
        self.assertIn("图片正在被占用", journal.read_text(encoding="utf-8"))

    def test_journal_failure_stops_before_database_delete(self):
        with patch("data_management.write_journal", side_effect=OSError("【测试】清单不可写")):
            with self.assertRaises(OSError):
                self.remove(True)
        self.assertEqual(dm.read_snapshot(self.article_id, self.db), self.snapshot)
        self.assertTrue(self.copy.exists())

    def test_post_commit_journal_failure_does_not_touch_images(self):
        write = dm.write_journal
        def fail_after_commit(path, report):
            if report["database_state"] == "deleted":
                raise OSError("【测试】提交后清单不可写")
            return write(path, report)
        with patch("data_management.write_journal", side_effect=fail_after_commit):
            with self.assertRaisesRegex(dm.ManagementError, "数据库记录已删除"):
                self.remove(True)
        self.assert_empty_relations()
        self.assertTrue(self.copy.exists())
        with contextlib.closing(dm.connect_db(next((self.root / "backups").glob("*/database.db")))) as c:
            self.assertEqual(dm.snapshot(c, self.article_id), self.snapshot)

    def test_cleanup_only_orphans_keeps_referenced_image_and_database(self):
        second = self.root / "【测试】有效引用.png"
        second.write_bytes(png(b"\x00\xff\x00"))
        other_id, _ = images.import_images(second, self.db)
        other = dm.read_snapshot(other_id, self.db)
        self.remove(False)
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        candidates, _ = dm.find_orphans(self.db)
        self.assertEqual(len(candidates), 1)
        with self.assertRaises(dm.ManagementError):
            dm.cleanup_orphans(db_path=self.db)
        report, _ = dm.cleanup_orphans(db_path=self.db, confirmation="DELETE", expected=candidates)
        self.assertFalse(self.copy.exists())
        self.assertEqual(dm.read_snapshot(other_id, self.db), other)
        self.assertEqual(report["files"][0]["state"], "removed_with_backup")
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)

    def test_new_reference_after_orphan_preview_is_protected(self):
        self.remove(False)
        candidates, _ = dm.find_orphans(self.db)
        other_id, _ = save_article({"title": "【测试】新增引用", "content": "【测试】正文"}, self.db)
        self.add_reference(other_id)
        report, _ = dm.cleanup_orphans(db_path=self.db, confirmation="DELETE", expected=candidates)
        self.assertEqual(report["files"][0]["state"], "shared_kept")
        self.assertTrue(self.copy.exists())

    def test_hardlinks_and_reparse_points_are_not_cleaned(self):
        hardlink = self.root / "【测试】用户硬链接.png"
        os.link(self.copy, hardlink)
        self.remove(False)
        candidates, warnings = dm.find_orphans(self.db)
        self.assertEqual(candidates, [])
        self.assertTrue(warnings)
        self.assertTrue(hardlink.exists())
        self.assertTrue(dm.is_link(SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)))
        with self.assertRaises(dm.ManagementError):
            dm.checked_image(self.db, "image_store/../original.png")

    def test_shared_import_lock_and_no_delete_while_locked(self):
        with images.image_store_lock(self.db):
            with self.assertRaisesRegex(ValueError, "另一个操作"):
                self.remove(True)
            with self.assertRaisesRegex(ValueError, "另一个操作"):
                images.import_images(self.source, self.db)
        self.assertEqual(dm.read_snapshot(self.article_id, self.db), self.snapshot)

    @unittest.skipUnless(os.name == "nt", "Windows 联接点测试")
    def test_actual_windows_junction_never_deletes_outside_store(self):
        import _winapi
        outside = self.root / "【测试】非图片库原始文件夹"
        outside.mkdir()
        original = outside / self.copy.name
        original.write_bytes(png())
        junction = self.root / "image_store" / ("e" * 64)
        _winapi.CreateJunction(str(outside), str(junction))
        self.addCleanup(junction.rmdir)  # 只移除联接点本身，不递归处理目标目录。
        candidates, warnings = dm.find_orphans(self.db)
        self.assertEqual(candidates, [])
        self.assertTrue(warnings)
        with self.connection() as c:
            c.execute("UPDATE article_images SET stored_path = ?", (f"image_store/{'e'*64}/{self.copy.name}",))
        report, _ = self.remove(True)
        self.assertEqual(report["files"][0]["state"], "kept_error")
        self.assertEqual(original.read_bytes(), png())

    def test_actual_menu_delete_requires_delete_and_keeps_default_images(self):
        answers = ["7", "2", str(self.article_id), "", "DELETE", "0", "4"]
        output = io.StringIO()
        with patch("sys.argv", ["main.py"]), patch("builtins.input", side_effect=answers), \
                contextlib.redirect_stdout(output):
            self.assertEqual(main.main(self.db), 0)
        self.assert_empty_relations()
        self.assertTrue(self.copy.exists())
        self.assertIn("关联分析 1 条，图片记录 1 条", output.getvalue())
        self.assertIn("按选择保留 1 张", output.getvalue())
        self.assertTrue(self.source.exists())

    def test_listing_space_and_actual_menu_cancel_are_read_only(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        rows = dm.list_articles(self.db)
        self.assertEqual(rows[0]["id"], self.article_id)
        self.assertEqual(rows[0]["image_count"], 1)
        self.assertEqual(rows[0]["analysis_count"], 1)
        self.assertEqual(rows[0]["imported_at"], self.snapshot["article"]["imported_at"])
        usage = dm.disk_usage(self.db)
        self.assertEqual(usage["database_bytes"], self.db.stat().st_size)
        self.assertEqual(usage["image_store"]["bytes"], len(png()))
        output = io.StringIO()
        answers = ["7", "1", "", "4", "2", str(self.article_id), "Y", "cancel", "0", "4"]
        with patch("sys.argv", ["main.py"]), patch("builtins.input", side_effect=answers), \
                contextlib.redirect_stdout(output):
            self.assertEqual(main.main(self.db), 0)
        self.assertIn("关联分析 1 条，图片记录 1 条", output.getvalue())
        self.assertIn("已取消，没有删除", output.getvalue())
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        self.assertTrue(self.copy.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
