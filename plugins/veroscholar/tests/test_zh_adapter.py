#!/usr/bin/env python3
"""test_zh_adapter.py — 中文文献源模块（模块 zhsrc，v1.8.0）。

mock requests，不连外网；沿用 tests/test_adapters.py 的 FakeResponse 模板。
"""
import sys, os, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from plugins.veroscholar.adapters import get_adapter
from plugins.veroscholar.adapters.openalex_zh import OpenAlexZHAdapter


class FakeResponse:
    def __init__(self, json_data=None, status_code=200):
        self._json = json_data or {}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError('HTTP %s' % self.status_code)

    def json(self):
        return self._json


_ZH_WORK = {
    'id': 'https://openalex.org/W1', 'doi': 'https://doi.org/10.3760/cma.j.issn.0254-6450.2020.02.003',
    'title': '某中文流行病学研究', 'abstract_inverted_index': {'摘要': [0]},
    'authorships': [{'author': {'display_name': '张三'}}],
    'primary_location': {'source': {'display_name': '中华流行病学杂志'}},
    'publication_year': 2020, 'cited_by_count': 12, 'open_access': {'oa_url': ''},
}
_NO_DOI_WORK = dict(_ZH_WORK, doi=None, id='https://openalex.org/W2', title='无DOI中文论文')


class TestOpenAlexZH(unittest.TestCase):

    def test_registry(self):
        self.assertIsInstance(get_adapter('openalex_zh'), OpenAlexZHAdapter)

    def test_search_sends_language_filter(self):
        with mock.patch('plugins.veroscholar.adapters.openalex_zh.requests.get') as mget:
            mget.return_value = FakeResponse({'results': [_ZH_WORK]})
            items = OpenAlexZHAdapter().search('深度学习', limit=10)
        params = mget.call_args.kwargs.get('params') or mget.call_args[1]['params']
        self.assertEqual(params['filter'], 'language:zh')
        self.assertEqual(params['search'], '深度学习')
        self.assertEqual(len(items), 1)

    def test_source_db_marked(self):
        with mock.patch('plugins.veroscholar.adapters.openalex_zh.requests.get') as mget:
            mget.return_value = FakeResponse({'results': [_ZH_WORK]})
            items = OpenAlexZHAdapter().search('x')
        self.assertEqual(items[0]['source_db'], 'openalex_zh')
        self.assertEqual(items[0]['doi'], '10.3760/cma.j.issn.0254-6450.2020.02.003')

    def test_keeps_no_doi_items(self):
        """与英文父类刻意差异：无 DOI 中文作品保留（title+year 去重兜底）。"""
        with mock.patch('plugins.veroscholar.adapters.openalex_zh.requests.get') as mget:
            mget.return_value = FakeResponse({'results': [_NO_DOI_WORK]})
            items = OpenAlexZHAdapter().search('x')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['title'], '无DOI中文论文')

    def test_http_error_propagates(self):
        with mock.patch('plugins.veroscholar.adapters.openalex_zh.requests.get') as mget:
            mget.return_value = FakeResponse({}, status_code=500)
            with self.assertRaises(RuntimeError):
                OpenAlexZHAdapter().search('x')


if __name__ == '__main__':
    unittest.main()
