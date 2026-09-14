#!/usr/bin/env python3
"""VeroScholar 认知闭环通道二 + 知识库双向打通（方案 §6.1 / §6.2 / §10）。

双通道认知进化（本模块负责通道二）：
  通道一（零开发）：Athena 编排路径 — agent_runner 发射 agent.task.completed，
                    memory_engine（现役引擎）订阅自动摄取。
  通道二（本模块）：插件 REST 直连路径（/api/v1/search、/api/v1/reviews、
                    笔记、论文问答）不经过 agent_runner、不发事件 —— 由本模块
                    在任务落库后显式直调 memory_engine 的 MemoryExtractor.submit
                    （合成 task/result 提交，避免误触发 reflexion）；
                    emit cogevolution.curation.submit 事件保留为 substrate 备用路径
                    （事件名与 substrate __init__.py 订阅一致，勿改）。

知识库双向打通（科研版 2026-09-07 拍板：project_workspace 已移出科研版）：
  写向 sync_paper / sync_annotation / sync_review —— 论文入库、笔记、综述成稿
        upsert 到 project_workspace（documents + document_chunks，schema 限定名
        跨 schema 写入，与调用方同事务提交或独立提交均可）。该能力保留为
        服务端版/未来重启用；科研桌面版不加载 project_workspace。
  读向 workspace_context —— 经 hook project_workspace/search 检索项目知识，
        作为 veroscholar.qa_context filter 挂入论文问答上下文（on_enable 注册）。

设计约束：
  - 全部尽力而为：任何失败仅记日志，绝不阻断主业务响应；
  - 认知引擎未启用时回流自动停用（finance/official 版零影响）；
  - 引擎绑定幂等：科研桌面（仅 memory_engine、无 project_workspace）首次回流时把用户
    cognitive_engine 显式指向 memory_engine 并补 user_profiles.meta 列
    （auth-center 建表缺该列，门控读取方静默容错，本模块自愈补列）；
    memory_engine 同场部署（服务端科研版）时不代选引擎。
"""
import json
import logging
import threading

logger = logging.getLogger('veroscholar.kb_sync')

_CURATION_SUBMIT_EVENT = 'cogevolution.curation.submit'
_SUBSTRATE_ID = 'cogevolution_substrate'
_MEMORY_ENGINE_ID = 'memory_engine'
_ensure_lock = threading.Lock()
_ensure_done = set()


# ───────────────────────── 插件可用性 ─────────────────────────

def _plugin_enabled(identifier) -> bool:
    try:
        from plugin_manager.manager import PluginManager
        from plugin_manager.models import PluginStatus
    except Exception:
        return False
    try:
        import flask
        app = flask.current_app
        manager = (app.extensions or {}).get('plugin_manager') if app else None
    except Exception:
        manager = None
    if manager is None:
        return False
    info = manager._cache.get(identifier)
    return info is not None and info.status in (
        PluginStatus.ENABLED, PluginStatus.ACTIVE)


def substrate_enabled() -> bool:
    """兼容旧名：认知引擎（memory_engine 或 substrate）是否可用。"""
    return bool(active_cognitive_engine())


def active_cognitive_engine() -> str:
    """当前启用的认知引擎（2026-09-07 用户拍板：科研版采用 memory_engine）。"""
    if _plugin_enabled(_MEMORY_ENGINE_ID):
        return _MEMORY_ENGINE_ID
    if _plugin_enabled(_SUBSTRATE_ID):
        return _SUBSTRATE_ID
    return ''


def project_workspace_enabled() -> bool:
    """project_workspace 是否启用（2026-09-07 拍板：科研版移出；同步自动停用）。"""
    return _plugin_enabled('project_workspace')


# ───────────────────────── 引擎绑定（幂等自愈） ─────────────────────────

def _ensure_meta_column(conn) -> None:
    """user_profiles.meta 缺列时补建（auth-center 建表历史缺该列）。"""
    try:
        row = conn.execute(
            "SELECT 1 FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = 'user_profiles'"
            " AND column_name = 'meta' LIMIT 1").fetchone()
        if not row:
            conn.execute(
                "ALTER TABLE public.user_profiles"
                " ADD COLUMN meta JSONB NOT NULL DEFAULT '{}'::jsonb")
            logger.info('user_profiles.meta column added (kb_sync self-heal)')
    except Exception as e:
        logger.warning('ensure user_profiles.meta failed: %s', e)


def ensure_cognitive_binding(user_id) -> bool:
    """科研桌面（仅 substrate）把用户认知引擎显式指向 substrate 并代开记忆开关。

    - 仅在 memory_engine 未启用时执行（服务端双引擎场景不代选，尊重 remind 门）；
    - 幂等：每进程每用户只做一次；已绑定的用户零写入。
    """
    if not user_id:
        return False
    engine = active_cognitive_engine()
    if not engine:
        return False
    other = _SUBSTRATE_ID if engine == _MEMORY_ENGINE_ID else _MEMORY_ENGINE_ID
    if _plugin_enabled(other):
        return False  # 双引擎共存：不代用户做选择
    key = ('user', user_id)
    with _ensure_lock:
        if key in _ensure_done:
            return True
    try:
        from plugins._base.db import get_raw_connection
        raw = get_raw_connection()
        try:
            cur = raw.cursor()
            _ensure_meta_column(cur)
            cur.execute(
                "SELECT meta FROM public.user_profiles WHERE user_id = %s FOR UPDATE",
                (user_id,))
            row = cur.fetchone()
            meta = {}
            if row and row[0]:
                meta = json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])
            changed = meta.get('cognitive_engine') != engine \
                or not meta.get('memory_opt_in')
            if changed:
                meta['cognitive_engine'] = engine
                meta['memory_opt_in'] = True
                if row:
                    cur.execute(
                        "UPDATE public.user_profiles"
                        " SET meta = %s::jsonb, updated_at = NOW() WHERE user_id = %s",
                        (json.dumps(meta), user_id))
                else:
                    cur.execute(
                        "INSERT INTO public.user_profiles (user_id, meta)"
                        " VALUES (%s, %s::jsonb)"
                        " ON CONFLICT (user_id) DO UPDATE"
                        " SET meta = EXCLUDED.meta, updated_at = NOW()",
                        (user_id, json.dumps(meta)))
                raw.commit()
                logger.info('cognitive engine bound to %s for user %s', engine, user_id)
            else:
                raw.rollback()
            cur.close()
        finally:
            raw.close()
        with _ensure_lock:
            _ensure_done.add(key)
        return True
    except Exception as e:
        logger.warning('ensure_cognitive_binding failed: %s', e)
        return False


# ───────────────────────── 通道二：显式回流 ─────────────────────────

def emit_task_curation(*, user_id=None, domain_id, content,
                       record_type='fact', source='veroscholar',
                       source_id=None, keywords=None, meta=None) -> bool:
    """直连任务落库后显式摄取到当前认知引擎（memory_engine 现役 / substrate 备用）。"""
    content = (content or '').strip()
    if not content or not domain_id:
        return False
    engine = active_cognitive_engine()
    if not engine:
        return False
    ensure_cognitive_binding(user_id)
    curation = {
        'owner_type': 'user',
        'owner_id': str(user_id or ''),
        'domain_id': str(domain_id),
        'record_type': record_type or 'fact',
        'content': content[:10000],
        'keywords': list(keywords or [])[:12],
        'source': source,
        'source_id': str(source_id) if source_id is not None else None,
        'meta': dict(meta or {}),
    }
    try:
        if engine == _MEMORY_ENGINE_ID:
            # 科研版现役引擎：直调 MemoryExtractor.submit（合成 task/result；
            # 不 emit agent.task.completed 以免误触发 reflexion）
            import flask
            manager = (flask.current_app.extensions or {}).get('plugin_manager') \
                if flask.current_app else None
            instance = manager.get_instance(_MEMORY_ENGINE_ID) if manager else None
            extractor = getattr(instance, '_extractor', None)
            if extractor is None:
                logger.warning('memory_engine extractor unavailable; curation dropped')
                return False
            extractor.submit(
                task={
                    'task_id': 'veroscholar-%s' % (source_id or 'direct'),
                    'user_id': str(user_id or ''),
                    'user_query': '[%s] %s' % (domain_id, content[:600]),
                },
                result={'summary': content[:2000], 'output': content[:2000]},
                agent_id='veroscholar')
            return True
        # substrate 备用路径
        from plugin_manager.event_bus import get_event_bus
        get_event_bus().emit(_CURATION_SUBMIT_EVENT, curation=curation)
        return True
    except Exception as e:
        logger.warning('curation emit failed: %s', e)
        return False


# ───────────────────────── 知识库写向（project_workspace） ─────────────────────────

_SYNC_PROJECT_NAME = 'VeroScholar 文献库'
_SYNC_PROJECT_DESC = 'VeroScholar 论文 / 笔记 / 综述自动同步（科研版知识闭环，请勿删除）'


def _ensure_sync_project(conn, user_id) -> str:
    """定位（或创建）veroscholar→workspace 的同步容器项目，返回 project_id。"""
    if not project_workspace_enabled():
        raise RuntimeError('project_workspace disabled')
    row = conn.execute(
        "SELECT id FROM project_workspace.projects"
        " WHERE metadata->>'veroscholar_sync' = 'true'"
        " ORDER BY created_at LIMIT 1").fetchone()
    if row:
        return row['id']
    owner_id = str(user_id or 'system')
    row = conn.execute(
        "INSERT INTO project_workspace.projects"
        " (owner_type, owner_id, name, description, metadata)"
        " VALUES ('user', ?, ?, ?, ?::jsonb) RETURNING id",
        (owner_id, _SYNC_PROJECT_NAME, _SYNC_PROJECT_DESC,
         json.dumps({'veroscholar_sync': True, 'source': 'veroscholar'}))
    ).fetchone()
    return row['id']


def _chunk_token_len(text) -> int:
    return max(1, len(text or '') // 4)


def sync_paper(conn, paper: dict, user_id=None) -> bool:
    """论文入库 → workspace documents(+chunks) upsert（幂等，paper_id 关联）。"""
    if not paper or not project_workspace_enabled():
        return False
    title = (paper.get('title') or '').strip()
    if not title:
        return False
    try:
        exists = conn.execute(
            "SELECT id FROM project_workspace.documents"
            " WHERE metadata->>'veroscholar_paper_id' = ? LIMIT 1",
            (str(paper['id']),)).fetchone()
        if exists:
            return False
        project_id = _ensure_sync_project(conn, user_id)
        abstract = (paper.get('abstract') or '').strip()
        doi = paper.get('doi') or ''
        doc = conn.execute(
            "INSERT INTO project_workspace.documents"
            " (project_id, filename, original_name, file_ext, file_size,"
            "  mime_type, status, summary, language, metadata, uploaded_by)"
            " VALUES (?, ?, ?, 'md', ?, 'text/markdown', 'ready', ?, ?, ?::jsonb, ?)"
            " RETURNING id",
            (project_id, f"{title[:200]}.md", title[:500],
             len(title) + len(abstract), abstract[:2000],
             'zh' if any('\u4e00' <= ch <= '\u9fff' for ch in title) else 'en',
             json.dumps({'veroscholar_paper_id': str(paper['id']),
                         'doi': doi, 'source_db': paper.get('source_db') or '',
                         'pdf_url': paper.get('pdf_url') or ''}),
             str(user_id or 'system'))
        ).fetchone()
        content = f"# {title}\n\n{abstract}".strip()
        conn.execute(
            "INSERT INTO project_workspace.document_chunks"
            " (document_id, project_id, chunk_index, content, token_count,"
            "  section_title, metadata)"
            " VALUES (?, ?, 0, ?, ?, 'abstract', ?::jsonb)",
            (doc['id'], project_id, content, _chunk_token_len(content),
             json.dumps({'veroscholar_paper_id': str(paper['id'])})))
        return True
    except Exception as e:
        logger.warning('sync_paper(%s) failed: %s', paper.get('id'), e)
        return False


def sync_annotation(conn, paper: dict, annotation: dict, user_id=None) -> bool:
    """笔记 → 论文同步文档的追加 chunk（无对应文档时先补同步论文）。"""
    if not paper or not annotation or not project_workspace_enabled():
        return False
    try:
        sync_paper(conn, paper, user_id)
        doc = conn.execute(
            "SELECT id, project_id FROM project_workspace.documents"
            " WHERE metadata->>'veroscholar_paper_id' = ? LIMIT 1",
            (str(paper['id']),)).fetchone()
        if not doc:
            return False
        seq = conn.execute(
            "SELECT COALESCE(MAX(chunk_index), -1) + 1 AS n"
            " FROM project_workspace.document_chunks"
            " WHERE document_id = ?", (doc['id'],)).fetchone()['n']
        content = (annotation.get('content') or '').strip()
        if not content:
            return False
        conn.execute(
            "INSERT INTO project_workspace.document_chunks"
            " (document_id, project_id, chunk_index, content, token_count,"
            "  section_title, metadata)"
            " VALUES (?, ?, ?, ?, ?, ?, ?::jsonb)",
            (doc['id'], doc['project_id'], int(seq), content[:8000],
             _chunk_token_len(content),
             f"note:{annotation.get('note_type') or 'summary'}",
             json.dumps({'veroscholar_annotation_id': str(annotation.get('id') or ''),
                         'veroscholar_paper_id': str(paper['id'])})))
        return True
    except Exception as e:
        logger.warning('sync_annotation failed: %s', e)
        return False


def sync_review(conn, review: dict, user_id=None) -> bool:
    """综述成稿 → workspace 独立文档（thesis_smith 等角色可复用）。"""
    if not review or not project_workspace_enabled():
        return False
    title = (review.get('title') or '').strip()
    content = (review.get('content') or '').strip()
    if not title or not content:
        return False
    try:
        exists = conn.execute(
            "SELECT id FROM project_workspace.documents"
            " WHERE metadata->>'veroscholar_review_id' = ? LIMIT 1",
            (str(review['id']),)).fetchone()
        if exists:
            return False
        project_id = _ensure_sync_project(conn, user_id)
        doc = conn.execute(
            "INSERT INTO project_workspace.documents"
            " (project_id, filename, original_name, file_ext, file_size,"
            "  mime_type, status, summary, metadata, uploaded_by)"
            " VALUES (?, ?, ?, 'md', ?, 'text/markdown', 'ready', ?, ?::jsonb, ?)"
            " RETURNING id",
            (project_id, f"{title[:200]}.md", title[:500], len(content),
             content[:2000],
             json.dumps({'veroscholar_review_id': str(review['id']),
                         'topic': review.get('topic') or '',
                         'status': review.get('status') or 'draft'}),
             str(user_id or 'system'))
        ).fetchone()
        conn.execute(
            "INSERT INTO project_workspace.document_chunks"
            " (document_id, project_id, chunk_index, content, token_count,"
            "  section_title, metadata)"
            " VALUES (?, ?, 0, ?, ?, 'review', ?::jsonb)",
            (doc['id'], project_id, content[:100000],
             _chunk_token_len(content),
             json.dumps({'veroscholar_review_id': str(review['id'])})))
        return True
    except Exception as e:
        logger.warning('sync_review(%s) failed: %s', review.get('id'), e)
        return False


# ───────────────────────── 知识库读向（hook 合并检索） ─────────────────────────

def workspace_context(query, top_k=6):
    """经 project_workspace/search hook 检索项目知识，返回注入用上下文段列表。"""
    if not (query or '').strip():
        return []
    try:
        from plugin_manager.hooks import get_hook_registry
        result = get_hook_registry().apply_filters(
            'project_workspace/search', query, project_id='', top_k=int(top_k))
    except Exception as e:
        logger.warning('project_workspace/search hook failed: %s', e)
        return []
    if not isinstance(result, dict) or not result.get('ok'):
        return []
    parts = []
    for item in (result.get('results') or [])[: int(top_k)]:
        if not isinstance(item, dict):
            continue
        text = (item.get('content') or '').strip()
        if not text:
            continue
        src = item.get('filename') or item.get('original_name') or '项目知识'
        parts.append(f'【项目知识 · {src}】\n{text[:1200]}')
    return parts


def register_kb_hooks() -> None:
    """on_enable 注册：论文问答上下文合并 project_workspace 知识（读向打通）。

    幂等：与 fulltext 钩子同先例（HookRegistry.add_filter 自动去重）。
    """
    from plugin_manager.hooks import get_hook_registry

    def _inject_workspace_context(context_parts, **kwargs):
        try:
            question = str(kwargs.get('question') or '')
            extra = workspace_context(question, top_k=6)
            if extra:
                return list(context_parts) + extra
        except Exception as e:
            logger.warning('workspace context inject failed: %s', e)
        return context_parts

    hooks = get_hook_registry()
    if not hooks.has_filter('veroscholar.qa_context', _inject_workspace_context):
        hooks.add_filter('veroscholar.qa_context', _inject_workspace_context,
                         identifier='veroscholar_kb_sync')
