#!/usr/bin/env python3
"""Semantic Scholar 适配器 — graph API v1。

免费 API，注意速率限制（无 key 约 1 rps/共享池）；routes 层需控制并发。
"""

import requests
from typing import Any, Dict, List

from .base import BaseScholarAdapter

_FIELDS = 'title,abstract,authors,venue,year,citationCount,externalIds,openAccessPdf'


class SemanticScholarAdapter(BaseScholarAdapter):
    BASE_URL = "https://api.semanticscholar.org/graph/v1"

    @staticmethod
    def _paper_to_item(p: Dict[str, Any], doi: str) -> Dict[str, Any]:
        return {
            'doi': doi,
            'title': p.get('title', ''),
            'authors': [{'name': a.get('name', '')} for a in p.get('authors', [])],
            'abstract': p.get('abstract', '') or '',
            'venue': p.get('venue', '') or '',
            'year': p.get('year'),
            'citation_count': p.get('citationCount', 0),
            'pdf_url': (p.get('openAccessPdf') or {}).get('url', '') if isinstance(p.get('openAccessPdf'), dict) else '',
            'source_db': 'semantic_scholar',
            'external_id': p.get('paperId', ''),
        }

    def search(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        url = f"{self.BASE_URL}/paper/search"
        params = {'query': query, 'limit': min(int(limit), 100), 'fields': _FIELDS}
        resp = requests.get(url, params=params, timeout=self.TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        results = []
        for p in data.get('data', []):
            doi = (p.get('externalIds') or {}).get('DOI')
            if not doi:
                continue
            results.append(self._paper_to_item(p, doi))
        return results

    def get_by_doi(self, doi: str) -> Dict[str, Any]:
        url = f"{self.BASE_URL}/paper/DOI:{doi}"
        params = {'fields': _FIELDS}
        resp = requests.get(url, params=params, timeout=self.TIMEOUT)
        resp.raise_for_status()
        p = resp.json()
        if not p:
            raise ValueError(f'SemanticScholarAdapter: paper not found: {doi}')
        return self._paper_to_item(p, doi)
