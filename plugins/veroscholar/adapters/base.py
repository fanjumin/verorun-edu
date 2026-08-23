#!/usr/bin/env python3
"""学术数据源适配器抽象基类。

统一各学术数据源的返回结构，routes 层据此做去重、入库与展示。
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List


class BaseScholarAdapter(ABC):
    """学术数据源适配器抽象基类。

    所有数据源必须实现 search() 与 get_by_doi()，
    返回统一结构：
        {
            'doi': str,
            'title': str,
            'authors': [{'name': str, 'orcid': str}],
            'abstract': str,
            'venue': str,
            'year': int,
            'citation_count': int,
            'pdf_url': str,
            'source_db': str,      # 'arxiv' | 'semantic_scholar' | 'openalex'
            'external_id': str,
        }
    """

    #: 单请求超时（秒）。§11.3 网络隔离要求统一超时，默认 15s。
    TIMEOUT = 15

    @abstractmethod
    def search(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """按关键词检索文献。

        Args:
            query: 检索词
            limit: 返回条数上限

        Returns:
            统一结构的文献列表；失败时抛出异常由上层降级处理。

        Raises:
            requests.HTTPError / ValueError: 数据源不可用
        """
        raise NotImplementedError

    @abstractmethod
    def get_by_doi(self, doi: str) -> Dict[str, Any]:
        """按 DOI 获取单篇文献。

        Args:
            doi: DOI 标识

        Returns:
            统一结构的单篇文献。

        Raises:
            ValueError: DOI 无法解析或不存在
        """
        raise NotImplementedError
