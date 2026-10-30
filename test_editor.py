"""免费编排测试，使用人工JSON方案，不调用真实API。"""
import json
import tempfile
import unittest
from pathlib import Path
import daily_editor as editor
import test_daily as fixtures

LINES=['# 日报','报告标识：test','','## 原栏目','','### 测试活动','时间：2026年10月30日。',
       '[原文](https://example.com/a)','','## 其他资讯','','- 普通文章：[原文](https://example.com/b)',
       '','采集覆盖提示：测试']

def response(plan):
    return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(plan,ensure_ascii=False)}}]}

class EditorTests(unittest.TestCase):
    def plan(self,blocks):
        return {'sections':[{'title':'今天先看这些','style':'compact','blocks':[b['id'] for b in reversed(blocks)]}]}

    def test_free_layout_keeps_all_facts_and_links(self):
        _,blocks,_=editor.blocks_from_lines(LINES)
        lines=editor.apply_plan(LINES,self.plan(blocks))
        output='\n'.join(lines)
        self.assertIn('## 今天先看这些',output);self.assertNotIn('## 原栏目',output)
        for value in ('2026年10月30日','https://example.com/a','https://example.com/b','报告标识：test'):
            self.assertIn(value,output)

    def test_missing_duplicate_and_unknown_references_rejected(self):
        _,blocks,_=editor.blocks_from_lines(LINES)
        for ids in (['b1'],['b1','b1'],['b1','b999']):
            with self.assertRaises(ValueError):
                editor.validate_plan({'sections':[{'title':'标题','style':'cards','blocks':ids}]},blocks)

    def test_cache_avoids_repeat_paid_request(self):
        with tempfile.TemporaryDirectory(prefix='ruc-test-only-editor-',dir=Path(__file__).parent) as folder:
            calls=[]
            def request(payload):
                calls.append(payload)
                return response(self.plan(json.loads(payload['messages'][1]['content'])))
            plan,mode=editor.choose_plan(LINES,folder,request)
            self.assertEqual(mode,'ai')
            self.assertEqual(editor.choose_plan(LINES,folder,request)[1],'cached')
            self.assertEqual(len(calls),1)

    def fixture(self):
        case=fixtures.DailyTests('test_complete_and_repeat_skips_all_paid_and_source_requests')
        case.setUp();self.addCleanup(case.doCleanups)
        case.value['daily']['editorial_enabled']=True;case.write_config()
        return case

    def test_integration_editor_failure_still_publishes_complete_content(self):
        case=self.fixture()
        self.assertEqual(case.run_daily(),2) # 模拟文章JSON不符合编排方案，回退
        status=case.status()
        self.assertEqual(status['editorial_status'],'fallback')
        self.assertIn('本地备用格式',Path(status['markdown_path']).read_text(encoding='utf-8'))
        calls=list(case.calls)
        case.run_daily()
        self.assertEqual(case.calls,calls) # 编排失败或未知也不盲目重复计费

    def test_integration_editor_and_cache_with_report_only(self):
        case=self.fixture();api_calls=[]
        def transport(kind,value,timeout):
            if kind=='api' and '只能引用已有资料编号' in value['messages'][0]['content']:
                api_calls.append('editor')
                return response(self.plan(json.loads(value['messages'][1]['content'])))
            return case.transport(kind,value,timeout)
        self.assertEqual(case.run_daily(transport=transport),0)
        self.assertEqual(case.status()['editorial_status'],'ai')
        self.assertEqual(case.run_daily(report_only=True,transport=transport),0)
        self.assertEqual(case.status()['editorial_status'],'cached')
        self.assertEqual(api_calls,['editor'])

if __name__=='__main__':unittest.main()
