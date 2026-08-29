#!/usr/bin/env python3
"""arXiv 适配器 — 通过 export.arxiv.org API 检索（Atom 格式）。

使用 requests + feedparser 解析，不依赖已弃用的 arxiv 包。
feedparser 采用惰性导入：仅在实际检索时加载，避免缺失时拖垮插件导入。
"""

import requests
from typing import Any, Dict, List

from .base import BaseScholarAdapter


class ArxivAdapter(BaseScholarAdapter):
    BASE_URL = "https://export.arxiv.org/api/query"

    @staticmethod
    def _get_feedparser():
        """惰性加载 feedparser；缺失时给出明确错误。"""
        try:
            import feedparser
            return feedparser
        except ImportError as e:
            raise RuntimeError(
                'feedparser is required for the arXiv adapter'
                ' (python_dependencies: feedparser)') from e

    @staticmethod
    def _parse_arxiv_id(entry_id: str) -> str:
        """从 Atom id 提取 arxiv id。id 形如 http://arxiv.org/abs/2401.00001v2。"""
        return entry_id.split('/abs/')[-1] if '/abs/' in entry_id else entry_id.split('/')[-1]

    @staticmethod
    def _entry_to_item(entry) -> Dict[str, Any]:
        arxiv_id = ArxivAdapter._parse_arxiv_id(entry.id)
        year = None
        if entry.get('published'):
            try:
                year = int(entry.published[:4])
            except (TypeError, ValueError):
                year = None
        return {
            'doi': f"arXiv:{arxiv_id}",
            'title': entry.title.replace('\n', ' ').strip(),
            'authors': [{'name': a.get('name', '')} for a in entry.get('authors', [])],
            'abstract': entry.summary.replace('\n', ' ').strip(),
            'venue': 'arXiv',
            'year': year,
            'citation_count': 0,  # arXiv 不提供引用数
            'pdf_url': f"https://arxiv.org/pdf/{arxiv_id}.pdf",
            'source_db': 'arxiv',
            'external_id': arxiv_id,
        }

    def search(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        params = {
            'search_query': f'all:{query}',
            'start': 0,
            'max_results': min(int(limit), 200),
            'sortBy': 'submittedDate',
            'sortOrder': 'descending',
        }
        resp = requests.get(self.BASE_URL, params=params, timeout=self.TIMEOUT)
        resp.raise_for_status()
        feed = self._get_feedparser().parse(resp.text)
        return [self._entry_to_item(e) for e in feed.entries]

    def get_by_doi(self, doi: str) -> Dict[str, Any]:
        if not doi.startswith('arXiv:'):
            raise ValueError(f'ArxivAdapter: unsupported DOI: {doi}')
        arxiv_id = doi.split(':', 1)[1]
        params = {'search_query': f'id:{arxiv_id}', 'max_results': 1}
        resp = requests.get(self.BASE_URL, params=params, timeout=self.TIMEOUT)
        resp.raise_for_status()
        feed = self._get_feedparser().parse(resp.text)
        if not feed.entries:
            raise ValueError(f'ArxivAdapter: paper not found: {doi}')
        return self._entry_to_item(feed.entries[0])
