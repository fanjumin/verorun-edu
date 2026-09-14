#!/usr/bin/env python3
"""OpenAlex 中文作品适配器（模块 zhsrc，v1.8.0）。

复用父类字段映射 / 摘要重建 / DOI 归一化，仅叠加语言过滤。
覆盖范围说明：OpenAlex 收录的是有 DOI 的中文作品（实测 505 万条，
含中华医学会系列等中文期刊）；无 DOI 的 CNKI/万方作品不在本模块范围。

与父类的刻意差异：无 DOI 的中文作品也保留（中文库无 DOI 比例高），
去重由 workflow._dedup_by_doi 走 title+year 兜底。
"""

from typing import Any, Dict, List

import requests

from .openalex import OpenAlexAdapter, _SELECT


class OpenAlexZHAdapter(OpenAlexAdapter):
    """OpenAlex 中文作品源（`language:zh` 过滤）。"""

    def search(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        params = {
            'search': query,
            'filter': 'language:zh',
            'per-page': min(int(limit), 100),
            'select': _SELECT,
        }
        resp = requests.get(self.BASE_URL, params=params, timeout=self.TIMEOUT)
        resp.raise_for_status()
        results = []
        for w in resp.json().get('results', []):
            doi = w.get('doi')
            item = self._work_to_item(w, self._normalize_doi(doi) if doi else '')
            item['source_db'] = 'openalex_zh'
            results.append(item)
        return results

    # get_by_doi 继承父类不做限制：DOI 反查与语言无关，保持行为一致。
