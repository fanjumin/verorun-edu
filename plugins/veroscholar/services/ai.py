#!/usr/bin/env python3
"""VeroScholar AI 服务层：论文问答（RAG）、摘要翻译、相关论文推荐。

复用平台内核：
  - agent_matrix.engine.UnifiedLLM             → chat() 生成
  - plugins._base.embeddings.EmbeddingService  → 向量化

约定：
  - 所有入口失败抛 RuntimeError（携带用户可读信息），由 routes 层转为 4xx/503 JSON；
  - embedding 能力缺失时 chat/translate 仍可用（上下文直取摘要 + 笔记）；
    related 经 services/vector_backend.py 三档降级（T2 pgvector → T1 本地向量 →
    T0 pg_trgm 关键词），桌面无 pgvector/embedding 时仍可用（科研版桌面约定）；
  - LLM / embedding 模块延迟导入，避免插件加载时产生系统级依赖。
"""

import logging

logger = logging.getLogger('veroscholar.ai')

_MODULE = 'veroscholar'

_llm = None
_emb = None


def _get_llm():
    global _llm
    if _llm is None:
        from agent_matrix.engine import UnifiedLLM
        _llm = UnifiedLLM()
    return _llm


def _get_emb():
    global _emb
    if _emb is None:
        from plugins._base.embeddings import EmbeddingService
        _emb = EmbeddingService({'module': _MODULE})
    return _emb


def _default_llm_target() -> tuple:
    """解析平台默认模型（与 shop_ai / cleaner_ai 同一约定）。

    来源：public.system_config 的 `ai_text_provider` / `ai_text_model`
    （veroscholar 连接 search_path 含 public，可直接读）。
    任一环节失败回退 ('deepseek', 'deepseek-chat')——网关默认注册项。

    背景：UnifiedLLM.chat 不传 provider/model 时 _resolve_model 必抛
    'Cannot resolve model'（线上 503 根因），必须显式指定。
    """
    try:
        from .. import models as m
        with m.get_db() as conn:
            rows = conn.execute(
                "SELECT key, value FROM system_config "
                "WHERE key IN ('ai_text_provider', 'ai_text_model')").fetchall()
        cfg = {r['key']: (r['value'] or '').strip() for r in rows}
        provider = cfg.get('ai_text_provider') or 'deepseek'
        model = cfg.get('ai_text_model') or 'deepseek-chat'
        return provider, model
    except Exception:
        return 'deepseek', 'deepseek-chat'


def _chat(messages, temperature=0.3, max_tokens=2048):
    """LLM chat 封装：失败抛 RuntimeError（budget/quota/网络等）。

    平台 UnifiedLLM 在 key 缺失/鉴权失败时抛 ValueError / AuthenticationError 等，
    此处统一收敛为 RuntimeError，使 routes 层的 except RuntimeError → 503 降级分支生效。
    """
    try:
        provider, model = _default_llm_target()
        return _get_llm().chat(
            messages, provider=provider, model=model,
            module=_MODULE, temperature=temperature, max_tokens=max_tokens)
    except Exception as e:
        raise RuntimeError('AI 服务暂不可用（%s）' % type(e).__name__) from e


def embed_text(text):
    """文本向量化，返回 '[0.1,0.2,...]' 字符串；失败返回 None（调用方降级）。"""
    if not text or not str(text).strip():
        return None
    try:
        vec = _get_emb().embed(str(text).strip())
    except Exception as e:
        logger.warning('embed failed: %s', e)
        return None
    if not vec:
        return None
    return '[' + ','.join(repr(v) for v in vec) + ']'


def chat_answer(conn, paper_id, question):
    """基于论文摘要 + 阅读笔记的 RAG 问答；返回回答文本。

    防幻觉约束：仅基于给定材料回答，材料缺失时明确说明"未提及"，绝不编造。
    """
    from .. import models as m

    paper = m.get_paper(conn, paper_id)
    if not paper:
        raise RuntimeError('Paper not found')
    abstract = (paper.get('abstract') or '').strip()
    notes = [n.get('content', '').strip()
             for n in m.list_annotations(conn, paper_id) if n.get('content')]

    if not abstract and not notes:
        raise RuntimeError('该论文暂无摘要或笔记，无法回答')

    context_parts = []
    if abstract:
        context_parts.append('【论文摘要】\n' + abstract)
    if notes:
        context_parts.append('【阅读笔记】\n- ' + '\n- '.join(notes[:8]))

    # ── 模块 B 扩展点：全文模块经 filter 注入全文摘录（失败自动降级）──
    try:
        from plugin_manager.hooks import get_hook_registry
        context_parts = get_hook_registry().apply_filters(
            'veroscholar.qa_context', context_parts,
            paper_id=paper_id, question=question)
    except Exception:
        pass

    system = (
        '你是一名严谨的科研助手。请仅基于给定的论文摘要与阅读笔记回答用户问题；'
        '若材料中没有答案，明确回答"材料中未提及"，绝不编造。'
        '使用与用户问题相同的语言，回答简明、分点。'
    )
    user = '论文信息：\n%s\n\n用户问题：%s\n\n请回答。' % (
        '\n\n'.join(context_parts), question)

    return _chat([{'role': 'system', 'content': system},
                  {'role': 'user', 'content': user}], temperature=0.3)


def translate(conn, paper_id, target_lang='zh'):
    """翻译论文摘要到目标语言（zh/en）；返回翻译文本。"""
    from .. import models as m

    paper = m.get_paper(conn, paper_id)
    if not paper:
        raise RuntimeError('Paper not found')
    abstract = (paper.get('abstract') or '').strip()
    if not abstract:
        raise RuntimeError('该论文暂无摘要，无法翻译')

    lang_name = '中文' if target_lang == 'zh' else 'English'
    system = (
        '你是专业学术翻译。将英文论文摘要翻译为%s，'
        '保留术语准确性与学术语气，不添加任何解释或注释。' % lang_name
    )
    user = '请翻译以下论文摘要：\n' + abstract
    return _chat([{'role': 'system', 'content': system},
                  {'role': 'user', 'content': user}], temperature=0.2)


def related_papers(conn, paper_id, limit=5):
    """相关论文推荐 — 经向量后端抽象层按环境自动选档（方案 §6.4）。

    T2 pgvector（服务器）  → 摘要向量余弦（ivfflat 索引）
    T1 本地向量（桌面）   → paper_vectors 侧表 + numpy 平面余弦
    T0 关键词（兑底）    → pg_trgm trigram 相似度

    embedding 不可用时不再抛 RuntimeError（桌面科研版约定）：
    直接落到 T0 关键词档，保证 related 入口永不 5xx。
    """
    from .. import models as m
    from . import vector_backend

    paper = m.get_paper(conn, paper_id)
    if not paper:
        raise RuntimeError('Paper not found')
    vec = embed_text(paper.get('abstract') or paper.get('title') or '')
    if vec is not None:
        try:
            vector_backend.update_paper_vector(conn, paper_id, vec)
        except Exception:
            pass
    try:
        return vector_backend.related_papers(conn, paper_id, vec, limit)
    except Exception as e:
        logger.warning('vector related failed, fallback to keyword: %s', e)
        return vector_backend.related_papers_keyword(conn, paper_id, limit)
