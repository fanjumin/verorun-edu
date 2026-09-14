#!/usr/bin/env python3
"""模块 B：qa_context filter 回调——向论文问答注入全文摘录。

签名约定（平台 hooks.apply_filters 按签名嗅探，见 plugin_manager/hooks.py）：
首参名必须是 `value`，否则 kwargs（paper_id/question）不会透传，
回调拿不到论文上下文，注入功能静默失效。
"""
import logging

logger = logging.getLogger('veroscholar.fulltext')


def inject_fulltext_context(value, paper_id=None, question=None, **kwargs):
    """Filter 回调：注入 top-3 相关全文块。任何失败静默返回原上下文。"""
    context_parts = value
    try:
        if not paper_id or not question:
            return context_parts
        from .. import models as m
        from .chunker import top_relevant_chunks
        with m.get_db() as conn:
            chunks = m.list_fulltext_chunks(conn, paper_id, limit=200)
        if not chunks:
            return context_parts
        picked = top_relevant_chunks(chunks, question, k=3)
        excerpt = '\n---\n'.join(
            '[p.%s] %s' % (c['page'], c['content'][:600]) for c in picked)
        context_parts = context_parts + ['【论文全文摘录】\n' + excerpt]
    except Exception as e:
        logger.warning('fulltext context injection skipped: %s', e)
    return context_parts
