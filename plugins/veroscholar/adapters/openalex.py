#!/usr/bin/env python3
"""OpenAlex 适配器 — works API。

OpenAlex 的摘要以倒排索引（abstract_inverted_index）返回，
需要重建为可读文本。
"""

import requests
from typing import Any, Dict, List

from .base import BaseScholarAdapter

_SELECT = ('id,doi,title,abstract_inverted_index,authorships,'
           'primary_location,publication_year,cited_by_count,open_access')


def _reconstruct_abstract(inverted_index: Dict[str, List[int]]) -> str:
    """把倒排索引重建为摘要文本；空索引返回空串。"""
    if not inverted_index:
        return ''
    words = {}
    for word, positions in inverted_index.items():
        for pos in positions:
            words[pos] = word
    if not words:
        return ''
    return ' '.join(words[i] for i in sorted(words))


class OpenAlexAdapter(BaseScholarAdapter):
    BASE_URL = "https://api.openalex.org/works"

    @staticmethod
    def _work_to_item(w: Dict[str, Any], doi: str) -> Dict[str, Any]:
        primary = w.get('primary_location') or {}
        source = primary.get('source') or {}
        venue = source.get('display_name') or ''
        return {
            'doi': doi,
            'title': w.get('title', '') or '',
            'authors': [{'name': (a.get('author') or {}).get('display_name', '')}
                        for a in (w.get('authorships') or [])],
            'abstract': _reconstruct_abstract(w.get('abstract_inverted_index') or {}),
            'venue': venue,
            'year': w.get('publication_year'),
            'citation_count': w.get('cited_by_count', 0),
            'pdf_url': ((w.get('open_access') or {}).get('oa_url') or '')
                       if isinstance(w.get('open_access'), dict) else '',
            'source_db': 'openalex',
            'external_id': str(w.get('id', '')),
        }

    @staticmethod
    def _normalize_doi(doi: str) -> str:
        """OpenAlex doi 字段带 https://doi.org/ 前缀，去掉以统一。"""
        return doi.replace('https://doi.org/', '').replace('http://doi.org/', '')

    def search(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        params = {
            'search': query,
            'per-page': min(int(limit), 100),
            'select': _SELECT,
        }
        resp = requests.get(self.BASE_URL, params=params, timeout=self.TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        results = []
        for w in data.get('results', []):
            doi = w.get('doi')
            if not doi:
                continue
            results.append(self._work_to_item(w, self._normalize_doi(doi)))
        return results

    def get_by_doi(self, doi: str) -> Dict[str, Any]:
        params = {'filter': f'doi:{doi}', 'per-page': 1, 'select': _SELECT}
        resp = requests.get(self.BASE_URL, params=params, timeout=self.TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        results = data.get('results') or []
        if not results:
            raise ValueError(f'OpenAlexAdapter: paper not found: {doi}')
        return self._work_to_item(results[0], doi)
