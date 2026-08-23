#!/usr/bin/env python3
"""学术数据源适配器工厂。

提供统一的注册表与获取入口，routes 层据此按名称取适配器实例。
"""

from .base import BaseScholarAdapter
from .arxiv import ArxivAdapter
from .semantic_scholar import SemanticScholarAdapter
from .openalex import OpenAlexAdapter

__all__ = [
    'BaseScholarAdapter',
    'ArxivAdapter',
    'SemanticScholarAdapter',
    'OpenAlexAdapter',
    'ADAPTER_REGISTRY',
    'get_adapter',
]

#: 别名 → 适配器类。'semantic' 为蓝图使用的短名，'semantic_scholar' 为完整名。
ADAPTER_REGISTRY = {
    'arxiv': ArxivAdapter,
    'semantic': SemanticScholarAdapter,
    'semantic_scholar': SemanticScholarAdapter,
    'openalex': OpenAlexAdapter,
}


def get_adapter(name: str):
    """按名称获取适配器实例；未知名称返回 None。"""
    cls = ADAPTER_REGISTRY.get(name)
    return cls() if cls else None
