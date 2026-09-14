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
import re

import requests

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


def apply_search_filters(papers: list, filters: dict) -> list:
    """后置过滤：年份区间 / 被引阈值 / 期刊包含（全部可选）。"""
    out = []
    year_from = filters.get('year_from')
    year_to = filters.get('year_to')
    min_cit = filters.get('min_citations')
    venue_kw = (filters.get('venue') or '').strip().lower()
    for p in papers:
        year = p.get('year')
        if year_from is not None and (year is None or year < int(year_from)):
            continue
        if year_to is not None and (year is None or year > int(year_to)):
            continue
        if min_cit is not None and (p.get('citation_count') or 0) < int(min_cit):
            continue
        if venue_kw and venue_kw not in (p.get('venue') or '').lower():
            continue
        out.append(p)
    return out


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
    for name in (sources or ['arxiv', 'semantic', 'openalex', 'openalex_zh']):
        enabled_key = {
            'arxiv': 'arxiv_enabled',
            'semantic': 'semantic_enabled',
            'semantic_scholar': 'semantic_enabled',
            'openalex': 'openalex_enabled',
            'openalex_zh': 'zh_enabled',
        }.get(name)
        if enabled_key and cfg.get(enabled_key, True) is False:
            continue
        available.append(name)
    return available


def run_multi_source_search(keywords, sources=None, limit=30, user_id=None,
                            per_source=None):
    """多源检索 → 去重 → 入库 → 记审计日志。

    Args:
        keywords: 关键词；分号分隔多个。
        sources:  数据源名称列表；缺省取插件配置默认源。
        limit:    返回结果总数上限（去重后截断）。
        user_id:  触发者用户 id（审计）。
        per_source: 每源每关键词条数；缺省回退 limit（兼容旧行为）。
                   设置该值可保证后位源（如中文源）不被前位源挤占去重窗口。

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
    per_limit = min(int(per_source or limit), 50)

    for name in source_names:
        adapter = get_adapter(name)
        if not adapter:
            continue
        try:
            # 每个源最多取前 2 组关键词，控制耗时与频率限制
            for kw in keywords[:2]:
                try:
                    all_items.extend(adapter.search(kw, limit=per_limit))
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
            keywords, sources=config.get('sources'), limit=limit,
            user_id=user_id, per_source=config.get('per_source'))

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


def _collect_upstream_papers(input_data: dict, ctx: dict, field: str) -> list:
    """按引擎传值契约收集上游论文。

    WorkflowEngine 仅把上游节点输出存入 `node_<id>_output`（input_data 直连前驱
    + context 累积），不按 output_field 提升到顶层，因此这里：
      1. 优先读 context[papers_field]（显式提升场景）；
      2. 否则扫描 input_data / context 的 node_*_output，取含非空 papers 的上游输出。
    """
    direct = ctx.get(field) or []
    if direct:
        return direct
    found = []
    for src in (input_data, ctx):
        if not isinstance(src, dict):
            continue
        for key, value in src.items():
            if key.startswith('node_') and key.endswith('_output') \
                    and isinstance(value, dict):
                cand = value.get('papers') or []
                if cand:
                    found = cand  # 按字典序遍历，后者覆盖 → 取最靠近的上游
    return found


def handle_veroscholar_dedup(node_def: dict, input_data: dict) -> dict:
    """确定性去重排序节点（替代 ai_process，保证可复现）。

    配置:
      - papers_field: 上下文中论文数组字段，默认 papers
      - output_field: 输出字段名，默认 papers
    """
    config = node_def.get('config', {})
    try:
        ctx = input_data.get('context', {}) if isinstance(input_data, dict) else {}
        papers = _collect_upstream_papers(
            input_data, ctx, config.get('papers_field', 'papers'))
        deduped = _dedup_by_doi(papers)
        deduped.sort(key=lambda p: (-(p.get('citation_count') or 0),
                                    -(p.get('year') or 0)))
        return {'success': True, 'papers': deduped, 'count': len(deduped),
                'output_field': config.get('output_field', 'papers')}
    except Exception as e:
        logger.exception('veroscholar_dedup node failed')
        return {'success': False, 'error': str(e)}


# ── 综述生成（自定义节点：数据自洽，替代不注入上下文的通用 ai_agent）──

def _llm_chat(messages, temperature=0.7, max_tokens=4096):
    """调用平台默认模型（与 services.ai 同一解析约定）。失败抛异常由调用方收敛。"""
    from agent_matrix.engine import UnifiedLLM
    from .services import ai as _ai
    provider, model = _ai._default_llm_target()
    return UnifiedLLM().chat(
        messages, provider=provider, model=model, module='veroscholar',
        temperature=temperature, max_tokens=max_tokens)


def handle_veroscholar_review_gen(node_def: dict, input_data: dict) -> dict:
    """综述生成节点：收集上游论文 → LLM 生成结构化综述。

    取代蓝图中的通用 ai_agent 生成节点——编排器 ai_agent 只发静态
    config.prompt，不注入上游输出，导致 LLM 永远收不到论文。
    配置:
      - max_papers:   传入 LLM 的论文上限，默认 20
      - papers_field: 论文字段名，默认 papers
    """
    config = node_def.get('config', {})
    try:
        ctx = input_data.get('context', {}) if isinstance(input_data, dict) else {}
        papers = _collect_upstream_papers(
            input_data, ctx, config.get('papers_field', 'papers'))
        if not papers:
            return {'success': False, 'error': 'no papers available for review generation'}
        topic = (ctx.get('topic') or '').strip()
        top = papers[: int(config.get('max_papers', 20))]
        listing = '\n'.join(
            '- %s（%s，%s，被引 %s）' % (
                (p.get('title') or '').strip() or '（无标题）',
                p.get('year') or '—',
                (p.get('venue') or '').strip() or '—',
                p.get('citation_count') or 0)
            for p in top)
        prompt = (
            '研究主题：%s\n\n以下是检索到的 %d 篇文献：\n%s\n\n'
            '请撰写结构化中文文献综述，必须包含以下章节：研究背景与意义、'
            '技术演进脉络、核心方法分类、主要贡献与局限、研究缺口与未来方向。'
            '只使用上述给定文献，不得编造不存在的文献或引用。输出 Markdown。'
        ) % (topic or '（未指定）', len(top), listing)
        content = _llm_chat(
            [{'role': 'system',
              'content': '你是资深学术综述专家。严格基于给定的文献清单撰写综述，'
                         '绝不编造清单中不存在的文献。'},
             {'role': 'user', 'content': prompt}],
            max_tokens=8192)

        # ── W1：回写 reviews 表（失败仅告警，不阻断工作流）──
        review_id = None
        try:
            doi_ids = []
            with m.get_db() as conn:
                for p in top:
                    doi = (p.get('doi') or '').strip()
                    if not doi:
                        continue
                    row = conn.execute(
                        "SELECT id FROM papers WHERE doi = ? LIMIT 1",
                        (doi,)).fetchone()
                    if row:
                        doi_ids.append(row['id'])
                review_id = m.create_review(
                    conn,
                    title=('%s — 文献综述' % topic) if topic else '文献综述',
                    topic=topic,
                    structure={'sections': ['研究背景与意义', '技术演进脉络',
                                            '核心方法分类', '主要贡献与局限',
                                            '研究缺口与未来方向']},
                    content=content,
                    paper_ids=doi_ids,
                    created_by=ctx.get('user_id'),
                    search_strategy={
                        'keywords': topic,
                        'sources': ctx.get('sources') or [],
                        'generated_by': 'veroscholar_review_gen',
                        'paper_count': len(top),
                    })
                # 知识库写向：综述成稿同步 project_workspace（同事务，尽力而为）
                try:
                    from .services import kb_sync as _kbs
                    _kbs.sync_review(
                        conn, {'id': review_id,
                               'title': ('%s — 文献综述' % topic) if topic else '文献综述',
                               'topic': topic, 'content': content,
                               'status': 'draft'},
                        ctx.get('user_id'))
                except Exception as e:
                    logger.warning('review kb sync failed (non-fatal): %s', e)
                conn.commit()
        except Exception as e:
            logger.warning('review write-back failed (non-fatal): %s', e)

        # 认知闭环通道二：综述成稿显式回流 substrate（非致命）
        try:
            from .services import kb_sync as _kbs
            _kbs.emit_task_curation(
                user_id=ctx.get('user_id'), domain_id='veroscholar.review',
                content='综述成稿《%s》：基于 %d 篇文献，要点：%s'
                        % (topic or '文献综述', len(top),
                           (content or '')[:1200]),
                record_type='fact', source_id=str(review_id) if review_id else None,
                keywords=[w.strip() for w in topic.split(';') if w.strip()][:8])
        except Exception as e:
            logger.warning('review curation emit failed (non-fatal): %s', e)

        return {'success': True, 'content': content or '',
                'count': len(top), 'review_id': review_id}
    except Exception as e:
        logger.exception('veroscholar_review_gen failed')
        return {'success': False, 'error': str(e)}


def get_dag_nodes() -> dict:
    """返回 {节点类型: 处理器}，由插件基类注册到 WorkflowEngine。"""
    return {
        'veroscholar_search': handle_veroscholar_search,
        'veroscholar_dedup': handle_veroscholar_dedup,
        'veroscholar_review_gen': handle_veroscholar_review_gen,
    }


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


_LIT_REVIEW_WF_NAME = '文献综述生成 v5'


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
    首次调用时自动将所有旧版综述定义（'文献综述生成*'，不含当前版）置为 is_active=0。
    """
    from orchestrator import models as om
    result = om.list_workflows(active_only=False, page=1, limit=100)
    for w in result.get('workflows', []):
        if w.get('name') == _LIT_REVIEW_WF_NAME:
            if w.get('is_active'):
                return w['id']
            # 旧定义被置为 inactive 时，重新激活
            om.update_workflow(w['id'], {'is_active': 1})
            return w['id']
    # 首次运行：将所有旧版综述定义置为 inactive，再创建新版
    for w in result.get('workflows', []):
        name = w.get('name') or ''
        if name.startswith('文献综述生成') and name != _LIT_REVIEW_WF_NAME \
                and w.get('is_active'):
            om.update_workflow(w['id'], {'is_active': 0})
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


# ══════════════════════════════════════════════════════════════════
# 引用真实性校验（防 LLM 幻觉引用）
# ══════════════════════════════════════════════════════════════════

#: DOI 形如 10.xxxx/xxxx，避免吞入后续标点/空白/括号
_DOI_RE = re.compile(r'\b10\.\d{4,9}/[^\s,;)\]}"\'<>]+', re.IGNORECASE)


def _normalize_doi_token(token: str) -> str:
    """DOI token → 规范化 DOI（去 URL 前缀、去尾部标点）。"""
    doi = token.strip()
    for prefix in ('https://doi.org/', 'http://doi.org/', 'doi.org/'):
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix):]
    return doi.rstrip('.,;:)')


_CROSSREF_API = 'https://api.crossref.org/works/%s'
_DATACITE_API = 'https://api.datacite.org/dois/%s'
_VERIFY_HEADERS = {
    'User-Agent': 'VeroScholar/1.7 (citation verification; mailto:admin@verorun.ai)',
}


def resolve_doi_online(doi: str) -> dict:
    """权威注册机构在线验真。

    Args:
        doi: 待校验的 DOI 字符串。

    Returns:
        {'status': 'registered'|'retracted'|'concern'|'unregistered'|'offline',
         'notice_doi': str|None}
    """
    doi = doi.strip()
    api = _DATACITE_API if doi.lower().startswith('10.48550/') else _CROSSREF_API
    try:
        r = requests.get(api % doi, headers=_VERIFY_HEADERS, timeout=15)
    except Exception:
        return {'status': 'offline', 'notice_doi': None}
    if r.status_code == 404:
        return {'status': 'unregistered', 'notice_doi': None}
    if r.status_code != 200:
        return {'status': 'offline', 'notice_doi': None}
    try:
        payload = r.json()
    except Exception:
        return {'status': 'offline', 'notice_doi': None}
    # Crossref: {'message': {...}}；DataCite: {'data': {'attributes': {...}}}
    msg = payload.get('message') or (payload.get('data') or {}).get('attributes') or {}
    updates = msg.get('updated-by') or []
    for u in updates:
        if u.get('type') == 'retraction':
            return {'status': 'retracted', 'notice_doi': u.get('DOI')}
    for u in updates:
        if u.get('type') == 'expression_of_concern':
            return {'status': 'concern', 'notice_doi': u.get('DOI')}
    return {'status': 'registered', 'notice_doi': None}


def verify_citations(conn, review: dict) -> dict:
    """校验综述引用的真实性（权威注册机构在线验真 + 本地库补充）。

    规则：
      1. 从 reviews.content 提取 DOI token，逐条在线验真（缓存优先，7天 TTL）
      2. 本地反查 papers 表作为补充（in_library 布尔）
      3. reviews.paper_ids 中的库内论文 id 逐条反查（缺失 → missing）
    失败仅返回报告，不抛异常（校验是尽力而为的辅助能力）。

    Returns:
        {
            'total_references': int,      # content 中检测到的 DOI 总数
            'references': [               # 每条 DOI 的校验结果
                {'doi': str, 'registry_status': str, 'notice_doi': str|None, 'in_library': bool},
            ],
            'library_papers': int,        # paper_ids 中库内存在的数量
            'missing_paper_ids': [str],   # paper_ids 中库内缺失的 id
        }
    """
    content = review.get('content') or ''
    paper_ids = review.get('paper_ids') or []
    if isinstance(paper_ids, str):
        try:
            paper_ids = json.loads(paper_ids)
        except Exception:
            paper_ids = []

    references = []
    for token in _DOI_RE.findall(content):
        doi = _normalize_doi_token(token)
        if not doi:
            continue
        # 缓存优先（7 天 TTL）
        row = conn.execute(
            "SELECT status, notice_doi FROM doi_checks "
            "WHERE doi = ? AND checked_at > now() - interval '7 days'",
            (doi,)).fetchone()
        if row:
            status, notice = row['status'], row['notice_doi']
        else:
            res = resolve_doi_online(doi)
            conn.execute(
                "INSERT INTO doi_checks (doi, status, notice_doi, checked_at) "
                "VALUES (?, ?, ?, now()) "
                "ON CONFLICT (doi) DO UPDATE SET status = EXCLUDED.status, "
                "notice_doi = EXCLUDED.notice_doi, checked_at = now()",
                (doi, res['status'], res['notice_doi']))
            conn.commit()
            status, notice = res['status'], res['notice_doi']
        in_library = conn.execute(
            "SELECT 1 FROM papers WHERE doi = ? LIMIT 1", (doi,)).fetchone() is not None
        references.append({
            'doi': doi,
            'registry_status': status,
            'notice_doi': notice,
            'in_library': in_library,
        })

    missing_paper_ids = []
    for pid in paper_ids:
        row = conn.execute(
            "SELECT 1 FROM papers WHERE id = ? LIMIT 1", (pid,)).fetchone()
        if not row:
            missing_paper_ids.append(pid)

    return {
        'total_references': len(references),
        'references': references,
        'library_papers': len(paper_ids) - len(missing_paper_ids),
        'missing_paper_ids': missing_paper_ids,
    }
