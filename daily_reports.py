"""UTF-8 日报，状态最后发布；所有输出均转义并移除已知密钥。"""
import hashlib
import html
import json
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from daily_analysis import CATEGORIES
from safe_http import validate_url, FetchError

def safe_link(url, secrets=()):
    try:
        validate_url(url)
        if any(s and s in url for s in secrets):
            return ''
        p = urlsplit(url)
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if not re.search('token|secret|key|auth|password', k, re.I)]
        return urlunsplit((p.scheme, p.netloc, p.path, urlencode(query), ''))
    except (FetchError, ValueError, TypeError):
        return ''

def clean(value, secrets):
    text = str(value if value is not None else '未确认')
    for secret in secrets:
        if secret:
            text = text.replace(secret, '[已隐藏]')
    # 避免模型或 RSS 把完整订阅地址/任意令牌网址写入正文。
    text = re.sub(r'https?://[^\s<>"）]+', '[网址请见原文链接]', text)
    return ''.join(c for c in text if c in '\n\t' or ord(c) >= 32)

def brief(article):
    """优先使用已有摘要；旧记录不重新付费，退回有明确标记的原文摘录。"""
    result = article.get('result') or {}
    summary = result.get('article_summary')
    if not summary:
        summary = next((e.get('summary') for e in result.get('events',[]) if e.get('summary')),None)
    if summary:
        prefix = '一句话概括：'
        source = summary
    else:
        source = article.get('content') or result.get('ocr_text') or ''
        prefix = '正文摘录：' if article.get('content') else '图片文字摘录：'
    source = ' '.join(source.split())
    if not source:
        return '仅有标题，正文或图片文字不足，暂无法概括，请核对原文。'
    sentence = re.split(r'(?<=[。！？!?])\s*|(?<=\.)\s+',source,maxsplit=1)[0]
    return prefix + sentence[:160] + ('…' if len(sentence)>160 else '')

def render(status, articles, failures, secrets=(), layout=None):
    lines = []
    def text(value):
        return html.escape(clean(value, secrets)).replace('[', '\\[').replace(']', '\\]').replace('*', '\\*').replace('`', '\\`').replace('\n', ' ')
    def link(article):
        url = safe_link(article.get('source_url', ''), secrets)
        return f'[原文]({url.replace(chr(40), "%28").replace(chr(41), "%29")})' if url else '原文链接未提供或已隐藏'
    lines += [f"# 校园日报 · {status['report_date']}", '', f"报告标识：{status['report_id']}",
              f"实际生成时间：{status['generated_at']}", '']
    if status['status'] == 'partial':
        lines += ['**数据不完整：部分采集、分析失败或达到处理时限，以下信息仅反映已完成的部分。允许按 partial 日报读取和发送。**', '']
    scope_note = '仅当天新采集入库，不等于当天发布；历史积压不自动补分析' if status.get('selection')=='today_discovered' else '按本地入库/发现时间；首次为最近24小时，不等于发布完整覆盖'
    lines += [f"统计范围：{status['window_start']} 至 {status['cutoff_at']}（{scope_note}）。",
              f"信息源：{status['sources_total']}；成功：{status['sources_success']}；失败或未完成：{status['sources_failed']}。",
              f"新增文章：{status['articles_new']}；已分析：{status['articles_analyzed']}；待分析或失败：{status['articles_pending']}。", '']
    if status['sources_total'] and not status['sources_success']:
        lines += ['全部 RSS 采集失败或未完成，无法确认是否有新增信息。', '']
    elif status['articles_new'] == 0:
        lines += ['本轮暂无新增文章。' if not status['sources_failed'] else '已成功读取的来源未发现新增文章；失败来源是否有更新尚无法确认。', '']
    deadlines = []
    featured = set()
    for category in CATEGORIES:
        found = False
        for article in articles:
            for event in (article.get('result') or {}).get('events', []):
                if event.get('category') != category:
                    continue
                if not found:
                    lines += ['## ' + category, '']
                    found = True
                featured.add(article['id'])
                lines += [f"### {text(event.get('name') or article['title'])}", '']
                for fields in [(('time','时间'),('location','地点'),('deadline','报名截止')),
                               (('organizer','主办'),('audience','对象'),('registration','报名'))]:
                    values = [label+'：'+text(event[field]) for field,label in fields if event.get(field)]
                    if values:
                        lines += ['；'.join(values)+'。']
                if event.get('summary'):
                    lines += ['简介：'+text(event['summary'])]
                lines += [link(article) + ' · AI 自动提取，关键日期与图片文字请核对原文。', '']
                for field, quote in event.get('evidence', {}).items():
                    if quote and event.get(field):
                        lines += [f"- {text(field)} 原文证据：{text(quote)}"]
                lines += ['']
                deadline = event.get('deadline') or ''
                match = re.search(r'(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})', deadline)
                if match:
                    from datetime import date, timedelta
                    try:
                        due = date(*map(int, match.groups()))
                        today = date.fromisoformat(status['report_date'])
                        if today <= due <= today + timedelta(days=7):
                            deadlines.append((due.isoformat(), event.get('name') or article['title'], article))
                    except ValueError:
                        pass
    if deadlines:
        lines += ['## 近期截止事项（未来7天）', '']
        lines += [f'- {due}：{text(name)} · {link(article)}' for due, name, article in sorted(deadlines, key=lambda x: x[0])]
    other_lines, unclassified_lines = [], []
    for article in articles:
        result = article.get('result') or {}
        unknown = [e for e in result.get('events',[]) if e.get('category') not in CATEGORIES]
        if article['id'] not in featured or unknown:
            label = '已分析' if article.get('result') else '待分析，尚不能判断信息价值'
            if unknown:
                for event in unknown:
                    information = event.get('summary') or event.get('name') or brief(article)
                    unclassified_lines.append(f"- {text(article['title'])} · 未归类条目：{text(information)} · {link(article)} · {label}")
                continue
            line = f"- {text(article['title'])} · {text(brief(article))} · {link(article)} · {label}"
            if result.get('article_category') in CATEGORIES + ['其他资讯']:
                other_lines.append(line)
            else:
                unclassified_lines.append(line)
    if other_lines:
        lines += ['', '## 其他资讯（一句话概括）', ''] + other_lines
    if unclassified_lines:
        lines += ['', '## 无法归类与待分析信息（一句话概括）', ''] + unclassified_lines
    error_lines = ['- ' + text(failure) for failure in failures]
    for article in articles:
        if not article.get('result') or article.get('warning'):
            error_lines += [f"- {text(article['title'])} · {link(article)} · {text(article.get('warning') or '待分析；不能据此判断文章没有价值')}"]
    if error_lines:
        lines += ['', '## 采集与分析异常', ''] + error_lines
    lines += ['', '采集覆盖提示：RSS 可能仅保留有限篇文章，每日一次可能漏掉已被源移除的内容；报告不承诺公众号历史全量覆盖。', '']
    if layout is not None:
        from daily_editor import apply_plan
        lines = apply_plan(lines, layout)
    markdown = '\n'.join(lines)
    # HTML 只渲染本程序产生的 Markdown 子集，源数据始终作为文本转义。
    body = []
    for line in lines:
        value = html.escape(html.unescape(line))
        value = re.sub(r'\[原文\]\((https?://[^\s]+)\)', r'<a href="\1" target="_blank" rel="noopener noreferrer">查看原文 ↗</a>', value)
        if line.startswith('# '):
            body.append('<h1>' + value[2:] + '</h1>')
        elif line.startswith('## '):
            body.append('<h2>' + value[3:] + '</h2>')
        elif line.startswith('### '):
            body.append('<h3>' + value[4:] + '</h3>')
        elif line:
            body.append('<p>' + value + '</p>')
    document = '<!doctype html><html lang="zh-CN"><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>校园日报 ' + status['report_date'] + '</title><style>body{font:16px/1.85 system-ui,"Microsoft YaHei",sans-serif;background:#f3f5f8;color:#243247;margin:0}main{max-width:900px;margin:32px auto;background:white;padding:40px;border-radius:16px}h1{color:#8b192b}h2{border-bottom:2px solid #eee;padding-top:24px}h3{color:#345b86}a{color:#9d2138}p{overflow-wrap:anywhere}@media(max-width:600px){main{margin:0;padding:20px}}</style><main>' + '\n'.join(body) + '</main></html>'
    return markdown, document

def atomic(path, data):
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)

def publish(folder, status, markdown, document):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    md = folder / (status['report_date'] + '.md')
    page = folder / (status['report_date'] + '.html')
    manifest = folder / 'status.json'
    dated = folder / ('status-' + status['report_date'] + '.json')
    status.update(markdown_path=str(md.resolve()), html_path=str(page.resolve()),
                  markdown_sha256=hashlib.sha256(markdown.encode('utf-8')).hexdigest(),
                  html_sha256=hashlib.sha256(document.encode('utf-8')).hexdigest(),
                  dated_status_path=str(dated.resolve()))
    old = {p: p.read_bytes() if p.exists() else None for p in (md, page, dated, manifest)}
    try:
        atomic(md, markdown.encode('utf-8'))
        atomic(page, document.encode('utf-8'))
        data = json.dumps(status, ensure_ascii=False, indent=2).encode('utf-8')
        atomic(dated, data)
        atomic(manifest, data)
    except OSError:
        for path, data in old.items():
            try:
                atomic(path, data) if data is not None else path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
