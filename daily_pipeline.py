"""每日采集入口。绝对路径、单实例锁、持久去重、有限处理与故障日报。"""
import hashlib
import json
import multiprocessing as mp
import os
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl

from wechat import DB_PATH
from image_import import open_ai_database, image_store_lock, MAX_TOTAL_BYTES, stored_images
from wechat2rss import open_sync_database, normalized_url, parse_feed, save_entry, safe_failure, SubscriptionError
from deepseek_analysis import settings_from_config, request_deepseek, AnalysisError
from daily_analysis import make_payload, validate, checked_download, legacy_result
from daily_reports import render, publish, atomic
from safe_http import fetch_public

BJ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent
DEFAULTS = {'enabled': True, 'editorial_enabled': True, 'max_api_calls': None, 'api_timeout_seconds': 60,
            'api_retries': 0, 'rss_timeout_seconds': 20, 'rss_retries': 1,
            'total_timeout_seconds': 540, 'collection_timeout_seconds': 300,
            'max_images_per_article': 3}

def clock():
    return datetime.now(BJ)

def worker(pipe, kind, value, timeout):
    try:
        if kind == 'fetch':
            data, _, _ = fetch_public(value, timeout=timeout, max_bytes=5*1024*1024)
            pipe.send(('ok', data))
        else:
            pipe.send(('ok', request_deepseek(value, paid_api_enabled=True, timeout=timeout)))
    except Exception as exc:
        # 服务端回显和原始网络异常不能进入日志。
        msg = str(exc) if isinstance(exc, AnalysisError) else safe_failure(exc)
        pipe.send(('error', msg))
    finally:
        pipe.close()

def bounded(kind, value, timeout):
    if timeout <= 0:
        raise TimeoutError('整体时间上限已到')
    context = mp.get_context('spawn')
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=worker, args=(child, kind, value, max(1, int(timeout))))
    process.start()
    child.close()
    try:
        if not parent.poll(timeout):
            raise TimeoutError('请求达到硬超时；API 结果可能未知，禁止自动重试')
        kind, result = parent.recv()
        if kind != 'ok':
            raise AnalysisError(result)
        return result
    finally:
        if process.is_alive():
            process.terminate()
        process.join(3)
        parent.close()

def connect(path):
    c = sqlite3.connect(path, timeout=5)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c

def initialize(path):
    path = Path(path).resolve()
    if path.exists():
        with closing(connect(path)) as c:
            if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or c.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('数据库校验失败')
            missing = not c.execute("SELECT 1 FROM sqlite_master WHERE name='daily_runs'").fetchone()
            if missing:
                folder = path.parent / 'backups'
                folder.mkdir(exist_ok=True)
                with closing(sqlite3.connect(folder / ('articles_before_daily_' + uuid.uuid4().hex + '.db'))) as dst:
                    c.backup(dst)
    with closing(open_ai_database(path)), closing(open_sync_database(path)):
        pass
    with closing(connect(path)) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS daily_runs(
          report_date TEXT PRIMARY KEY, window_start TEXT NOT NULL, cutoff_at TEXT, status TEXT,
          report_id TEXT, generated_at TEXT);
        CREATE TABLE IF NOT EXISTS daily_source_runs(
          report_date TEXT, source_key TEXT, name TEXT, status TEXT, attempts INTEGER DEFAULT 0,
          checked_at TEXT, new_count INTEGER DEFAULT 0, error TEXT, PRIMARY KEY(report_date,source_key));
        CREATE TABLE IF NOT EXISTS daily_feed_entries(
          source_key TEXT, stable_key TEXT, article_id INTEGER REFERENCES articles(id) ON DELETE CASCADE,
          PRIMARY KEY(source_key,stable_key));
        CREATE TABLE IF NOT EXISTS daily_queue(
          article_id INTEGER PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE, discovered_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS daily_members(
          report_date TEXT, article_id INTEGER REFERENCES articles(id) ON DELETE CASCADE,
          PRIMARY KEY(report_date,article_id));
        CREATE TABLE IF NOT EXISTS daily_ai(
          article_id INTEGER PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
          status TEXT, result_json TEXT, error TEXT, warning TEXT, model TEXT, analyzed_at TEXT);
        CREATE TABLE IF NOT EXISTS daily_attempts(
          id INTEGER PRIMARY KEY, report_date TEXT, article_id INTEGER REFERENCES articles(id) ON DELETE SET NULL,
          started_at TEXT, status TEXT);
        ''')

def config(path):
    value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    options = DEFAULTS | value.get('daily', {})
    if type(options['enabled']) is not bool:
        raise ValueError('daily.enabled 必须为 true/false')
    if type(options['editorial_enabled']) is not bool:
        raise ValueError('daily.editorial_enabled 必须为 true/false')
    limit = options['max_api_calls']
    if limit is not None and (type(limit) is not int or limit < 0):
        raise ValueError('daily.max_api_calls 必须为 null（不限次数）或非负整数')
    ranges = {'api_timeout_seconds': (5,120), 'api_retries': (0,1),
              'rss_timeout_seconds': (1,60), 'rss_retries': (0,2), 'total_timeout_seconds': (30,1200),
              'collection_timeout_seconds': (1,600), 'max_images_per_article': (0,6)}
    for key, (low, high) in ranges.items():
        if type(options[key]) is not int or not low <= options[key] <= high:
            raise ValueError('daily 配置不合法：' + key)
    return value, options

def sources(c, cfg):
    result = {}
    for row in c.execute('SELECT id,feed_url,name FROM wechat2rss_sources ORDER BY id'):
        try:
            url = normalized_url(row['feed_url'])
        except Exception:
            url = row['feed_url']
        result[url] = {'id': row['id'], 'url': url, 'name': row['name']}
    for row in cfg.get('sources', []):
        try:
            url = normalized_url(row.get('url',''))
        except Exception:
            url = str(row.get('url',''))
        result.setdefault(url, {'id': None, 'url': url, 'name': row.get('name') or '普通 RSS'})
    for url, source in result.items():
        source['key'] = hashlib.sha256(url.encode()).hexdigest()
    return list(result.values())

def save(c, source, entry, timestamp):
    key = (source['key'], entry['stable_key'])
    if c.execute('SELECT 1 FROM daily_feed_entries WHERE source_key=? AND stable_key=?', key).fetchone():
        return 0
    if source['id'] is not None:
        created = save_entry(c, source['id'], source['name'], entry)
        article_id = c.execute('SELECT article_id FROM wechat2rss_entries WHERE source_id=? AND stable_key=?',
                               (source['id'], entry['stable_key'])).fetchone()[0]
    else:
        digest = hashlib.sha256(json.dumps([entry['title'],source['name'],entry['content']],ensure_ascii=False).encode()).hexdigest()
        cur = c.execute('''INSERT OR IGNORE INTO articles(title,source_name,source_url,published_at,content,
            import_method,imported_at,content_hash) VALUES(?,?,?,?,?,'rss',?,?)''',
            (entry['title'],source['name'],entry['source_url'],entry['published_at'],entry['content'],timestamp,digest))
        created = cur.rowcount == 1
        article_id = cur.lastrowid if created else c.execute(
            'SELECT id FROM articles WHERE source_url=?' if entry['source_url'] else
            "SELECT id FROM articles WHERE source_url='' AND content_hash=?", (entry['source_url'] or digest,)).fetchone()[0]
        for image in entry['image_urls']:
            c.execute('INSERT OR IGNORE INTO wechat2rss_images(article_id,image_url) VALUES(?,?)', (article_id,image))
    c.execute('INSERT INTO daily_feed_entries VALUES(?,?,?)', (*key,article_id))
    if created:
        c.execute('INSERT OR IGNORE INTO daily_queue VALUES(?,?)', (article_id,timestamp))
    return int(created)

def run(db_path=DB_PATH, config_path=ROOT/'config.json', *, report_only=False, retry_article=None,
        transport=bounded, now=None, monotonic=time.monotonic):
    root = Path(db_path).resolve().parent
    reports = root/'reports'
    timestamp = now or clock()
    timestamp = timestamp.astimezone(BJ)
    day = timestamp.date().isoformat()
    started = monotonic()
    report_id = uuid.uuid4().hex
    log = None
    try:
        # 与图片管理共用现有跨进程锁，避免日任务和删除/清理发生竞争。
        with image_store_lock(db_path):
            logdir = root/'logs'; logdir.mkdir(exist_ok=True)
            log = (logdir/(day+'.log')).open('a',encoding='utf-8')
            initialize(db_path)
            cfg, opt = config(config_path)
            settings = settings_from_config(config_path)
            end = started + opt['total_timeout_seconds']
            def note(msg):
                log.write(clock().isoformat()+' '+msg+'\n'); log.flush()
            with closing(connect(db_path)) as c:
                feeds = sources(c,cfg)
                if not feeds:
                    raise ValueError('未配置任何来源')
                secrets = [os.environ.get('DEEPSEEK_API_KEY','')] + [s['url'] for s in feeds]
                for s in feeds:
                    try:
                        secrets += [v for _,v in parse_qsl(urlsplit(s['url']).query) if len(v)>6]
                    except ValueError:
                        pass
                previous = c.execute('SELECT * FROM daily_runs WHERE report_date=?',(day,)).fetchone()
                last = c.execute("SELECT MAX(cutoff_at) FROM daily_runs WHERE status='complete' AND report_date<?",(day,)).fetchone()[0]
                window = previous['window_start'] if previous else last or (timestamp-timedelta(hours=24)).isoformat()
                c.execute('INSERT OR IGNORE INTO daily_runs(report_date,window_start) VALUES(?,?)',(day,window)); c.commit()
                collect_end = min(end, started+opt['collection_timeout_seconds'])
                for index, source in enumerate(feeds,1):
                    record = c.execute('SELECT * FROM daily_source_runs WHERE report_date=? AND source_key=?',(day,source['key'])).fetchone()
                    if record and record['status']=='success':
                        continue
                    attempts = record['attempts'] if record else 0
                    # report-only 不联网；未知中断也计入已尝试次数。
                    while not report_only and attempts < 1+opt['rss_retries'] and monotonic()<collect_end:
                        attempts += 1
                        c.execute('''INSERT INTO daily_source_runs(report_date,source_key,name,status,attempts,checked_at)
                            VALUES(?,?,?,'running',?,?) ON CONFLICT(report_date,source_key) DO UPDATE SET
                            status='running',attempts=excluded.attempts,checked_at=excluded.checked_at''',
                            (day,source['key'],source['name'],attempts,clock().isoformat())); c.commit()
                        try:
                            normalized_url(source['url'])
                            data = transport('fetch', source['url'], min(opt['rss_timeout_seconds'],collect_end-monotonic()))
                            title, entries, skipped, truncated = parse_feed(data,source['url'])
                            source['name'] = title or source['name']
                            fresh = 0
                            with c:
                                for entry in entries:
                                    fresh += save(c,source,entry,clock().isoformat())
                                state = 'success' if entries and not skipped and not truncated else 'incomplete'
                                error = None if state=='success' else 'RSS 暂为空、条目缺标题或超过200条上限，不能确认完整采集'
                                c.execute('''UPDATE daily_source_runs SET status=?,new_count=new_count+?,error=?,name=?
                                    WHERE report_date=? AND source_key=?''',(state,fresh,error,source['name'],day,source['key']))
                            note(f'来源 {index}：{state}，新增 {fresh}')
                            if state=='success':
                                break
                        except Exception as exc:
                            c.rollback()
                            reason = str(exc) if isinstance(exc,(AnalysisError,TimeoutError,SubscriptionError)) else '网络/格式/入库失败；详情已隐藏以保护订阅密钥'
                            c.execute("UPDATE daily_source_runs SET status='failed',error=? WHERE report_date=? AND source_key=?",
                                      (reason[:300],day,source['key'])); c.commit()
                            note(f'来源 {index}：失败')
                cutoff = clock().isoformat() if now is None else timestamp.isoformat()
                # 所有本任务新发现的未报告文章都保留到报告中，跨日失败队列不会丢失。
                c.execute('''INSERT OR IGNORE INTO daily_members SELECT ?,article_id FROM daily_queue
                    WHERE discovered_at<=? AND (discovered_at>? OR article_id NOT IN
                    (SELECT article_id FROM daily_ai WHERE status='success'))''',(day,cutoff,window)); c.commit()
                if retry_article is not None:
                    if not c.execute('SELECT 1 FROM daily_queue WHERE article_id=?',(retry_article,)).fetchone():
                        raise ValueError('只能重试日任务采集的文章')
                    c.execute("UPDATE daily_ai SET status='pending' WHERE article_id=? AND status!='success'",(retry_article,))
                    c.execute('INSERT OR IGNORE INTO daily_members VALUES(?,?)',(day,retry_article)); c.commit()
                rows = c.execute('SELECT a.* FROM articles a JOIN daily_members m ON m.article_id=a.id WHERE m.report_date=? ORDER BY a.id',(day,)).fetchall()
                for row in rows:
                    article = dict(row)
                    aid = row['id']
                    old = c.execute('SELECT * FROM daily_ai WHERE article_id=?',(aid,)).fetchone()
                    if old and old['status'] in ('success','failed','running'):
                        continue
                    legacy = c.execute("SELECT result_json FROM ai_analyses WHERE article_id=? AND status='needs_review' ORDER BY id DESC LIMIT 1",(aid,)).fetchone()
                    if legacy:
                        result = legacy_result(json.loads(legacy[0]))
                        c.execute("INSERT OR REPLACE INTO daily_ai VALUES(?,'success',?,NULL,NULL,?,?)",(aid,json.dumps(result,ensure_ascii=False),settings['model'],clock().isoformat())); c.commit()
                        continue
                    used = c.execute('SELECT COUNT(*) FROM daily_attempts WHERE report_date=?',(day,)).fetchone()[0]
                    if report_only or not opt['enabled'] or not settings['paid_api_enabled'] or (opt['max_api_calls'] is not None and used>=opt['max_api_calls']) or monotonic()>=end:
                        continue
                    warning = None
                    images = []
                    cached = c.execute('SELECT 1 FROM article_images WHERE article_id=?',(aid,)).fetchone()
                    if cached:
                        images = stored_images(aid,db_path)
                    urls = [r[0] for r in c.execute('SELECT image_url FROM wechat2rss_images WHERE article_id=? ORDER BY id',(aid,))]
                    if cached and len(images)<min(len(urls),opt['max_images_per_article']):
                        warning = '此前部分海报下载失败，尚未完整核对；可手动补充图片'
                    if len(urls)>opt['max_images_per_article']:
                        warning = '部分图片超过每日配置数量上限，尚未完整核对'
                    total = 0
                    for image_index, url in enumerate([] if cached else urls[:opt['max_images_per_article']],1):
                        try:
                            data = transport('fetch',url,min(opt['rss_timeout_seconds'],end-monotonic()))
                            total += len(data)
                            if total>MAX_TOTAL_BYTES:
                                raise ValueError()
                            image = checked_download(data)
                            digest = hashlib.sha256(data).hexdigest()
                            fingerprint = hashlib.sha256(('daily:'+str(aid)).encode()).hexdigest()
                            suffix = '.png' if image['mime_type']=='image/png' else '.jpg'
                            relative = f'image_store/{fingerprint}/{image_index:02d}_{digest}{suffix}'
                            target = root/relative
                            from data_management import plain_directory, checked_image
                            plain_directory(root/'image_store',create=True)
                            plain_directory(target.parent,create=True)
                            if not target.exists():
                                atomic(target,data)
                            else:
                                checked_image(db_path,relative,digest)
                            c.execute('''INSERT OR IGNORE INTO article_images
                                (article_id,image_index,original_name,stored_path,sha256,mime_type,width,height,size_bytes)
                                VALUES(?,?,?,?,?,?,?,?,?)''',(aid,image_index,'RSS 海报'+suffix,relative,digest,
                                image['mime_type'],image['width'],image['height'],len(data))); c.commit()
                            images.append(image)
                        except Exception:
                            warning = '部分海报下载失败、格式不支持或达到时限，图片信息可能遗漏'
                    if not article['content'] and not images:
                        c.execute("INSERT OR REPLACE INTO daily_ai VALUES(?,'pending',NULL,?,NULL,?,?)",(aid,'无可供分析的正文或图片',settings['model'],clock().isoformat())); c.commit()
                        continue
                    for attempt in range(1+opt['api_retries']):
                        used = c.execute('SELECT COUNT(*) FROM daily_attempts WHERE report_date=?',(day,)).fetchone()[0]
                        if (opt['max_api_calls'] is not None and used>=opt['max_api_calls']) or monotonic()>=end:
                            break
                        # 先落盘占用调用额度，中断后不自动再发付费请求。
                        cur = c.execute("INSERT INTO daily_attempts(report_date,article_id,started_at,status) VALUES(?,?,?,'running')",(day,aid,clock().isoformat()))
                        attempt_id = cur.lastrowid
                        c.execute("INSERT OR REPLACE INTO daily_ai VALUES(?,'running',NULL,NULL,?,?,?)",(aid,warning,settings['model'],clock().isoformat())); c.commit()
                        try:
                            payload = make_payload(article,images)
                            envelope = transport('api',payload,min(opt['api_timeout_seconds'],end-monotonic()))
                            result = validate(envelope,article)
                            c.execute("UPDATE daily_ai SET status='success',result_json=?,error=NULL,analyzed_at=? WHERE article_id=?",(json.dumps(result,ensure_ascii=False),clock().isoformat(),aid))
                            c.execute("UPDATE daily_attempts SET status='success' WHERE id=?",(attempt_id,)); c.commit()
                            break
                        except Exception as exc:
                            # 仅明确429/503拒绝响应可选重试；超时/中断/未知结果绝不自动重试。
                            retryable = isinstance(exc,AnalysisError) and ('请求受限' in str(exc) or '服务繁忙' in str(exc))
                            c.execute("UPDATE daily_ai SET status='failed',error=? WHERE article_id=?",('AI 请求失败或结果不可靠；可能已计费，请手动核对后重试',aid))
                            c.execute("UPDATE daily_attempts SET status='failed' WHERE id=?",(attempt_id,)); c.commit()
                            if not retryable:
                                break
                states = {r['source_key']:dict(r) for r in c.execute('SELECT * FROM daily_source_runs WHERE report_date=?',(day,))}
                failures = []
                success = 0
                for index,s in enumerate(feeds,1):
                    state = states.get(s['key'])
                    if state and state['status']=='success':
                        success += 1
                    else:
                        failures.append(f"来源 {index}："+(state['error'] or '未完成') if state else f'来源 {index}：尚未采集（时间上限或只生成报告）')
                articles = []
                for row in rows:
                    article = dict(row)
                    result = c.execute('SELECT * FROM daily_ai WHERE article_id=?',(row['id'],)).fetchone()
                    article['result'] = json.loads(result['result_json']) if result and result['status']=='success' else None
                    article['warning'] = (result['warning'] or result['error']) if result else '待分析（未授权、额度或时间上限）'
                    articles.append(article)
                analyzed = sum(bool(a['result']) for a in articles)
                pending = len(articles)-analyzed
                status = {'report_date':day,'report_id':report_id,'generated_at':clock().isoformat(),
                          'window_start':window,'cutoff_at':cutoff,'status':'partial' if failures or pending or any(a['warning'] for a in articles) else 'complete',
                          'sources_total':len(feeds),'sources_success':success,'sources_failed':len(feeds)-success,
                          'articles_new':c.execute('SELECT COUNT(*) FROM daily_queue q JOIN daily_members m ON m.article_id=q.article_id WHERE m.report_date=? AND q.discovered_at>?',(day,window)).fetchone()[0],
                          'articles_analyzed':analyzed,'articles_pending':pending}
                from daily_editor import choose_plan
                base_markdown, _ = render(status,articles,failures,secrets)
                editorial_attempt_id=None
                def editorial_request(payload):
                    nonlocal editorial_attempt_id
                    used=c.execute('SELECT COUNT(*) FROM daily_attempts WHERE report_date=?',(day,)).fetchone()[0]
                    if monotonic()>=end or (opt['max_api_calls'] is not None and used>=opt['max_api_calls']):
                        raise TimeoutError('没有剩余编排预算')
                    cur=c.execute("INSERT INTO daily_attempts(report_date,article_id,started_at,status) VALUES(?,NULL,?,'running')",(day,clock().isoformat()))
                    attempt_id=cur.lastrowid;editorial_attempt_id=attempt_id;c.commit()
                    try:
                        response=transport('api',payload,min(opt['api_timeout_seconds'],end-monotonic()))
                        c.execute("UPDATE daily_attempts SET status='received' WHERE id=?",(attempt_id,));c.commit()
                        return response
                    except Exception:
                        c.execute("UPDATE daily_attempts SET status='failed' WHERE id=?",(attempt_id,));c.commit()
                        raise
                layout=None;mode='local'
                if opt['editorial_enabled'] and articles:
                    used=c.execute('SELECT COUNT(*) FROM daily_attempts WHERE report_date=?',(day,)).fetchone()[0]
                    allowed=not report_only and opt['enabled'] and settings['paid_api_enabled'] and monotonic()<end and (opt['max_api_calls'] is None or used<opt['max_api_calls'])
                    layout,mode=choose_plan(base_markdown.splitlines(),reports/'.editorial_cache',editorial_request if allowed else None)
                    if editorial_attempt_id is not None:
                        c.execute('UPDATE daily_attempts SET status=? WHERE id=?',('success' if mode=='ai' else 'failed',editorial_attempt_id));c.commit()
                    if mode=='fallback':
                        status['status']='partial'
                        failures.append('AI编排未完成或校验未通过，已保留全部内容并使用本地备用格式。')
                status['editorial_status']=mode
                markdown, document = render(status,articles,failures,secrets,layout=layout)
                publish(reports,status,markdown,document)
                c.execute('UPDATE daily_runs SET cutoff_at=?,status=?,report_id=?,generated_at=? WHERE report_date=?',
                          (cutoff,status['status'],report_id,status['generated_at'],day)); c.commit()
                note('日报已发布：'+status['status'])
                print('日报已生成：'+str(reports/(day+'.md'))+'；状态 '+status['status'])
                return 0 if status['status']=='complete' else 2
    except Exception:
        # 不把原始异常（可能含订阅地址或认证信息）输出到日志。
        if log:
            log.write('任务失败：数据库、配置或报告写入不可用；未宣称报告完成\n'); log.flush()
        try:
            reports.mkdir(exist_ok=True)
            # 并发拒绝不能覆盖正在运行任务的状态。
            if log is not None:
                atomic(reports/'status.json', json.dumps({'report_date':day,'report_id':report_id,
                    'generated_at':clock().isoformat(),'status':'failed','markdown_path':None,'html_path':None,
                    'sources_total':None,'sources_success':None,'sources_failed':None,
                    'articles_new':None,'articles_analyzed':None,'articles_pending':None,
                    'error':'数据无法可靠读取或正式报告无法写入'},ensure_ascii=False).encode('utf-8'))
        except OSError:
            pass
        print('任务未完成：请检查 logs、数据库、配置和 reports 写入权限；未声明成功。')
        return 1
    finally:
        if log:
            log.close()
