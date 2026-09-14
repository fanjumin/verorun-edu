#!/usr/bin/env python3
"""VeroScholar 引源索骥 — Flask Blueprint。

提供:
  - 页面路由（iframe 独立页，§12.11）:
      GET /admin/veroscholar/dashboard   引源索骥总览
      GET /admin/veroscholar/search      文献检索
      GET /admin/veroscholar/review      综述生成器
  - RESTful API（JWT 管理员鉴权）:
      GET/POST /api/v1/papers[/<id>]       论文库 / 详情
      GET/POST /api/v1/papers/<id>/annotations  笔记
      POST     /api/v1/search              多库检索
      GET/POST /api/v1/projects[/<id>]     项目
      POST     /api/v1/projects/<id>/papers 论文入项目
      GET/POST /api/v1/reviews[/<id>]      综述
      POST     /api/v1/workflow/run        触发综述 DAG
      GET      /api/v1/workflow/instances/<id> 工作流实例状态
      GET      /api/v1/stats               统计
  - 静态文件: /admin/veroscholar/static/<path>（免鉴权）

鉴权: 参照 visitor_profile/routes.py — before_request + JWT 校验 is_admin。
    页面无 token 跳转登录页；API 无 token 返回 401 JSON。
"""

import json
import os
import sys
import threading
import uuid

from flask import (Blueprint, current_app, g, jsonify, redirect,
                   render_template, request, send_from_directory)

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

from plugin_manager.logger import get_plugin_logger
from . import models as m
from .workflow import (apply_search_filters, run_multi_source_search,
                       trigger_literature_review, verify_citations)
from .services import ai
from .services import kb_sync

logger = get_plugin_logger('veroscholar')

veroscholar_bp = Blueprint(
    'veroscholar', __name__,
    url_prefix='/admin/veroscholar',
    template_folder='templates',
    static_folder='static',
    static_url_path='/admin/veroscholar/static',
)

#: 免鉴权路径（静态文件）
_AUTH_EXEMPT_PATHS = ['/admin/veroscholar/static/']


# ══════════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════════

def _parse_json(value, default=None):
    """jsonb 列反序列化：psycopg2 默认返回字符串，统一解析。"""
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return value


def _load_config() -> dict:
    """从 PluginManager 读取插件配置；失败回退空配置（服务自动降级）。"""
    try:
        pm = current_app.extensions.get('plugin_manager')
        if pm:
            cfg = pm.get_config('veroscholar') or {}
            if isinstance(cfg, dict):
                return cfg
    except Exception:
        pass
    return {}


def _current_payload() -> dict:
    """返回 before_request 校验通过的 JWT payload。"""
    return getattr(g, 'veroscholar_payload', None) or {}


def _current_user_id():
    return _current_payload().get('user_id')


def _embed_text(text):
    """文本向量化，返回 '[0.1,0.2,...]' 字符串；失败返回 None（调用方降级）。"""
    try:
        from plugins._base.embeddings import EmbeddingService
        svc = EmbeddingService({'module': 'veroscholar'})
        if not svc.is_ready():
            return None
        vec = svc.embed(text)
        if vec:
            return '[' + ','.join(repr(v) for v in vec) + ']'
    except Exception as e:
        logger.warning('embed failed: %s', e)
    return None


def _paper_out(row: dict) -> dict:
    """论文行 → API 输出（解析 jsonb、剥离 embedding 向量）。"""
    if not row:
        return None
    out = dict(row)
    out['authors'] = _parse_json(row.get('authors'), [])
    out.pop('embedding', None)
    return out


def _load_translations() -> dict:
    """加载当前语言的插件翻译表。"""
    try:
        from i18n import get_lang
        locale = get_lang()
    except Exception:
        locale = 'zh-CN'
    try:
        from plugin_manager.base import _load_plugin_yaml
        data = _load_plugin_yaml(
            'veroscholar', os.path.join(os.path.dirname(__file__), 'i18n'))
        return data.get(locale, {})
    except Exception:
        return {}


def _get_workflow_engine():
    """获取全局 WorkflowEngine 实例（由 orchestrator.routes 初始化）。"""
    try:
        from orchestrator.routes import _worker_pool
        if _worker_pool and getattr(_worker_pool, 'workflow_engine', None):
            return _worker_pool.workflow_engine
    except Exception as e:
        logger.warning('workflow engine unavailable: %s', e)
    return None


# ══════════════════════════════════════════════════════════════════
# 鉴权
# ══════════════════════════════════════════════════════════════════

@veroscholar_bp.before_request
def check_auth():
    """所有路由需管理员 JWT 验证（静态文件除外）。

    页面直接访问无 token → 跳转登录页；API → 401 JSON。
    """
    path = request.path
    for exempt in _AUTH_EXEMPT_PATHS:
        if path.startswith(exempt):
            return None

    # CORS 预检放行：浏览器 preflight（OPTIONS）不携带 Authorization，
    # 蓝图级 401 会让整个插件的桌面端 API 被 CORS 拦截（表现为"网络不可达"）。
    # 放行后由 Flask 自动 OPTIONS 200 + Electron onHeadersReceived 注入
    # Access-Control-Allow-* 头完成预检；真实鉴权仍在同源实际请求上进行。
    if request.method == 'OPTIONS':
        return None

    from services.jwt_service import validate_token
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        token = request.args.get('token')
    if not token:
        token = request.cookies.get('sso_token') or request.cookies.get('tm_token')
    payload = validate_token(token) if token else None

    if not payload or not payload.get('is_admin'):
        if request.is_json or path.startswith('/admin/veroscholar/api/'):
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        return redirect('/admin/login')

    g.veroscholar_payload = payload
    return None


def _is_valid_uuid(value) -> bool:
    """校验字符串是否为合法 UUID（papers/projects/reviews 主键均为 uuid 类型）。"""
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


@veroscholar_bp.before_request
def validate_uuid_params():
    """路径中的 uuid 主键前置校验：非法 → 400。

    避免非法 uuid 直接进入 SQL，被 psycopg2 抛 DataError 后兜底成 500
    并回显底层 SQL 文本（回归测试缺陷③）。
    """
    for key in ('paper_id', 'project_id', 'review_id', 'file_id',
                'run_id', 'hypothesis_id'):
        val = (request.view_args or {}).get(key)
        if val is not None and not _is_valid_uuid(val):
            return jsonify({'success': False, 'error': 'invalid %s' % key}), 400
    return None


# ══════════════════════════════════════════════════════════════════
# 页面路由（iframe 独立页，§12.11 例外条款）
# ══════════════════════════════════════════════════════════════════

@veroscholar_bp.route('/dashboard')
def dashboard_page():
    """引源索骥总览页。"""
    return render_template('dashboard.html',
                           translations=_load_translations(),
                           g=g)


@veroscholar_bp.route('/search')
def search_page():
    """文献检索页。"""
    return render_template('search.html',
                           translations=_load_translations(),
                           g=g)


@veroscholar_bp.route('/review')
def review_page():
    """综述生成器页。"""
    return render_template('review.html',
                           translations=_load_translations(),
                           g=g)


@veroscholar_bp.route('/')
def hub_page():
    """VeroScholar 集成面板（单一入口，三功能页签）。

    goPlugin 的 embed_url 指向本路由；面板内以页签 iframe 加载
    dashboard/search/review 三个独立页面（见 templates/hub.html）。
    """
    return render_template('hub.html',
                           translations=_load_translations(),
                           g=g)


@veroscholar_bp.route('/static/<path:filename>')
def static_files(filename):
    """插件静态文件。"""
    return send_from_directory(
        os.path.join(os.path.dirname(__file__), 'static'), filename)


# ══════════════════════════════════════════════════════════════════
# API — 论文库
# ══════════════════════════════════════════════════════════════════

@veroscholar_bp.route('/api/v1/papers', methods=['GET'])
def api_list_papers():
    """论文库列表（支持 q / source_db / year / tag / status 筛选）。"""
    q = (request.args.get('q') or '').strip()
    source_db = (request.args.get('source_db') or '').strip()
    year = (request.args.get('year') or '').strip() or None
    tag_id = (request.args.get('tag') or '').strip() or None
    status = (request.args.get('status') or '').strip() or None
    limit = int(request.args.get('limit', 50))
    offset = int(request.args.get('offset', 0))
    limit = max(1, min(limit, 100))

    try:
        with m.get_db() as conn:
            papers = [_paper_out(r) for r in
                      m.list_papers(conn, limit, offset, source_db, year, q,
                                    tag_id, status)]
            total = m.count_papers(conn, source_db, year, q, tag_id, status)
        return jsonify({'success': True, 'data': papers, 'total': total})
    except Exception as e:
        logger.error('list papers failed: %s', e)
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


@veroscholar_bp.route('/api/v1/papers/<paper_id>', methods=['GET'])
def api_paper_detail(paper_id):
    """论文详情 + 笔记列表。"""
    try:
        with m.get_db() as conn:
            row = m.get_paper(conn, paper_id)
            if not row:
                return jsonify({'success': False, 'error': 'Paper not found'}), 404
            annotations = m.list_annotations(conn, paper_id)
        return jsonify({
            'success': True,
            'data': _paper_out(row),
            'annotations': annotations,
        })
    except Exception as e:
        logger.error('paper detail failed: %s', e)
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


@veroscholar_bp.route('/api/v1/papers/<paper_id>/tags', methods=['GET'])
def api_paper_tags(paper_id):
    """某论文的标签列表。"""
    try:
        with m.get_db() as conn:
            if not m.get_paper(conn, paper_id):
                return jsonify({'success': False, 'error': 'Paper not found'}), 404
            tags = m.paper_tags(conn, paper_id)
        return jsonify({'success': True, 'data': tags})
    except Exception as e:
        logger.error('paper tags failed: %s', e)
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


@veroscholar_bp.route('/api/v1/papers/<paper_id>/tags', methods=['POST'])
def api_paper_add_tag(paper_id):
    """给论文加标签（幂等）。body: {name}"""
    body = request.get_json(silent=True) or {}
    name = (body.get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'error': 'tag name is required'}), 400
    if len(name) > 64:
        return jsonify({'success': False, 'error': 'tag name too long'}), 400
    try:
        with m.get_db() as conn:
            if not m.get_paper(conn, paper_id):
                return jsonify({'success': False, 'error': 'Paper not found'}), 404
            m.add_tag(conn, paper_id, name)
            tags = m.paper_tags(conn, paper_id)
            conn.commit()
        return jsonify({'success': True, 'data': tags})
    except Exception as e:
        logger.error('paper add tag failed: %s', e)
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


@veroscholar_bp.route('/api/v1/papers/<paper_id>/tags/<int:tag_id>',
                      methods=['DELETE'])
def api_paper_remove_tag(paper_id, tag_id):
    """从论文移除标签。"""
    try:
        with m.get_db() as conn:
            if not m.get_paper(conn, paper_id):
                return jsonify({'success': False, 'error': 'Paper not found'}), 404
            m.remove_tag(conn, paper_id, tag_id)
            tags = m.paper_tags(conn, paper_id)
            conn.commit()
        return jsonify({'success': True, 'data': tags})
    except Exception as e:
        logger.error('paper remove tag failed: %s', e)
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


@veroscholar_bp.route('/api/v1/papers/<paper_id>/status', methods=['PATCH'])
def api_paper_status(paper_id):
    """更新阅读状态。body: {status: unread|reading|read}"""
    body = request.get_json(silent=True) or {}
    status = (body.get('status') or '').strip()
    try:
        with m.get_db() as conn:
            if not m.get_paper(conn, paper_id):
                return jsonify({'success': False, 'error': 'Paper not found'}), 404
            if not m.set_reading_status(conn, paper_id, status):
                return jsonify({'success': False,
                                'error': 'status must be unread/reading/read'}), 400
            conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logger.error('paper status failed: %s', e)
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


@veroscholar_bp.route('/api/v1/tags', methods=['GET'])
def api_list_tags():
    """全部标签（供筛选下拉）。"""
    try:
        with m.get_db() as conn:
            tags = m.list_tags(conn)
        return jsonify({'success': True, 'data': tags})
    except Exception as e:
        logger.error('list tags failed: %s', e)
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


@veroscholar_bp.route('/api/v1/papers/<paper_id>/export', methods=['GET'])
def api_export_paper(paper_id):
    """导出论文引用（BibTeX / RIS），供写作引用管理使用。

    请求: GET /api/v1/papers/<id>/export?format=bib|ris  （默认 bib）
    返回: text/plain 附件（带 Content-Disposition）。
    """
    fmt = (request.args.get('format') or 'bib').strip().lower()
    if fmt not in ('bib', 'ris'):
        return jsonify({'success': False, 'error': 'format must be bib or ris'}), 400
    try:
        with m.get_db() as conn:
            row = m.get_paper(conn, paper_id)
            if not row:
                return jsonify({'success': False, 'error': 'Paper not found'}), 404
            if fmt == 'bib':
                text, ext = m.to_bibtex(row), 'bib'
            else:
                text, ext = m.to_ris(row), 'ris'
        if not text:
            return jsonify({'success': False, 'error': 'Paper has no title'}), 422
        filename = 'paper-%s.%s' % (paper_id, ext)
        return current_app.response_class(
            text, mimetype='text/plain',
            headers={'Content-Disposition':
                     'attachment; filename="%s"' % filename})
    except Exception as e:
        logger.error('export paper failed: %s', e)
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


@veroscholar_bp.route('/api/v1/papers/<paper_id>/chat', methods=['POST'])
def api_paper_chat(paper_id):
    """基于论文摘要 + 笔记的 RAG 问答（防幻觉：仅基于给定材料）。"""
    body = request.get_json(silent=True) or {}
    question = (body.get('question') or '').strip()
    if not question:
        return jsonify({'success': False, 'error': 'question is required'}), 400
    try:
        with m.get_db() as conn:
            answer = ai.chat_answer(conn, paper_id, question)
            conn.commit()
        # 认知闭环通道二：论文问答直连路径显式回流（substrate 未启用时自动停用）
        try:
            kb_sync.emit_task_curation(
                user_id=_current_user_id(), domain_id='veroscholar.qa',
                content='论文问答 Q：%s\nA：%s' % (question[:600], str(answer)[:1500]),
                record_type='fact', source_id=paper_id)
        except Exception:
            pass
        return jsonify({'success': True, 'data': {'answer': answer}})
    except RuntimeError as e:
        return jsonify({'success': False, 'error': str(e)}), 503
    except Exception as e:
        logger.error('paper chat failed: %s', e)
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


@veroscholar_bp.route('/api/v1/papers/<paper_id>/translate', methods=['POST'])
def api_paper_translate(paper_id):
    """翻译论文摘要（body.target_lang: zh|en，默认 zh）。"""
    body = request.get_json(silent=True) or {}
    target_lang = (body.get('target_lang') or 'zh').strip()
    if target_lang not in ('zh', 'en'):
        target_lang = 'zh'
    try:
        with m.get_db() as conn:
            translated = ai.translate(conn, paper_id, target_lang)
            conn.commit()
        return jsonify({'success': True, 'data': {'translation': translated}})
    except RuntimeError as e:
        return jsonify({'success': False, 'error': str(e)}), 503
    except Exception as e:
        logger.error('paper translate failed: %s', e)
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


@veroscholar_bp.route('/api/v1/papers/<paper_id>/related', methods=['GET'])
def api_paper_related(paper_id):
    """相关论文推荐（向量三档降级：pgvector → 本地向量 → pg_trgm 关键词）。"""
    try:
        limit = min(max(int(request.args.get('limit') or 5), 1), 20)
    except Exception:
        limit = 5
    try:
        with m.get_db() as conn:
            related = ai.related_papers(conn, paper_id, limit)
            conn.commit()
        return jsonify({'success': True, 'data': related})
    except RuntimeError as e:
        return jsonify({'success': False, 'error': str(e)}), 503
    except Exception as e:
        logger.error('paper related failed: %s', e)
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


# ══════════════════════════════════════════════════════════════════
# API — 多库检索
# ══════════════════════════════════════════════════════════════════

@veroscholar_bp.route('/api/v1/search', methods=['POST'])
def api_search():
    """多库并行检索：去重 → 入库 → 记审计。"""
    body = request.get_json(silent=True) or {}
    keywords = (body.get('keywords') or '').strip()
    if not keywords:
        return jsonify({'success': False, 'error': 'keywords is required'}), 400

    limit = int(body.get('limit') or _load_config().get('default_search_limit', 30))
    limit = max(5, min(limit, 100))
    sources = body.get('sources')

    # 解析过滤参数
    filters = {}
    for k in ('year_from', 'year_to', 'min_citations'):
        if body.get(k) is not None:
            try:
                filters[k] = int(body[k])
            except (TypeError, ValueError):
                return jsonify({'success': False, 'error': '%s must be an integer' % k}), 400
    if body.get('venue'):
        filters['venue'] = str(body['venue']).strip()

    try:
        papers, errors = run_multi_source_search(
            keywords, sources=sources, limit=limit, user_id=_current_user_id())
        # 应用后置过滤
        if filters:
            papers = apply_search_filters(papers, filters)
        # 落库检索策略（尽力而为）
        strategy_id = None
        try:
            with m.get_db() as conn:
                strategy_id = m.log_search_strategy(conn, {
                    'keywords': keywords, 'sources': sources, 'limit': limit,
                    'filters': filters, 'result_count': len(papers),
                }, _current_user_id())
                conn.commit()
        except Exception as e:
            logger.error('log search strategy failed: %s', e)
        # 认知闭环通道二 + 论文知识同步（substrate/workspace 未启用时自动停用）
        try:
            kb_sync.emit_task_curation(
                user_id=_current_user_id(), domain_id='veroscholar.literature',
                content='多库检索「%s」命中 %d 篇（源：%s）'
                        % (keywords[:300], len(papers),
                           ','.join((sources or []) if isinstance(sources, list) else ['auto'])),
                record_type='fact', source_id=strategy_id,
                keywords=[w.strip() for w in keywords.split(';') if w.strip()][:8])
        except Exception:
            pass
        # 返回结果
        ret = {
            'success': True,
            'data': papers,
            'count': len(papers),
            'errors': errors,
        }
        if strategy_id is not None:
            ret['strategy_id'] = strategy_id
        return jsonify(ret)
    except Exception as e:
        logger.error('multi source search failed: %s', e)
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


# ══════════════════════════════════════════════════════════════════
# API — 笔记 / 批注
# ══════════════════════════════════════════════════════════════════

@veroscholar_bp.route('/api/v1/papers/<paper_id>/annotations', methods=['POST'])
def api_add_annotation(paper_id):
    """添加论文笔记；向量化在后台线程执行，不阻塞响应。"""
    body = request.get_json(silent=True) or {}
    content = (body.get('content') or '').strip()
    note_type = (body.get('note_type') or 'summary').strip()
    if not content:
        return jsonify({'success': False, 'error': 'content is required'}), 400
    if note_type not in ('summary', 'critique', 'methodology', 'question'):
        note_type = 'summary'

    try:
        with m.get_db() as conn:
            row = m.get_paper(conn, paper_id)
            if not row:
                return jsonify({'success': False, 'error': 'Paper not found'}), 404
            ann_id = m.add_annotation(
                conn, paper_id, _current_user_id(), note_type, content)
            # 知识库写向：笔记追加到论文同步文档（同事务，尽力而为）
            kb_sync.sync_annotation(conn, row,
                                    {'id': ann_id, 'note_type': note_type,
                                     'content': content},
                                    _current_user_id())
            conn.commit()
    except Exception as e:
        logger.error('add annotation failed: %s', e)
        return jsonify({'success': False, 'error': 'Internal server error'}), 500

    # 认知闭环通道二：笔记显式回流（substrate 未启用时自动停用）
    try:
        kb_sync.emit_task_curation(
            user_id=_current_user_id(), domain_id='veroscholar.notes',
            content='论文笔记[%s]：%s' % (note_type, content[:1200]),
            record_type='fact', source_id=str(ann_id))
    except Exception:
        pass

    # 后台向量化笔记内容（失败静默降级，不影响主流程；
    # 无 pgvector 的桌面库自动跳过 — vector_backend 选档）
    def _bg_embed():
        try:
            vec = _embed_text(content)
            if not vec:
                return
            from .services import vector_backend
            with m.get_db() as conn:
                if vector_backend.detect_backend(conn) != 'pgvector':
                    return
                conn.execute(
                    "UPDATE annotations SET embedding = ?::vector WHERE id = ?",
                    (vec, ann_id))
                conn.commit()
        except Exception:
            logger.warning('background embed annotation failed', exc_info=True)

    threading.Thread(target=_bg_embed, daemon=True).start()
    return jsonify({'success': True, 'id': ann_id})


# ══════════════════════════════════════════════════════════════════
# API — 项目
# ══════════════════════════════════════════════════════════════════

@veroscholar_bp.route('/api/v1/projects', methods=['GET'])
def api_list_projects():
    """项目列表。"""
    try:
        with m.get_db() as conn:
            projects = m.list_projects(conn)
            for p in projects:
                p['team_members'] = _parse_json(p.get('team_members'), [])
        return jsonify({'success': True, 'data': projects})
    except Exception as e:
        logger.error('list projects failed: %s', e)
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


@veroscholar_bp.route('/api/v1/projects', methods=['POST'])
def api_create_project():
    """创建研究项目。"""
    body = request.get_json(silent=True) or {}
    name = (body.get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'error': 'name is required'}), 400
    description = (body.get('description') or '').strip()
    try:
        with m.get_db() as conn:
            pid = m.create_project(conn, name, description, _current_user_id())
            conn.commit()
        return jsonify({'success': True, 'id': pid})
    except Exception as e:
        logger.error('create project failed: %s', e)
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


@veroscholar_bp.route('/api/v1/projects/<project_id>', methods=['GET'])
def api_project_detail(project_id):
    """项目详情（含项目内论文）。"""
    try:
        with m.get_db() as conn:
            project = m.get_project(conn, project_id)
            if not project:
                return jsonify({'success': False, 'error': 'Project not found'}), 404
            project['team_members'] = _parse_json(project.get('team_members'), [])
            papers = m.list_project_papers(conn, project_id)
            for p in papers:
                p['authors'] = _parse_json(p.get('authors'), [])
        return jsonify({'success': True, 'data': project, 'papers': papers})
    except Exception as e:
        logger.error('project detail failed: %s', e)
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


@veroscholar_bp.route('/api/v1/projects/<project_id>/papers', methods=['POST'])
def api_add_project_paper(project_id):
    """把论文加入项目。"""
    body = request.get_json(silent=True) or {}
    paper_id = (body.get('paper_id') or '').strip()
    if not paper_id:
        return jsonify({'success': False, 'error': 'paper_id is required'}), 400
    if not _is_valid_uuid(paper_id):
        return jsonify({'success': False, 'error': 'invalid paper_id'}), 400
    try:
        with m.get_db() as conn:
            if not m.get_project(conn, project_id):
                return jsonify({'success': False, 'error': 'Project not found'}), 404
            paper = m.get_paper(conn, paper_id)
            if not paper:
                return jsonify({'success': False, 'error': 'Paper not found'}), 404
            m.add_paper_to_project(conn, project_id, paper_id)
            # 知识库写向：入库项目 = 用户策展信号，同步到 project_workspace（同事务）
            kb_sync.sync_paper(conn, paper, _current_user_id())
            conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logger.error('add paper to project failed: %s', e)
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


# ══════════════════════════════════════════════════════════════════
# API — 综述
# ══════════════════════════════════════════════════════════════════

@veroscholar_bp.route('/api/v1/reviews', methods=['GET'])
def api_list_reviews():
    """综述列表。"""
    try:
        with m.get_db() as conn:
            reviews = m.list_reviews(conn, created_by=None)
        return jsonify({'success': True, 'data': reviews})
    except Exception as e:
        logger.error('list reviews failed: %s', e)
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


@veroscholar_bp.route('/api/v1/reviews/<review_id>', methods=['GET'])
def api_review_detail(review_id):
    """综述详情。"""
    try:
        with m.get_db() as conn:
            review = m.get_review(conn, review_id)
            if not review:
                return jsonify({'success': False, 'error': 'Review not found'}), 404
            review['structure'] = _parse_json(review.get('structure'), {})
            review['paper_ids'] = _parse_json(review.get('paper_ids'), [])
        return jsonify({'success': True, 'data': review})
    except Exception as e:
        logger.error('review detail failed: %s', e)
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


@veroscholar_bp.route('/api/v1/reviews/<review_id>/verify', methods=['GET'])
def api_verify_review(review_id):
    """校验综述引用的真实性（DOI 反查论文库，标记可能幻觉的引用）。"""
    try:
        with m.get_db() as conn:
            review = m.get_review(conn, review_id)
            if not review:
                return jsonify({'success': False, 'error': 'Review not found'}), 404
            report = verify_citations(conn, review)
        return jsonify({'success': True, 'data': report})
    except Exception as e:
        logger.error('verify review failed: %s', e)
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


@veroscholar_bp.route('/api/v1/reviews', methods=['POST'])
def api_create_review():
    """创建综述文档（骨架）。"""
    body = request.get_json(silent=True) or {}
    title = (body.get('title') or '').strip()
    if not title:
        return jsonify({'success': False, 'error': 'title is required'}), 400
    try:
        with m.get_db() as conn:
            rid = m.create_review(
                conn, title, (body.get('topic') or '').strip(),
                body.get('structure'), '', body.get('paper_ids') or [],
                _current_user_id(), search_strategy=body.get('search_strategy'))
            # 知识库写向：综述骨架同步到 project_workspace（同事务，尽力而为）
            kb_sync.sync_review(conn, {'id': rid, 'title': title,
                                       'topic': body.get('topic') or '',
                                       'content': '', 'status': 'draft'},
                                _current_user_id())
            conn.commit()
        return jsonify({'success': True, 'id': rid})
    except Exception as e:
        logger.error('create review failed: %s', e)
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


# ══════════════════════════════════════════════════════════════════
# API — 综述生成工作流
# ══════════════════════════════════════════════════════════════════

@veroscholar_bp.route('/api/v1/workflow/run', methods=['POST'])
def api_run_workflow():
    """一键触发文献综述 DAG 工作流。

    请求: {"topic": "综述主题", "keywords": "可选;扩展关键词"}
    返回: {"success": true, "instance_id": int}
    """
    body = request.get_json(silent=True) or {}
    topic = (body.get('topic') or '').strip()
    if not topic:
        return jsonify({'success': False, 'error': 'topic is required'}), 400

    engine = _get_workflow_engine()
    if not engine:
        return jsonify({'success': False,
                        'error': 'Workflow engine is not available'}), 503
    try:
        instance_id = trigger_literature_review(
            engine, topic, _current_user_id(),
            keywords=(body.get('keywords') or '').strip())
        # 认知闭环通道二：综述任务触发直连路径回流（终稿落库时另有 review_gen 节点回流）
        try:
            kb_sync.emit_task_curation(
                user_id=_current_user_id(), domain_id='veroscholar.review',
                content='触发综述工作流「%s」（关键词：%s）'
                        % (topic[:300], (body.get('keywords') or '')[:200]),
                record_type='fact', source_id=str(instance_id),
                keywords=[w.strip() for w in (body.get('keywords') or '').split(';') if w.strip()][:8])
        except Exception:
            pass
        return jsonify({'success': True, 'instance_id': instance_id})
    except Exception as e:
        logger.error('run literature review workflow failed: %s', e)
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


@veroscholar_bp.route('/api/v1/workflow/instances/<int:instance_id>', methods=['GET'])
def api_workflow_instance(instance_id):
    """查询综述工作流实例状态。"""
    try:
        from orchestrator import models as om
        instance = om.get_workflow_instance(instance_id)
        if not instance:
            return jsonify({'success': False, 'error': 'Instance not found'}), 404
        nodes = om.get_node_instances_by_workflow(instance_id)
        return jsonify({
            'success': True,
            'instance': instance,
            'nodes': nodes,
        })
    except Exception as e:
        logger.error('workflow instance query failed: %s', e)
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


# ══════════════════════════════════════════════════════════════════
# API — 统计
# ══════════════════════════════════════════════════════════════════

@veroscholar_bp.route('/api/v1/stats', methods=['GET'])
def api_stats():
    """插件统计（仪表盘 + 管理后台总览卡片）。"""
    try:
        with m.get_db() as conn:
            stats = m.get_stats(conn)
        return jsonify({'success': True, 'data': stats})
    except Exception as e:
        logger.error('stats failed: %s', e)
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


# ── 模块 B 路由注册（继承本蓝图鉴权/校验）──
from .fulltext.routes import register_fulltext_routes
register_fulltext_routes(veroscholar_bp)

# ── Discovery Engine 路由注册（继承本蓝图鉴权/校验）──
from .discovery import register_discovery_routes
register_discovery_routes(veroscholar_bp)
