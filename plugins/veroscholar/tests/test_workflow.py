#!/usr/bin/env python3
"""test_workflow.py — 工作流层单元测试（mock requests，不连外网）。

运行:
    cd F:\\Sites\\VeroRun
    python -m unittest plugins.veroscholar.tests.test_workflow -v
"""

import sys
import os
import unittest
import contextlib
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from plugins.veroscholar import workflow as wf


class FakeResponse:
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self):
        return self._json


class TestResolveDOIOnline(unittest.TestCase):
    """resolve_doi_online — 5 态 + 路由 + 异常覆盖。"""

    def test_registered_crossref(self):
        with mock.patch('plugins.veroscholar.workflow.requests.get') as mget:
            mget.return_value = FakeResponse(200, {
                'message': {'updated-by': []}
            })
            result = wf.resolve_doi_online('10.1038/nature14539')
        self.assertEqual(result['status'], 'registered')
        self.assertIsNone(result['notice_doi'])

    def test_retracted(self):
        with mock.patch('plugins.veroscholar.workflow.requests.get') as mget:
            mget.return_value = FakeResponse(200, {
                'message': {
                    'updated-by': [{'type': 'retraction', 'DOI': '10.1016/retract.2020'}]
                }
            })
            result = wf.resolve_doi_online('10.1016/s0140-6736(20)31290-3')
        self.assertEqual(result['status'], 'retracted')
        self.assertEqual(result['notice_doi'], '10.1016/retract.2020')

    def test_concern(self):
        with mock.patch('plugins.veroscholar.workflow.requests.get') as mget:
            mget.return_value = FakeResponse(200, {
                'message': {
                    'updated-by': [{'type': 'expression_of_concern', 'DOI': '10.1016/concern.2020'}]
                }
            })
            result = wf.resolve_doi_online('10.1016/something')
        self.assertEqual(result['status'], 'concern')
        self.assertEqual(result['notice_doi'], '10.1016/concern.2020')

    def test_unregistered_404(self):
        with mock.patch('plugins.veroscholar.workflow.requests.get') as mget:
            mget.return_value = FakeResponse(404)
            result = wf.resolve_doi_online('10.9999/fake.doi.2026')
        self.assertEqual(result['status'], 'unregistered')
        self.assertIsNone(result['notice_doi'])

    def test_offline_exception(self):
        with mock.patch('plugins.veroscholar.workflow.requests.get') as mget:
            mget.side_effect = Exception('connection refused')
            result = wf.resolve_doi_online('10.1038/nature14539')
        self.assertEqual(result['status'], 'offline')

    def test_offline_non_200(self):
        with mock.patch('plugins.veroscholar.workflow.requests.get') as mget:
            mget.return_value = FakeResponse(500)
            result = wf.resolve_doi_online('10.1038/nature14539')
        self.assertEqual(result['status'], 'offline')

    def test_arxiv_routes_to_datacite(self):
        """10.48550/ 前缀应路由到 DataCite。"""
        with mock.patch('plugins.veroscholar.workflow.requests.get') as mget:
            mget.return_value = FakeResponse(200, {
                'data': {'attributes': {'updated-by': []}}
            })
            result = wf.resolve_doi_online('10.48550/arXiv.1706.03762')
        self.assertEqual(result['status'], 'registered')
        # 确认请求 URL 是 DataCite 而非 Crossref
        call_url = mget.call_args[0][0]
        self.assertIn('api.datacite.org', call_url)

    def test_offline_bad_json(self):
        with mock.patch('plugins.veroscholar.workflow.requests.get') as mget:
            mget.return_value = FakeResponse(200, {})  # 无 message
            result = wf.resolve_doi_online('10.1038/nature14539')
        self.assertEqual(result['status'], 'registered')  # 空的 msg 视为 registered


class TestDedupNode(unittest.TestCase):
    """handle_veroscholar_dedup — 确定性 + 去重 + 空输入。"""

    def test_dedup_by_doi(self):
        papers = [
            {'doi': '10.1/a', 'citation_count': 50, 'year': 2020, 'title': 'A'},
            {'doi': '10.2/b', 'citation_count': 100, 'year': 2022, 'title': 'B'},
            {'doi': '10.1/a', 'citation_count': 50, 'year': 2020, 'title': 'A dup'},
        ]
        node_def = {'config': {'papers_field': 'papers', 'output_field': 'papers'}}
        input_data = {'context': {'papers': papers}}
        result = wf.handle_veroscholar_dedup(node_def, input_data)
        self.assertTrue(result['success'])
        self.assertEqual(result['count'], 2)
        # 按被引降序排列
        self.assertEqual(result['papers'][0]['doi'], '10.2/b')
        self.assertEqual(result['papers'][1]['doi'], '10.1/a')

    def test_deterministic(self):
        papers = [
            {'doi': '10.1/a', 'citation_count': 30, 'year': 2021, 'title': 'A'},
            {'doi': '10.2/b', 'citation_count': 30, 'year': 2023, 'title': 'B'},
        ]
        node_def = {'config': {}}
        input_data = {'context': {'papers': papers}}
        r1 = wf.handle_veroscholar_dedup(node_def, input_data)
        r2 = wf.handle_veroscholar_dedup(node_def, input_data)
        self.assertEqual(r1['papers'], r2['papers'])

    def test_empty_input(self):
        node_def = {'config': {}}
        input_data = {'context': {}}
        result = wf.handle_veroscholar_dedup(node_def, input_data)
        self.assertTrue(result['success'])
        self.assertEqual(result['count'], 0)
        self.assertEqual(result['papers'], [])

    def test_dedup_by_title_year(self):
        papers = [
            {'title': 'Paper X', 'year': 2020, 'citation_count': 10},
            {'title': 'Paper X', 'year': 2020, 'citation_count': 20},  # 重复
            {'title': 'Paper Y', 'year': 2021, 'citation_count': 5},
        ]
        node_def = {'config': {}}
        input_data = {'context': {'papers': papers}}
        result = wf.handle_veroscholar_dedup(node_def, input_data)
        self.assertEqual(result['count'], 2)


class TestApplySearchFilters(unittest.TestCase):
    """apply_search_filters — 年份/被引/venue 过滤。"""

    def setUp(self):
        self.papers = [
            {'doi': '10.1/a', 'year': 2020, 'citation_count': 100, 'venue': 'NeurIPS', 'title': 'A'},
            {'doi': '10.2/b', 'year': 2022, 'citation_count': 50, 'venue': 'ICML', 'title': 'B'},
            {'doi': '10.3/c', 'year': 2024, 'citation_count': 200, 'venue': 'Nature', 'title': 'C'},
        ]

    def test_year_range(self):
        filtered = wf.apply_search_filters(self.papers, {'year_from': 2021, 'year_to': 2023})
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]['doi'], '10.2/b')

    def test_min_citations(self):
        filtered = wf.apply_search_filters(self.papers, {'min_citations': 150})
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]['doi'], '10.3/c')

    def test_venue_case_insensitive(self):
        filtered = wf.apply_search_filters(self.papers, {'venue': 'neurips'})
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]['doi'], '10.1/a')

    def test_no_filters_returns_all(self):
        filtered = wf.apply_search_filters(self.papers, {})
        self.assertEqual(len(filtered), 3)

    def test_year_none_excluded(self):
        papers = [{'doi': '10.1/a', 'year': None, 'citation_count': 10, 'title': 'A'},
                  {'doi': '10.2/b', 'year': 2023, 'citation_count': 10, 'title': 'B'}]
        filtered = wf.apply_search_filters(papers, {'year_from': 2020})
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]['doi'], '10.2/b')


class TestDedupUpstreamContract(unittest.TestCase):
    """去重节点取数契约：引擎只把上游输出存 node_<id>_output，
    不按 output_field 提升到 context 顶层（线上 0 篇 bug 的回归防护）。"""

    def test_reads_from_upstream_node_output(self):
        input_data = {
            'context': {'topic': 't'},
            'node_n1_output': {'success': True, 'papers': [
                {'doi': '10.1/a', 'title': 'A', 'year': 2020, 'citation_count': 5}]},
        }
        out = wf.handle_veroscholar_dedup({'config': {}}, input_data)
        self.assertTrue(out['success'])
        self.assertEqual(out['count'], 1)

    def test_direct_context_field_takes_priority(self):
        input_data = {
            'context': {'papers': [{'doi': '10.9/z', 'title': 'Z',
                                     'year': 2024, 'citation_count': 1}]},
            'node_n1_output': {'papers': [{'doi': '10.1/a'}]},
        }
        out = wf.handle_veroscholar_dedup({'config': {}}, input_data)
        self.assertEqual(out['papers'][0]['doi'], '10.9/z')

    def test_empty_when_no_upstream(self):
        out = wf.handle_veroscholar_dedup({'config': {}}, {'context': {}})
        self.assertTrue(out['success'])
        self.assertEqual(out['count'], 0)


class TestReviewGenNode(unittest.TestCase):
    """综述生成节点：收集上游论文 → 注入提示词 → LLM（mock）。"""

    def test_generates_with_upstream_papers(self):
        input_data = {
            'context': {'topic': 'attention mechanism'},
            'node_n2_output': {'success': True, 'papers': [
                {'title': 'Paper A', 'year': 2021, 'venue': 'NeurIPS',
                 'citation_count': 5}]},
        }
        fake_conn = mock.MagicMock()

        @contextlib.contextmanager
        def fake_db():
            yield fake_conn

        with mock.patch.object(wf, '_llm_chat',
                               return_value='# 综述\n正文') as mllm, \
             mock.patch.object(wf.m, 'get_db', side_effect=lambda: fake_db()), \
             mock.patch.object(wf.m, 'create_review', return_value='rev-uuid'):
            out = wf.handle_veroscholar_review_gen({'config': {}}, input_data)
        self.assertTrue(out['success'])
        self.assertIn('综述', out['content'])
        self.assertEqual(out['count'], 1)
        prompt = mllm.call_args[0][0][1]['content']
        self.assertIn('Paper A', prompt)
        self.assertIn('attention mechanism', prompt)

    def test_review_written_back(self):
        """W1：生成成功后回写 reviews 表（mock models 层，断言调用参数）。"""
        input_data = {
            'context': {'topic': 'attention', 'user_id': 7},
            'node_n2_output': {'success': True, 'papers': [
                {'title': 'Paper A', 'year': 2021, 'venue': 'NeurIPS',
                 'citation_count': 5, 'doi': '10.1/a'}]},
        }
        fake_conn = mock.MagicMock()
        fake_conn.execute.return_value.fetchone.return_value = {'id': 'uuid-1'}

        @contextlib.contextmanager
        def fake_db():
            yield fake_conn

        with mock.patch.object(wf, '_llm_chat', return_value='# 综述'), \
             mock.patch.object(wf.m, 'get_db', side_effect=lambda: fake_db()), \
             mock.patch.object(wf.m, 'create_review',
                               return_value='rev-uuid') as mrev:
            out = wf.handle_veroscholar_review_gen({'config': {}}, input_data)
        self.assertEqual(out['review_id'], 'rev-uuid')
        self.assertEqual(mrev.call_args[1]['created_by'], 7)
        self.assertEqual(mrev.call_args[1]['paper_ids'], ['uuid-1'])

    def test_fails_without_papers(self):
        out = wf.handle_veroscholar_review_gen({'config': {}}, {'context': {}})
        self.assertFalse(out['success'])
        self.assertIn('no papers', out['error'])


class TestMultiSourceSearchPerSource(unittest.TestCase):
    """W3：per_source 分源配额——后位源（中文源）不被前位源挤占去重窗口。"""

    def test_per_source_passthrough(self):
        fake_adapter = mock.MagicMock()
        fake_adapter.search.return_value = [
            {'doi': '10.%d/a' % i, 'title': 'T%d' % i, 'year': 2020}
            for i in range(2)]
        fake_conn = mock.MagicMock()

        @contextlib.contextmanager
        def fake_db():
            yield fake_conn

        with mock.patch.object(wf, '_resolve_sources',
                               return_value=['openalex_zh']), \
             mock.patch.object(wf, 'get_adapter', return_value=fake_adapter), \
             mock.patch.object(wf.m, 'get_db', side_effect=lambda: fake_db()):
            results, errors = wf.run_multi_source_search(
                'transformer', sources=['openalex_zh'],
                limit=30, per_source=8)
        # 每源每关键词请求 per_source 条，而非全局 limit
        self.assertEqual(fake_adapter.search.call_args[1]['limit'], 8)
        self.assertEqual(len(results), 2)
        self.assertEqual(errors, {})

    def test_per_source_defaults_to_limit(self):
        fake_adapter = mock.MagicMock()
        fake_adapter.search.return_value = []
        fake_conn = mock.MagicMock()

        @contextlib.contextmanager
        def fake_db():
            yield fake_conn

        with mock.patch.object(wf, '_resolve_sources', return_value=['arxiv']), \
             mock.patch.object(wf, 'get_adapter', return_value=fake_adapter), \
             mock.patch.object(wf.m, 'get_db', side_effect=lambda: fake_db()):
            wf.run_multi_source_search('transformer', sources=['arxiv'], limit=30)
        # 缺省 per_source 时保持旧行为：每源取 limit
        self.assertEqual(fake_adapter.search.call_args[1]['limit'], 30)


class TestSearchNodePerSource(unittest.TestCase):
    """W3 透传回归：handle_veroscholar_search 必须把 config.per_source
    传给 run_multi_source_search（曾遗漏致线上中文源配额不生效）。"""

    def test_per_source_passed_through(self):
        node = {'config': {'sources': ['openalex'], 'limit': 30, 'per_source': 8}}
        with mock.patch.object(wf, 'run_multi_source_search',
                               return_value=([{'doi': '10.1/a'}], {})) as mrun:
            out = wf.handle_veroscholar_search(node, {'context': {'topic': 'x'}})
        kwargs = mrun.call_args[1]
        self.assertEqual(kwargs.get('per_source'), 8)

    def test_per_source_absent_keeps_old_behavior(self):
        node = {'config': {'sources': ['openalex'], 'limit': 30}}
        with mock.patch.object(wf, 'run_multi_source_search',
                               return_value=([{'doi': '10.1/a'}], {})) as mrun:
            wf.handle_veroscholar_search(node, {'context': {'topic': 'x'}})
        self.assertIsNone(mrun.call_args[1].get('per_source'))


if __name__ == '__main__':

    unittest.main()