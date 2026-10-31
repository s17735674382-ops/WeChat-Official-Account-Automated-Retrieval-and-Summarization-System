"""日报发布后每周清理；先备份、保留去重哈希，只触碰项目管理的数据。"""
import hashlib
import json
import re
import shutil
import sqlite3
import stat
from contextlib import closing, nullcontext
from datetime import datetime, timedelta
from pathlib import Path

from data_management import (backup_database, clean_candidates, connect_db, is_link,
                             plain_directory, write_journal)
from image_import import image_store_lock
from daily_reports import atomic
from wechat2rss import retired_keys


def regular(path):
    info = path.lstat()
    if is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('拒绝清理链接、硬链接或非普通文件')
    return info


def valid_report(root, now):
    folder = root/'reports'
    plain_directory(folder)
    regular(folder/'status.json')
    state = json.loads((folder/'status.json').read_text(encoding='utf-8'))
    day = now.date().isoformat()
    if state.get('report_date') != day or state.get('status') not in ('complete','partial'):
        raise ValueError('当天日报尚未可靠生成，禁止清理')
    for suffix, key in (('.md','markdown_sha256'),('.html','html_sha256')):
        path = folder/(day+suffix); regular(path)
        data = path.read_bytes()
        if (hashlib.sha256(data).hexdigest() != state.get(key) or
                state['report_id'] not in data.decode('utf-8') or
                state['generated_at'] not in data.decode('utf-8')):
            raise ValueError('报告标识、时间或哈希不匹配，禁止清理')
    return state


def remove_old_backups(root, cutoff):
    """仅清理本功能已完成、超过保留期的备份；既有人工备份不动。"""
    base = root/'backups'; plain_directory(base)
    count = 0
    for folder in base.iterdir():
        if not re.fullmatch(r'weekly_cleanup_[0-9T_+\-]+_[0-9a-f]{32}',folder.name):
            continue
        try:
            plain_directory(folder)
            journal = folder/'operation.json'; regular(journal)
            item = json.loads(journal.read_text(encoding='utf-8'))
            if (item.get('operation')!='weekly_retention' or item.get('status')!='complete' or
                    item.get('report_date','9999-12-31') >= cutoff):
                continue
            # 不跟随任何链接或联接点。逐个验证并删除精确位于本备份目录的文件。
            files, dirs = [], []
            pending = [folder]
            while pending:
                directory = pending.pop(); plain_directory(directory); dirs.append(directory)
                for path in directory.iterdir():
                    if not path.resolve().is_relative_to(folder.resolve()):
                        raise ValueError('备份路径越界')
                    info = path.lstat()
                    if is_link(info):
                        raise ValueError('备份内有链接，整份保留')
                    if stat.S_ISDIR(info.st_mode): pending.append(path)
                    else: regular(path); files.append(path)
            # 清单最后删除，中途故障可继续定位。
            for path in sorted(files,key=lambda p:p==journal): path.unlink()
            for path in reversed(dirs): path.rmdir()
            count += 1
        except (OSError,ValueError,KeyError):
            continue
    return count


def cleanup_after_report(db_path, now, days=30, *, locked=False):
    """每周日出报后执行；已执行过的系统支持下一次出报时补做错过的一周。"""
    if type(days) is not int or not 7<=days<=3650:
        raise ValueError('保留天数必须是7至3650的整数')
    root = Path(db_path).resolve().parent
    marker = root/'logs/weekly_cleanup.json'
    with nullcontext() if locked else image_store_lock(db_path):
        plain_directory(root/'logs',create=True)
        last = None
        if marker.exists():
            regular(marker)
            last = json.loads(marker.read_text(encoding='utf-8')).get('week')
        week = (now.date()-timedelta(days=(now.weekday()+1)%7)).isoformat()
        if last == week or (last is None and now.weekday()!=6):
            return None
        state = valid_report(root,now)
        threshold = now.replace(hour=0,minute=0,second=0,microsecond=0)-timedelta(days=days-1)
        cutoff = threshold.date().isoformat()
        folder, backup = backup_database(db_path,'weekly_cleanup')
        journal = folder/'operation.json'
        report = {'operation':'weekly_retention','report_date':now.date().isoformat(),
                  'week':week,'created_at':now.isoformat(),'status':'prepared',
                  'database_backup':str(backup),'database_state':'not_deleted',
                  'article_ids':[],'files':[],'deleted_reports':[], 'report_id':state['report_id']}
        write_journal(journal,report)
        with closing(connect_db(db_path,writable=True)) as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or c.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('数据库校验失败')
                rows = c.execute("SELECT * FROM articles WHERE import_method IN ('rss','wechat2rss')").fetchall()
                ids = []
                for row in rows:
                    try:
                        imported = datetime.fromisoformat(row['imported_at'])
                        if imported.tzinfo is None: continue
                        if imported >= threshold: continue
                    except (ValueError,TypeError): continue
                    aid = row['id']
                    if c.execute("SELECT 1 FROM daily_ai WHERE article_id=? AND status='running'",(aid,)).fetchone(): continue
                    if c.execute("SELECT 1 FROM ai_analyses WHERE article_id=? AND status='running'",(aid,)).fetchone(): continue
                    if c.execute('SELECT 1 FROM daily_members WHERE article_id=? AND report_date>=?',(aid,cutoff)).fetchone(): continue
                    ids.append(aid)
                # 核对备份覆盖待删记录，期间发生变化则整次回滚。
                with closing(sqlite3.connect(backup)) as saved:
                    for aid in ids:
                        live = tuple(c.execute('SELECT * FROM articles WHERE id=?',(aid,)).fetchone())
                        if saved.execute('SELECT * FROM articles WHERE id=?',(aid,)).fetchone()!=live:
                            raise ValueError('备份后数据变化，取消清理')
                candidates = {}
                for aid in ids:
                    row = c.execute('SELECT * FROM articles WHERE id=?',(aid,)).fetchone()
                    if row['source_url']:
                        c.execute('INSERT OR IGNORE INTO rss_retired VALUES(?,?)',('url',hashlib.sha256(row['source_url'].encode()).hexdigest()))
                    for old in c.execute('''SELECT s.feed_url,e.stable_key FROM wechat2rss_entries e
                            JOIN wechat2rss_sources s ON s.id=e.source_id WHERE e.article_id=?''',(aid,)):
                        for pair in retired_keys(old[0],{'stable_key':old[1],'source_url':row['source_url']}):
                            c.execute('INSERT OR IGNORE INTO rss_retired VALUES(?,?)',pair)
                    for old in c.execute('SELECT source_key,stable_key FROM daily_feed_entries WHERE article_id=?',(aid,)):
                        digest=hashlib.sha256((old[0]+':'+old[1]).encode()).hexdigest()
                        c.execute("INSERT OR IGNORE INTO rss_retired VALUES('stable',?)",(digest,))
                    for image in c.execute('SELECT stored_path,sha256 FROM article_images WHERE article_id=?',(aid,)):
                        candidates[image[0]]={'stored_path':image[0],'sha256':image[1],'state':'planned'}
                report['article_ids']=ids; report['files']=list(candidates.values());write_journal(journal,report)
                for aid in ids:
                    for table in ('ai_analyses','article_images','image_articles'):
                        c.execute('DELETE FROM '+table+' WHERE article_id=?',(aid,))
                    c.execute('DELETE FROM articles WHERE id=?',(aid,))
                # 保留最近成功截止点用于兼容旧的统计模式。
                last_complete = c.execute("SELECT MAX(report_date) FROM daily_runs WHERE status='complete'").fetchone()[0]
                c.execute('DELETE FROM daily_runs WHERE report_date<? AND report_date<>COALESCE(?,\'\')',(cutoff,last_complete))
                for table in ('daily_source_runs','daily_attempts','daily_members'):
                    c.execute('DELETE FROM '+table+' WHERE report_date<?',(cutoff,))
                if c.execute('PRAGMA foreign_key_check').fetchone(): raise ValueError('关联校验失败')
                c.commit()
            except BaseException:
                c.rollback(); raise
            report['database_state']='deleted'; write_journal(journal,report)
            # 数据库提交成功后才处理图片；复用哈希、共享引用和恢复副本检查。
            c.execute('BEGIN IMMEDIATE')
            try: clean_candidates(c,db_path,report['files'],folder,report,journal)
            finally: c.rollback()
            if ids: c.execute('VACUUM')
        reports = root/'reports'; recovery=folder/'reports';recovery.mkdir()
        for path in reports.iterdir():
            match = re.fullmatch(r'(?:status-)?(\d{4}-\d{2}-\d{2})(?:\.md|\.html|\.json)',path.name)
            if not match or match[1]>=cutoff: continue
            regular(path)
            data=path.read_bytes(); atomic(recovery/path.name,data)
            report['deleted_reports'].append(path.name);write_journal(journal,report)
            if path.read_bytes()!=data: raise ValueError('报告清理期间被修改')
            path.unlink()
        report['status']='complete' if not any(i['state']=='kept_error' for i in report['files']) else 'partial'
        write_journal(journal,report)
        # partial 也记本周已执行，防止不断产生备份；异常清单留待人工处理。
        atomic(marker,json.dumps({'week':week,'completed_at':now.isoformat(),'status':report['status'],
                                 'journal':str(journal)},ensure_ascii=False,indent=2).encode())
        remove_old_backups(root,cutoff)
        return report
