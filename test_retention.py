"""仅临时测试数据，无网络、无正式库删除。"""
import contextlib
import json
from datetime import timedelta
from unittest.mock import patch
import unittest
import test_daily as fixtures
import daily_pipeline as daily
import daily_retention as retention


class PolicyTests(unittest.TestCase):
    setUp=fixtures.DailyTests.setUp
    write_config=fixtures.DailyTests.write_config
    transport=fixtures.DailyTests.transport
    run_daily=fixtures.DailyTests.run_daily
    status=fixtures.DailyTests.status

    def test_yesterday_pending_is_not_analyzed_today(self):
        self.value['daily']['max_api_calls']=0;self.write_config();self.run_daily()
        self.calls.clear();self.value['daily']['max_api_calls']=None;self.write_config()
        self.run_daily(now=fixtures.T+timedelta(days=1))
        self.assertNotIn('api',self.calls)
        self.assertEqual(self.status()['articles_pending'],0)

    def test_dated_status_matches_pointer_and_report(self):
        self.run_daily()
        p=self.root/'reports'
        self.assertEqual((p/'status.json').read_bytes(),(p/'status-2026-10-29.json').read_bytes())
        self.assertIn('+08:00',self.status()['generated_at'])

    def old_article(self):
        self.run_daily()
        with contextlib.closing(daily.connect(self.db)) as c:
            c.execute("UPDATE articles SET imported_at='2026-08-01T10:00:00+08:00'")
            c.execute("UPDATE daily_members SET report_date='2026-08-01'");c.commit()

    def sunday(self):
        return fixtures.T+timedelta(days=3)

    def test_sunday_deletes_old_backed_up_records_preserves_dedup(self):
        self.old_article();self.run_daily(now=self.sunday(),report_only=True)
        result=retention.cleanup_after_report(self.db,self.sunday())
        self.assertEqual(len(result['article_ids']),1)
        self.assertTrue(__import__('pathlib').Path(result['database_backup']).is_file())
        with contextlib.closing(daily.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM articles').fetchone()[0],0)
            source=daily.sources(c,self.value)[0]
            entry=daily.parse_feed(fixtures.FEED,source['url'])[1][0]
            with c:self.assertEqual(daily.save(c,source,entry,self.sunday().isoformat()),0)
            self.assertFalse(c.execute('PRAGMA foreign_key_check').fetchone())
        self.assertIsNone(retention.cleanup_after_report(self.db,self.sunday()))

    def test_failed_backup_prevents_delete(self):
        self.old_article();self.run_daily(now=self.sunday(),report_only=True)
        with patch.object(retention,'backup_database',side_effect=OSError('测试备份失败')):
            with self.assertRaises(OSError):retention.cleanup_after_report(self.db,self.sunday())
        with contextlib.closing(daily.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM articles').fetchone()[0],1)

    def test_no_report_no_cleanup(self):
        daily.initialize(self.db)
        with self.assertRaises(OSError):retention.cleanup_after_report(self.db,self.sunday())

    def test_old_report_moves_to_recovery_and_current_stays(self):
        self.run_daily(now=self.sunday(),report_only=True)
        old=self.root/'reports/2026-08-01.md';old.write_text('【测试】旧报告',encoding='utf-8')
        result=retention.cleanup_after_report(self.db,self.sunday())
        self.assertFalse(old.exists())
        backup=__import__('pathlib').Path(result['database_backup']).parent
        self.assertEqual((backup/'reports/2026-08-01.md').read_text(encoding='utf-8'),'【测试】旧报告')
        self.assertTrue((self.root/'reports/2026-11-01.md').exists())

    def test_production_defaults_wechat_only(self):
        self.value['daily'].pop('wechat_only');self.write_config()
        self.assertTrue(daily.config(self.cfg)[1]['wechat_only'])

