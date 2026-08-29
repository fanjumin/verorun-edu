#!/usr/bin/env python3
"""VeroScholar 工作流与检索执行模块。

职责：
  1. 自定义 DAG 节点 veroscholar_search（多库检索）
  2. 多源检索统一入口 run_multi_source_search()（routes / DAG 节点复用）
  3. 综述生成工作流蓝图的加载与实例化触发

节点注册方式：
  - 插件启用时由 __init__.py 通过 register_dag_nodes() 返回
    {'veroscholar_search': handle_veroscholar_search}
"""

import json
import logging
import os

from .adapters import get_adapter
from . import models as m

logger = logging.getLogger('veroscholar.workflow')

WORKFLOWS_DIR = os.path.join(os.path.dirname(__file__), 'workflows')


# ══════════════════════════════════════════════════════════════════
# 多源检索（routes API 与 DAG 节点共用）
# ══════════════════════════════════════════════════════════════════

def _dedup_by_doi(items: list) -> list:
    """按 DOI 去重（保序），无 DOI 时按 title+year 去重。"""
    seen = set()
    result = []
    for item in items:
        key = (item.get('doi') or '').strip().lower() or \
              ((item.get('title') or '').strip().lower(), item.get('year'))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _resolve_sources(sources) -> list:
    """按插件配置过滤数据源，返回可用源名称列表。"""
    cfg = {}
    try:
        from flask import current_app
        pm = current_app.extensions.get('plugin_manager')
        if pm:
            cfg = pm.get_config('veroscholar') or {}
            if not isinstance(cfg, dict):
                cfg = {}
    except Exception:
        cfg = {}
    available = []
    for name in (sources or ['arxiv', 'semantic', 'openalex']):
        enabled_key = {
            'arxiv': 'arxiv_enabled',
            'semantic': 'semantic_enabled',
            'semantic_scholar': 'semantic_enabled',
            'openalex': 'openalex_enabled',
        }.get(name)
        if enabled_key and cfg.get(enabled_key, True) is False:
            continue
        available.append(name)
    return available


def run_multi_source_search(keywords, sources=None, limit=30, user_id=None):
    """多源检索 → 去重 → 入库 → 记审计日志。

    Returns:
        (results: list, errors: dict)  results 为统一结构的论文列表。
    单源失败不阻断其他源，errors 记录失败原因供前端展示。
    """
    if isinstance(keywords, str):
        keywords = [kw for kw in keywords.split(';') if kw.strip()] or [keywords]
    keywords = [k.strip() for k in keywords if k.strip()] or ['']

    source_names = _resolve_sources(sources)
    all_items = []
    errors = {}

    for name in source_names:
        adapter = get_adapter(name)
        if not adapter:
            continue
        try:
            # 每个源最多取前 2 组关键词，控制耗时与频率限制
            for kw in keywords[:2]:
                try:
                    all_items.extend(adapter.search(kw, limit=min(int(limit), 50)))
                except Exception as e:
                    logger.warning('adapter %s search kw=%r failed: %s', name, kw, e)
                    errors.setdefault(name, str(e))
        except Exception as e:
            logger.warning('adapter %s failed: %s', name, e)
            errors[name] = str(e)

    results = _dedup_by_doi(all_items)

    # 入库 + 审计（尽力而为，失败不阻断返回）
    try:
        with m.get_db() as conn:
            for item in results[: int(limit) * 2]:
                m.upsert_paper(conn, item)
            m.log_search(conn, ';'.join(keywords), ','.join(source_names),
                         len(results), user_id)
            conn.commit()
    except Exception as e:
        logger.error('save search results failed: %s', e)

    return results[: int(limit)], errors


# ══════════════════════════════════════════════════════════════════
# 自定义 DAG 节点：veroscholar_search
# ══════════════════════════════════════════════════════════════════

def handle_veroscholar_search(node_def: dict, input_data: dict) -> dict:
    """多库文献检索节点处理器。

    配置:
      - sources:        数据源列表，默认三源
      - limit:          每源条数
      - keywords_field: 上下文中的关键词字段，默认 keywords
      - output_field:   输出字段名，默认 papers
    输入上下文:
      - context.keywords 或 context.topic（关键词/主题）
    返回:
      {'success': True, 'papers': [...], 'count': n, 'errors': {...}}
    """
    config = node_def.get('config', {})
    try:
        ctx = input_data.get('context', {}) if isinstance(input_data, dict) else {}
        keywords = ctx.get(config.get('keywords_field', 'keywords')) \
            or ctx.get('keywords') or ctx.get('topic') or ctx.get('query') or ''
        limit = int(config.get('limit', 30))
        user_id = ctx.get('user_id')

        papers, errors = run_multi_source_search(
            keywords, sources=config.get('sources'), limit=limit, user_id=user_id)

        if not papers and errors:
            return {'success': False, 'error': json.dumps(errors, ensure_ascii=False)}

        return {
            'success': True,
            'papers': papers,
            'count': len(papers),
            'errors': errors,
            'output_field': config.get('output_field', 'papers'),
        }
    except Exception as e:
        logger.exception('veroscholar_search node failed')
        return {'success': False, 'error': str(e)}


def get_dag_nodes() -> dict:
    """返回 {节点类型: 处理器}，由插件基类注册到 WorkflowEngine。"""
    return {'veroscholar_search': handle_veroscholar_search}


def register_veroscholar_nodes(engine) -> int:
    """将自定义节点注册到指定 engine（兜底入口，主流程走 register_dag_nodes）。"""
    count = 0
    for node_type, handler in get_dag_nodes().items():
        engine.register_node_handler(node_type, handler)
        count += 1
    logger.info('registered %d veroscholar dag nodes', count)
    return count


# ══════════════════════════════════════════════════════════════════
# 综述生成工作流蓝图
# ══════════════════════════════════════════════════════════════════

def load_literature_review_template() -> dict:
    """读取 workflows/literature_review.json 蓝图。"""
    fpath = os.path.join(WORKFLOWS_DIR, 'literature_review.json')
    with open(fpath, 'r', encoding='utf-8') as f:
        return json.load(f)


_LIT_REVIEW_WF_NAME = '文献综述生成'


def create_literature_review_workflow(name: str = None) -> int:
    """把综述蓝图实例化为 orchestrator 工作流记录，返回 workflow_id。

    蓝图本身是只读模板（对齐 orchestrator/workflow_templates.py 约定）；
    这里写入 orchestrator.workflow_definitions 表，供 run_workflow() 触发。
    注意：orchestrator.models.create_workflow(data) 内部自行管理数据库连接，
    因此这里不接收外部 conn；definition 传 dict，由 orchestrator 序列化，
    避免二次 json.dumps 造成双编码。
    """
    tpl = load_literature_review_template()
    definition = {
        'nodes': tpl['definition']['nodes'],
        'edges': tpl['definition']['edges'],
    }
    from orchestrator import models as om
    wf_id = om.create_workflow({
        'name': name or tpl.get('name', _LIT_REVIEW_WF_NAME),
        'description': tpl.get('description', ''),
        'definition': definition,
        'is_active': 1,
    })
    return wf_id


def _get_or_create_lit_review_workflow() -> int:
    """复用已存在的同名综述工作流定义，避免每次触发都新建定义堆积。

    返回可用的 workflow_definitions.id。
    """
    from orchestrator import models as om
    result = om.list_workflows(active_only=True, page=1, limit=100)
    for w in result.get('workflows', []):
        if w.get('name') == _LIT_REVIEW_WF_NAME:
            return w['id']
    return create_literature_review_workflow(_LIT_REVIEW_WF_NAME)


def trigger_literature_review(engine, topic: str, user_id=None,
                              keywords=None) -> int:
    """一键触发综述工作流。

    Args:
        engine: WorkflowEngine 实例
        topic: 综述主题
        user_id: 触发者用户 id
        keywords: 可选预扩展关键词（分号分隔）

    Returns:
        workflow_instance_id
    """
    wf_id = _get_or_create_lit_review_workflow()

    initial_context = {
        'topic': topic,
        'keywords': keywords or '',
        'user_id': user_id,
    }
    return engine.run_workflow(
        wf_id, trigger_type='manual', initial_context=initial_context)
