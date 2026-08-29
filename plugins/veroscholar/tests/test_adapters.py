#!/usr/bin/env python3
"""test_adapters.py — 学术数据源适配器单元测试（mock 网络请求）。

运行:
    cd F:\\Sites\\VeroRun
    python -m unittest plugins.veroscholar.tests.test_adapters -v
"""

import sys
import os
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from plugins.veroscholar.adapters import get_adapter
from plugins.veroscholar.adapters.arxiv import ArxivAdapter
from plugins.veroscholar.adapters.semantic_scholar import SemanticScholarAdapter
from plugins.veroscholar.adapters.openalex import OpenAlexAdapter, _reconstruct_abstract


class FakeResponse:
    """模拟 requests.Response。"""

    def __init__(self, text='', json_data=None, status_code=200):
        self.text = text
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')

    def json(self):
        return self._json or {}


class FakeFeedparser:
    """注入 sys.modules 的假 feedparser（本地未安装）。"""

    def __init__(self, entries):
        self._entries = entries

    def parse(self, text):
        return type('Feed', (), {'entries': self._entries})()


def _inject_feedparser(entries):
    fp = FakeFeedparser(entries)
    sys.modules['feedparser'] = fp
    return fp


def _fake_entry(title, arxiv_id, published='2024-05-01T10:00:00Z', abstract='A test abstract.'):
    """模拟 feedparser.FeedParserDict：支持属性访问 + .get()。

    真实 feedparser 返回的对象为 FeedParserDict（dict 子类，属性访问等价
    key 访问），ArxivAdapter._entry_to_item 按该约定读取 entry.id / .title
    等属性。SimpleNamespace 提供属性访问，.get 代理回 dict。
    """
    data = {
        'id': f'http://arxiv.org/abs/{arxiv_id}',
        'title': title,
        'published': published,
        'authors': [{'name': 'Alice'}],
        'summary': abstract,
    }
    ns = SimpleNamespace(**data)
    ns.get = data.get
    return ns


class TestReconstructAbstract(unittest.TestCase):
    def test_inverted_index_reconstruction(self):
        idx = {'this': [0], 'is': [1], 'a': [2], 'test': [3]}
        self.assertEqual(_reconstruct_abstract(idx), 'this is a test')

    def test_empty_inverted_index(self):
        self.assertEqual(_reconstruct_abstract({}), '')
        self.assertEqual(_reconstruct_abstract(None), '')


class TestArxivAdapter(unittest.TestCase):
    def setUp(self):
        self.adapter = ArxivAdapter()
        self._old_fp = sys.modules.get('feedparser')

    def tearDown(self):
        if self._old_fp is None:
            sys.modules.pop('feedparser', None)
        else:
            sys.modules['feedparser'] = self._old_fp

    @mock.patch('plugins.veroscholar.adapters.arxiv.requests.get')
    def test_search_parses_entries(self, mock_get):
        mock_get.return_value = FakeResponse(text='<feed/>')
        _inject_feedparser([_fake_entry('Attention Is All You Need', '1706.03762')])

        items = self.adapter.search('attention')
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item['doi'], 'arXiv:1706.03762')
        self.assertEqual(item['source_db'], 'arxiv')
        self.assertEqual(item['year'], 2024)
        self.assertEqual(item['authors'][0]['name'], 'Alice')
        self.assertEqual(item['pdf_url'], 'https://arxiv.org/pdf/1706.03762.pdf')

    @mock.patch('plugins.veroscholar.adapters.arxiv.requests.get')
    def test_get_by_doi(self, mock_get):
        mock_get.return_value = FakeResponse(text='<feed/>')
        _inject_feedparser([_fake_entry('Some Paper', '2401.00001')])

        item = self.adapter.get_by_doi('arXiv:2401.00001')
        self.assertEqual(item['external_id'], '2401.00001')

    def test_get_by_doi_unsupported(self):
        with self.assertRaises(ValueError):
            self.adapter.get_by_doi('10.1234/foo')

    @mock.patch('plugins.veroscholar.adapters.arxiv.requests.get')
    def test_search_http_error_propagates(self, mock_get):
        mock_get.return_value = FakeResponse(status_code=500)
        with self.assertRaises(RuntimeError):
            self.adapter.search('attention')


class TestSemanticScholarAdapter(unittest.TestCase):
    def setUp(self):
        self.adapter = SemanticScholarAdapter()

    @mock.patch('plugins.veroscholar.adapters.semantic_scholar.requests.get')
    def test_search_maps_fields(self, mock_get):
        mock_get.return_value = FakeResponse(json_data={
            'data': [{
                'title': 'S2 Paper',
                'abstract': 'abstract',
                'authors': [{'name': 'Bob'}],
                'venue': 'ICLR',
                'year': 2023,
                'citationCount': 10,
                'externalIds': {'DOI': '10.1/abc'},
                'paperId': 'P1',
                'openAccessPdf': {'url': 'https://x/pdf'},
            }],
        })
        items = self.adapter.search('transformer')
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item['doi'], '10.1/abc')
        self.assertEqual(item['source_db'], 'semantic_scholar')
        self.assertEqual(item['citation_count'], 10)
        self.assertEqual(item['pdf_url'], 'https://x/pdf')

    @mock.patch('plugins.veroscholar.adapters.semantic_scholar.requests.get')
    def test_search_skips_missing_doi(self, mock_get):
        mock_get.return_value = FakeResponse(json_data={
            'data': [{'title': 'No DOI', 'externalIds': {}}],
        })
        items = self.adapter.search('x')
        self.assertEqual(items, [])


class TestOpenAlexAdapter(unittest.TestCase):
    def setUp(self):
        self.adapter = OpenAlexAdapter()

    @mock.patch('plugins.veroscholar.adapters.openalex.requests.get')
    def test_search_reconstructs_abstract(self, mock_get):
        mock_get.return_value = FakeResponse(json_data={
            'results': [{
                'id': 'W1',
                'doi': 'https://doi.org/10.2/xyz',
                'title': 'OA Paper',
                'abstract_inverted_index': {'hello': [0], 'world': [1]},
                'authorships': [{'author': {'display_name': 'Carol'}}],
                'primary_location': {'source': {'display_name': 'Nature'}},
                'publication_year': 2022,
                'cited_by_count': 42,
                'open_access': {'oa_url': 'https://oa/paper'},
            }],
        })
        items = self.adapter.search('deep learning')
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item['doi'], '10.2/xyz')
        self.assertEqual(item['abstract'], 'hello world')
        self.assertEqual(item['venue'], 'Nature')
        self.assertEqual(item['source_db'], 'openalex')
        self.assertEqual(item['citation_count'], 42)

    @mock.patch('plugins.veroscholar.adapters.openalex.requests.get')
    def test_get_by_doi_not_found(self, mock_get):
        mock_get.return_value = FakeResponse(json_data={'results': []})
        with self.assertRaises(ValueError):
            self.adapter.get_by_doi('10.999/none')


class TestAdapterFactory(unittest.TestCase):
    def test_get_adapter_aliases(self):
        self.assertIsInstance(get_adapter('arxiv'), ArxivAdapter)
        self.assertIsInstance(get_adapter('semantic'), SemanticScholarAdapter)
        self.assertIsInstance(get_adapter('semantic_scholar'), SemanticScholarAdapter)
        self.assertIsInstance(get_adapter('openalex'), OpenAlexAdapter)

    def test_get_adapter_unknown(self):
        self.assertIsNone(get_adapter('not_a_source'))


if __name__ == '__main__':
    unittest.main()
