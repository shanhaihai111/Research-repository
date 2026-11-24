import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import chinese_content as c

DETAIL = ('研究背景：本研究关注抑郁症治疗中的持续疗效问题。'
          '研究设计：研究纳入成年抑郁症患者并比较治疗方案。'
          '主要方法：研究使用随机分配和症状量表进行评估。'
          '主要结果：摘要报告干预组的症状改善，但未报告精确效应量。'
          '结论与局限：结果支持继续研究，样本规模限制了外推。'
          '科研启发：后续试验可预先定义长期疗效和停药结局。')

class Tests(unittest.TestCase):
    def test_no_secret_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(c.ContentError): c.config_from_env()

    def test_mock_generation_and_cache(self):
        article={'doi':'10.1000/example','title':'Trial of depression treatment',
                 'journal':'Example Journal','abstract':'Adults with depression were randomized. Symptoms improved.',
                 'type':'Randomized Controlled Trial'}
        output={'title_cn':'抑郁症治疗方案的随机对照试验',
                'summary_cn':'研究将成年抑郁症患者随机分组，比较治疗方案。摘要报告症状改善，但没有提供精确效应量。',
                'detail_cn':DETAIL}
        takeaway={'weekly_takeaway_cn':'本期研究关注抑郁症治疗的症状改善，后续试验需要明确长期疗效、停药及患者结局。'}
        config={'key':'mock','base':'https://example.invalid/v1','model':'mock',
                'timeout':1,'budget':2,'spent':0,'input_per_million':1,'output_per_million':1}
        with tempfile.TemporaryDirectory() as tmp, patch.object(c,'config_from_env',return_value=config), patch.object(c,'_request',side_effect=[output,takeaway]) as call:
            rows=[dict(article)]; c.enrich(rows,Path(tmp))
            self.assertEqual(call.call_count,2)
            self.assertTrue(c._valid_article(rows[0]))
            rows2=[dict(article)]; call.side_effect=[takeaway]
            c.enrich(rows2,Path(tmp))
            self.assertEqual(call.call_count,3)

if __name__ == '__main__': unittest.main()
