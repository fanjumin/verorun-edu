#!/usr/bin/env python3
"""VeroScholar 科研工作台 — Flask Blueprint。

提供:
  - 页面路由（iframe 独立页，§12.11）:
      GET /admin/veroscholar/dashboard   科研工作台总览
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

from flask import (Blueprint, current_app, g, jsonify, redirect,
                   render_template, request, send_from_directory)

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

from plugin_manager.logger import get_plugin_logger
from . import models as m
from .workflow import run_multi_source_search, trigger_literature_review

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


# ══════════════════════════════════════════════════════════════════
# 页面路由（iframe 独立页，§12.11 例外条款）
# ══════════════════════════════════════════════════════════════════

@veroscholar_bp.route('/dashboard')
def dashboard_page():
    """科研工作台总览页。"""
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
    """论文库列表（支持 q / source_db / year 筛选）。"""
    q = (request.args.get('q') or '').strip()
    source_db = (request.args.get('source_db') or '').strip()
    year = (request.args.get('year') or '').strip() or None
    limit = int(request.args.get('limit', 50))
    offset = int(request.args.get('offset', 0))
    limit = max(1, min(limit, 100))

    try:
        with m.get_db() as conn:
            papers = [_paper_out(r) for r in
                      m.list_papers(conn, limit, offset, source_db, year, q)]
            total = m.count_papers(conn, source_db, year, q)
        return jsonify({'success': True, 'data': papers, 'total': total})
    except Exception as e:
        logger.error('list papers failed: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': str(e)}), 500


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

    try:
        papers, errors = run_multi_source_search(
            keywords, sources=sources, limit=limit, user_id=_current_user_id())
        return jsonify({
            'success': True,
            'data': papers,
            'count': len(papers),
            'errors': errors,
        })
    except Exception as e:
        logger.error('multi source search failed: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500


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
            conn.commit()
    except Exception as e:
        logger.error('add annotation failed: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500

    # 后台向量化笔记内容（失败静默降级，不影响主流程）
    def _bg_embed():
        try:
            vec = _embed_text(content)
            if not vec:
                return
            with m.get_db() as conn:
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
        return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': str(e)}), 500


@veroscholar_bp.route('/api/v1/projects/<project_id>/papers', methods=['POST'])
def api_add_project_paper(project_id):
    """把论文加入项目。"""
    body = request.get_json(silent=True) or {}
    paper_id = (body.get('paper_id') or '').strip()
    if not paper_id:
        return jsonify({'success': False, 'error': 'paper_id is required'}), 400
    try:
        with m.get_db() as conn:
            if not m.get_project(conn, project_id):
                return jsonify({'success': False, 'error': 'Project not found'}), 404
            if not m.get_paper(conn, paper_id):
                return jsonify({'success': False, 'error': 'Paper not found'}), 404
            m.add_paper_to_project(conn, project_id, paper_id)
            conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logger.error('add paper to project failed: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': str(e)}), 500


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
                _current_user_id())
            conn.commit()
        return jsonify({'success': True, 'id': rid})
    except Exception as e:
        logger.error('create review failed: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': True, 'instance_id': instance_id})
    except Exception as e:
        logger.error('run literature review workflow failed: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': str(e)}), 500


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
        return jsonify({'success': False, 'error': str(e)}), 500
