"""v0.6 免费模拟测试：临时数据库、人工 RSS/模型结果，禁止真实连接。"""
import contextlib
import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import daily_pipeline as daily
import daily_reports as reports
from daily_analysis import FIELDS, CATEGORIES, validate
from image_import import image_store_lock
from test_v04 import png

T = datetime(2026,10,29,9,50,tzinfo=daily.BJ)
FEED = '''<rss><channel><title>【测试】校园</title><item><title>【测试】讲座</title><guid>fixed-id</guid>
<link>https://mp.weixin.qq.com/s/test</link><description>【测试】讲座，2026年10月30日，教室A。</description></item></channel></rss>'''.encode()

def envelope():
    event = {k:None for k in FIELDS}
    event.update(name='【测试】讲座',category=CATEGORIES[0],time='2026年10月30日',location='教室A',
                 evidence={k:None for k in FIELDS})
    event['evidence'].update(name='【测试】讲座',time='2026年10月30日',location='教室A')
    return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'ocr_text':None,'events':[event]},ensure_ascii=False)}}]}

class DailyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ruc-test-only-daily-',dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root/'articles.db'
        self.cfg = self.root/'config.json'
        self.value = {'sources':[{'name':'【测试】源','url':'https://example.com/rss?token=PRIVATE_TEST_SECRET'}],
                      'deepseek':{'paid_api_enabled':True},'daily':{'rss_retries':0}}
        self.write_config()
        self.block = patch('socket.create_connection',side_effect=AssertionError('禁止真实网络'))
        self.block.start(); self.addCleanup(self.block.stop)
        self.calls = []

    def write_config(self):
        self.cfg.write_text(json.dumps(self.value,ensure_ascii=False),encoding='utf-8')

    def transport(self,kind,value,timeout):
        self.calls.append(kind)
        return FEED if kind=='fetch' else envelope()

    def run_daily(self,**kwargs):
        now = kwargs.pop('now',T)
        with patch.object(daily,'clock',return_value=now), contextlib.redirect_stdout(__import__('io').StringIO()):
            return daily.run(self.db,self.cfg,now=now,transport=kwargs.pop('transport',self.transport),**kwargs)

    def status(self):
        return json.loads((self.root/'reports/status.json').read_text(encoding='utf-8'))

    def test_complete_and_repeat_skips_all_paid_and_source_requests(self):
        self.assertEqual(self.run_daily(),0)
        first = list(self.calls)
        self.assertEqual(self.run_daily(),0)
        self.assertEqual(self.calls,first)
        self.assertEqual(self.status()['articles_new'],1)
        self.assertEqual(self.status()['articles_analyzed'],1)
        self.assertEqual(self.calls.count('api'),1)

    def test_all_sources_failed_still_publishes_fault_brief(self):
        def fail(*args): raise TimeoutError()
        self.assertEqual(self.run_daily(transport=fail),2)
        s=self.status(); self.assertEqual(s['status'],'partial'); self.assertEqual(s['sources_failed'],1)
        content=Path(s['markdown_path']).read_text(encoding='utf-8')
        self.assertIn('无法确认是否有新增信息',content)
        self.assertNotIn('本轮暂无新增文章',content)

    def test_multiple_sources_partial_and_global_url_dedup(self):
        self.value['sources'] += [{'name':'另一个','url':'https://example.org/rss'}]
        self.write_config()
        self.assertEqual(self.run_daily(),0)
        self.assertEqual(self.status()['sources_success'],2)
        self.assertEqual(self.status()['articles_new'],1)

    def test_one_source_fails_others_continue(self):
        self.value['sources'] += [{'name':'失败源','url':'https://bad.example/rss'}]; self.write_config()
        def transport(kind,value,timeout):
            if kind=='fetch' and 'bad.example' in value: raise TimeoutError()
            return self.transport(kind,value,timeout)
        self.assertEqual(self.run_daily(transport=transport),2)
        self.assertEqual(self.status()['sources_success'],1)
        self.assertEqual(self.status()['articles_analyzed'],1)

    def test_api_failure_preserves_pending_and_does_not_auto_retry(self):
        def transport(kind,value,timeout):
            self.calls.append(kind)
            if kind=='api': raise TimeoutError()
            return FEED
        self.assertEqual(self.run_daily(transport=transport),2)
        self.assertEqual(self.status()['articles_pending'],1)
        self.run_daily(transport=transport)
        self.assertEqual(self.calls.count('api'),1)
        self.assertIn('待分析',Path(self.status()['markdown_path']).read_text(encoding='utf-8'))

    def test_next_day_no_new_articles_is_complete_and_preserves_prior_report(self):
        self.run_daily()
        before=(self.root/'reports/2026-10-29.md').read_bytes()
        self.assertEqual(self.run_daily(now=T+timedelta(days=1)),0)
        self.assertEqual(self.status()['articles_new'],0)
        self.assertIn('本轮暂无新增文章',Path(self.status()['markdown_path']).read_text(encoding='utf-8'))
        self.assertEqual((self.root/'reports/2026-10-29.md').read_bytes(),before)

    def test_report_only_makes_partial_without_network(self):
        self.assertEqual(self.run_daily(report_only=True),2)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.status()['sources_failed'],1)

    def test_daily_call_limit_keeps_title_link_in_report(self):
        self.value['daily']['max_api_calls']=0; self.write_config()
        self.assertEqual(self.run_daily(),2)
        self.assertEqual(self.calls,['fetch'])
        body=Path(self.status()['markdown_path']).read_text(encoding='utf-8')
        self.assertIn('讲座',body); self.assertIn('https://mp.weixin.qq.com/s/test',body)

    def test_default_unlimited_analyzes_more_than_ten_articles(self):
        import xml.etree.ElementTree as ET
        root=ET.fromstring(FEED); item=next(root.iter('item'))
        entries=[]
        for n in range(12):
            node=ET.fromstring(ET.tostring(item))
            node.find('guid').text='test-guid-'+str(n)
            node.find('link').text='https://mp.weixin.qq.com/s/test-'+str(n)
            entries.append(ET.tostring(node))
        feed='<rss><channel><title>测试</title>'.encode()+b''.join(entries)+b'</channel></rss>'
        def transport(kind,value,timeout):
            self.calls.append(kind)
            return feed if kind=='fetch' else envelope()
        self.assertEqual(self.run_daily(transport=transport),0)
        self.assertEqual(self.calls.count('api'),12)
        self.assertEqual(self.status()['articles_analyzed'],12)

    def test_explicit_null_limit_and_invalid_limits(self):
        self.value['daily']['max_api_calls']=None; self.write_config()
        self.assertIsNone(daily.config(self.cfg)[1]['max_api_calls'])
        for invalid in (-1,True,'unlimited'):
            self.value['daily']['max_api_calls']=invalid; self.write_config()
            with self.assertRaises(ValueError): daily.config(self.cfg)

    def test_general_news_summary_is_requested_and_validated(self):
        from daily_analysis import make_payload
        article={'title':'校园新闻','content':'学校图书馆完成设备更新。','source_name':'测试'}
        payload=make_payload(article,[])
        self.assertIn('非活动文章也要概括',payload['messages'][1]['content'][0]['text'])
        obj={'ocr_text':None,'events':[],'article_summary':'图书馆设备已更新。',
             'article_summary_evidence':article['content'],'article_category':'其他资讯'}
        value=envelope(); value['choices'][0]['message']['content']=json.dumps(obj)
        self.assertEqual(validate(value,article)['article_summary'],obj['article_summary'])

    def test_unproven_summary_and_unknown_category_are_not_guessed(self):
        obj={'ocr_text':None,'events':[],'article_summary':'编造内容','article_summary_evidence':'不存在证据','article_category':'新奇类别'}
        value=envelope();value['choices'][0]['message']['content']=json.dumps(obj)
        result=validate(value,{'title':'测试','content':'原始文字'})
        self.assertIsNone(result['article_summary']);self.assertIsNone(result['article_category'])

    def test_featured_other_and_unclassified_articles_all_appear(self):
        self.run_daily();s=self.status()
        event=json.loads(envelope()['choices'][0]['message']['content'])['events'][0]
        articles=[{'id':1,'title':'重点文章','source_url':'https://example.com/one','result':{'events':[event,dict(event,category=None,name='未归类事项',summary=None)]}},
                  {'id':2,'title':'一般新闻','source_url':'https://example.com/two','result':{'events':[],'article_category':'其他资讯','article_summary':'图书馆设备更新。'}},
                  {'id':3,'title':'分类未知文章','source_url':'https://example.com/three','content':'这是保留下来的原文。下一句。','result':{'events':[]}}]
        md,page=reports.render(s,articles,[])
        for value in ['未归类事项','一般新闻','图书馆设备更新','分类未知文章','这是保留下来的原文','无法归类']:
            self.assertIn(value,md);self.assertIn(value,page)
        for url in ['https://example.com/one','https://example.com/two','https://example.com/three']:self.assertIn(url,md)

    def test_old_results_use_body_or_ocr_without_new_api_call(self):
        self.assertEqual(reports.brief({'content':'第一句。第二句。','result':{'events':[]}}),'正文摘录：第一句。')
        self.assertEqual(reports.brief({'content':'','result':{'events':[],'ocr_text':'海报首句。其余。'}}),'图片文字摘录：海报首句。')

    def test_pending_title_only_article_is_not_dropped(self):
        self.run_daily();article={'id':10,'title':'正文尚未读到','source_url':'https://example.com/pending'}
        md,_=reports.render(self.status(),[article],[])
        self.assertIn('正文尚未读到',md);self.assertIn('暂无法概括',md);self.assertIn('待分析',md)

    def test_legacy_visual_summary_is_preserved_without_events(self):
        from daily_analysis import legacy_result
        result=legacy_result({'summary':'一般资讯的旧摘要。'})
        self.assertEqual(result['article_summary'],'一般资讯的旧摘要。')
        self.assertEqual(result['events'],[])

    def test_empty_report_sections_and_unknown_fields_are_omitted(self):
        self.run_daily(); status=self.status()
        md,page=reports.render(status,[],[])
        self.assertNotIn('## ',md)
        self.assertNotIn('<h2>',page)
        event={k:None for k in FIELDS}; event.update(name='测试活动',category=CATEGORIES[0],evidence={})
        article={'id':1,'title':'测试文章','source_url':'https://example.com/article','result':{'events':[event]}}
        md,page=reports.render(status,[article],[])
        self.assertIn('学术讲座与论坛',md)
        for absent in ['## 竞赛','近期截止事项','其他文章与待分析文章','采集与分析异常','地点：','未确认']:
            self.assertNotIn(absent,md); self.assertNotIn(absent,page)

    def test_fault_report_keeps_warning_without_empty_categories(self):
        def fail(*args): raise TimeoutError()
        self.run_daily(transport=fail)
        md=Path(self.status()['markdown_path']).read_text(encoding='utf-8')
        self.assertIn('数据不完整',md); self.assertIn('无法确认是否有新增信息',md)
        self.assertIn('## 采集与分析异常',md)
        self.assertNotIn('## 学术讲座',md); self.assertNotIn('## 近期截止',md)

    def test_status_hashes_report_identifiers_utf8_and_no_secret(self):
        self.run_daily(); s=self.status()
        for field,hash_field in [('markdown_path','markdown_sha256'),('html_path','html_sha256')]:
            data=Path(s[field]).read_bytes(); text=data.decode('utf-8')
            self.assertEqual(hashlib.sha256(data).hexdigest(),s[hash_field])
            self.assertIn(s['report_id'],text); self.assertNotIn('PRIVATE_TEST_SECRET',text)
        self.assertIn('<a href=',Path(s['html_path']).read_text(encoding='utf-8'))
        self.assertFalse((self.root/'reports/latest.md').exists())

    def test_lock_prevents_overlapping_run(self):
        with image_store_lock(self.db):
            self.assertEqual(self.run_daily(),1)
        self.assertEqual(self.calls,[])

    def test_write_failure_is_failed_not_complete(self):
        with patch.object(reports,'atomic',side_effect=PermissionError('test')):
            self.assertEqual(self.run_daily(),1)
        self.assertEqual(self.status()['status'],'failed')

    def test_overwrite_failure_restores_previous_pair(self):
        self.run_daily(); md=self.root/'reports/2026-10-29.md'; old=md.read_bytes()
        original=reports.atomic
        counter=[0]
        def fail_once(path,data):
            counter[0]+=1
            if counter[0]==2: raise PermissionError('test')
            return original(path,data)
        with patch.object(reports,'atomic',side_effect=fail_once):
            self.assertEqual(self.run_daily(),1)
        self.assertEqual(md.read_bytes(),old)

    def test_beijing_date_missed_schedule_uses_actual_execution_date(self):
        utc=datetime(2026,10,30,1,51,tzinfo=timezone.utc)
        self.run_daily(now=utc)
        self.assertEqual(self.status()['report_date'],'2026-10-30')
        script=(Path(__file__).parent/'register_daily_task.ps1').read_text(encoding='utf-8-sig')
        self.assertIn('StartWhenAvailable',script); self.assertIn('IgnoreNew',script)
        self.assertIn('AddHours(10)',script)

    def test_time_limit_still_writes_partial(self):
        self.assertEqual(self.run_daily(monotonic=lambda:10000 if self.calls else 0,
            transport=lambda kind,value,timeout: self.calls.append(kind) or FEED),2)
        self.assertEqual(self.status()['status'],'partial')

    def test_unverified_date_is_not_promoted_to_fact(self):
        value=envelope(); obj=json.loads(value['choices'][0]['message']['content'])
        obj['events'][0]['time']='明天'; obj['events'][0]['evidence']['time']='明天'
        value['choices'][0]['message']['content']=json.dumps(obj)
        result=validate(value,{'title':'测试','content':'明天'})
        self.assertIsNone(result['events'][0]['time'])

    def test_invalid_single_source_does_not_cancel_report(self):
        self.value['sources'].append({'name':'坏网址','url':'file:///test'})
        self.write_config()
        self.assertEqual(self.run_daily(),2)
        self.assertEqual(self.status()['sources_success'],1)

    def test_poster_is_checked_copied_and_sent_with_text(self):
        feed=FEED.replace(b'</description>',b'&lt;img src="https://example.com/image.png"/&gt;</description>')
        payloads=[]
        def transport(kind,value,timeout):
            if kind=='api':
                payloads.append(value)
                return envelope()
            return png() if 'image.png' in value else feed
        self.assertEqual(self.run_daily(transport=transport),0)
        self.assertEqual(len(list((self.root/'image_store').rglob('*.png'))),1)
        self.assertTrue(any(b.get('type')=='image_url' for b in payloads[0]['messages'][1]['content']))

    def test_network_worker_hard_timeout_can_be_terminated(self):
        # 仅使用明确禁止的本机目标，绝不访问真实服务；极短预算验证子进程中止。
        with self.assertRaises(TimeoutError):
            daily.bounded('fetch','http://127.0.0.1/',0.001)

if __name__=='__main__': unittest.main()
