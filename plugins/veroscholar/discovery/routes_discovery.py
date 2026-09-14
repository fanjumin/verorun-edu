#!/usr/bin/env python3
"""Discovery Engine API 路由（v1.11.0）。

注册到主插件 veroscholar_bp（继承蓝图级 JWT 管理员鉴权 + UUID 校验），
端点一律返回 `{success: true, data: ...}` / `{success: false, error: ...}`
（与插件其余 API 约定一致）。

端点：
  GET  /admin/veroscholar/discovery            发现工作台页面（iframe）
  POST /admin/veroscholar/api/v1/discovery/trigger   触发一轮发现
  GET  /admin/veroscholar/api/v1/discovery/runs      历史运行列表
  GET  /admin/veroscholar/api/v1/discovery/runs/<run_id>      运行详情
  GET  /admin/veroscholar/api/v1/discovery/runs/<run_id>/hypotheses  假设列表
  GET  /admin/veroscholar/api/v1/discovery/runs/<run_id>/discussions 讨论记录
  GET  /admin/veroscholar/api/v1/discovery/stats    发现引擎统计
  POST /admin/veroscholar/api/v1/discovery/hypotheses/<id>/vote  人工投票

门禁：discovery_enabled=false 时所有 API 返回 403，页面正常打开（仅展示
"功能未启用"提示），确保关闭状态零副作用。
"""

import json
import logging
import threading

from flask import g, jsonify, render_template, request

from .. import models as m
from . import models_discovery as md
from . import nodes as nd
from . import discuss as ds

logger = logging.getLogger('veroscholar.discovery.routes')


# ══════════════════════════════════════════════════════════════════
# 路由注册
# ══════════════════════════════════════════════════════════════════

def register_discovery_routes(bp):
    """将 discovery 路由注册到指定蓝图（由 routes.py 底部调用）。"""

    # ── 页面 ──
    @bp.route('/discovery', methods=['GET'])
    def discovery_page():
        """发现工作台页面（经 hub 页签访问）。"""
        try:
            from ..routes import _load_translations
            translations = _load_translations()
        except Exception:
            translations = {}
        return render_template('discovery.html', translations=translations)

    # ── 触发发现 ──
    @bp.route('/api/v1/discovery/trigger', methods=['POST'])
    def api_discovery_trigger():
        """触发一轮发现。

        请求体: {question, project_id?, mode?: "sync"|"async"|"auto"}
        - auto（默认）: 引擎可用走 DAG 异步；否则降级后台线程执行（DE-5：
          立即返回 run_id，前端轮询，根治网关 322s 超时截断）
        - sync: 强制同步阻塞（调试/测试专用，生产勿用）
        - async: 强制 DAG 异步
        """
        if not _discovery_enabled():
            return jsonify({'success': False,
                            'error': 'Discovery engine is disabled'}), 403
        body = request.get_json(silent=True) or {}
        question = str(body.get('question') or '').strip()
        if not question:
            return jsonify({'success': False, 'error': 'question is required'}), 400
        if len(question) > 300:
            return jsonify({'success': False,
                            'error': 'question is too long (max 300 chars)'}), 400
        project_id = body.get('project_id')
        mode = str(body.get('mode') or 'auto')
        user_id = _current_user_id()

        if mode == 'sync':
            return _sync_generate(question, project_id, user_id)

        try:
            return _async_generate(question, project_id, user_id)
        except Exception as e:
            # DE-5：降级不再阻塞请求（整条 LLM 流水线 >322s 会被网关掐断，
            # 且后台实际仍在跑，前端却显示失败）。改为后台线程执行流水线，
            # 立即返回 run_id，前端轮询运行详情直至终态。
            logger.warning('async discovery unavailable, falling back to '
                           'background run: %s', e)
            return _background_generate(question, project_id, user_id)

    # ── 运行列表 ──
    @bp.route('/api/v1/discovery/runs', methods=['GET'])
    def api_discovery_runs():
        """当前用户的历史发现运行（倒序分页）。"""
        if not _discovery_enabled():
            return jsonify({'success': False,
                            'error': 'Discovery engine is disabled'}), 403
        try:
            limit = min(int(request.args.get('limit', 20)), 50)
            offset = max(int(request.args.get('offset', 0)), 0)
        except (TypeError, ValueError):
            limit, offset = 20, 0
        try:
            with m.get_db() as conn:
                runs = md.list_runs(conn, _current_user_id(), limit, offset)
                total = md.count_runs(conn, _current_user_id())
            return jsonify({'success': True,
                            'data': {'runs': runs, 'total': total}})
        except Exception as e:
            logger.error('list runs failed: %s', e)
            return jsonify({'success': False,
                            'error': 'Internal server error'}), 500

    # ── 运行详情 ──
    @bp.route('/api/v1/discovery/runs/<run_id>', methods=['GET'])
    def api_discovery_run_detail(run_id):
        """运行详情 + 事件流 + 统计。"""
        if not _discovery_enabled():
            return jsonify({'success': False,
                            'error': 'Discovery engine is disabled'}), 403
        try:
            with m.get_db() as conn:
                run = md.get_run(conn, run_id)
                if not run:
                    return jsonify({'success': False,
                                    'error': 'Run not found'}), 404
                events = md.list_events(conn, run_id)
            data = dict(run)
            data['events'] = events
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            logger.error('run detail failed: %s', e)
            return jsonify({'success': False,
                            'error': 'Internal server error'}), 500

    # ── 假设列表 ──
    @bp.route('/api/v1/discovery/runs/<run_id>/hypotheses', methods=['GET'])
    def api_discovery_hypotheses(run_id):
        """一轮运行的全部假设（Elo 降序，含证据池与讨论）。"""
        if not _discovery_enabled():
            return jsonify({'success': False,
                            'error': 'Discovery engine is disabled'}), 403
        try:
            with m.get_db() as conn:
                run = md.get_run(conn, run_id)
                if not run:
                    return jsonify({'success': False,
                                    'error': 'Run not found'}), 404
                hypotheses = md.list_hypotheses(conn, run_id)
                out = []
                for h in hypotheses:
                    item = dict(h)
                    item['evidence_ids'] = _loads(item.get('evidence_ids'), [])
                    item['discussions'] = md.list_discussions(conn, h['id'])
                    out.append(item)
            return jsonify({'success': True, 'data': out})
        except Exception as e:
            logger.error('list hypotheses failed: %s', e)
            return jsonify({'success': False,
                            'error': 'Internal server error'}), 500

    # ── 讨论记录 ──
    @bp.route('/api/v1/discovery/runs/<run_id>/discussions', methods=['GET'])
    def api_discovery_discussions(run_id):
        """一轮运行下全部讨论记录（按假设分组）。"""
        if not _discovery_enabled():
            return jsonify({'success': False,
                            'error': 'Discovery engine is disabled'}), 403
        try:
            with m.get_db() as conn:
                run = md.get_run(conn, run_id)
                if not run:
                    return jsonify({'success': False,
                                    'error': 'Run not found'}), 404
                hypotheses = md.list_hypotheses(conn, run_id)
                groups = []
                for h in hypotheses:
                    groups.append({
                        'hypothesis_id': h['id'],
                        'statement': h.get('statement'),
                        'discussions': md.list_discussions(conn, h['id']),
                    })
            return jsonify({'success': True, 'data': groups})
        except Exception as e:
            logger.error('list discussions failed: %s', e)
            return jsonify({'success': False,
                            'error': 'Internal server error'}), 500

    # ── 统计 ──
    @bp.route('/api/v1/discovery/stats', methods=['GET'])
    def api_discovery_stats():
        """发现引擎全局统计。"""
        if not _discovery_enabled():
            return jsonify({'success': False,
                            'error': 'Discovery engine is disabled'}), 403
        try:
            with m.get_db() as conn:
                stats = md.get_stats(conn)
            return jsonify({'success': True, 'data': stats})
        except Exception as e:
            logger.error('discovery stats failed: %s', e)
            return jsonify({'success': False,
                            'error': 'Internal server error'}), 500

    # ── 人工投票 ──
    @bp.route('/api/v1/discovery/hypotheses/<hypothesis_id>/vote', methods=['POST'])
    def api_discovery_vote(hypothesis_id):
        """人工投票：{up: true} 与排名最高的另一条假设对比并记录 Elo。"""
        if not _discovery_enabled():
            return jsonify({'success': False,
                            'error': 'Discovery engine is disabled'}), 403
        body = request.get_json(silent=True) or {}
        try:
            with m.get_db() as conn:
                h = md.get_hypothesis(conn, hypothesis_id)
                if not h:
                    return jsonify({'success': False,
                                    'error': 'Hypothesis not found'}), 404
                peers = md.list_ranked(conn, h['run_id'])
                if len(peers) < 2:
                    return jsonify({'success': False,
                                    'error': 'Not enough hypotheses to vote'}), 400
                opponent = peers[0] if peers[0]['id'] != hypothesis_id else peers[1]
                if body.get('up', True):
                    winner, loser = hypothesis_id, opponent['id']
                else:
                    winner, loser = opponent['id'], hypothesis_id
                md.record_vote(conn, run_id=h['run_id'],
                               winner_id=winner, loser_id=loser,
                               voter='user:%s' % _current_user_id())
                ranked = md.list_ranked(conn, h['run_id'])
                conn.commit()
            return jsonify({'success': True, 'data': ranked})
        except Exception as e:
            logger.error('vote failed: %s', e)
            return jsonify({'success': False,
                            'error': 'Internal server error'}), 500

    return bp


# ══════════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════════

def _discovery_enabled() -> bool:
    """discovery 总开关（默认关闭）。"""
    try:
        from ..routes import _load_config
        cfg = _load_config()
    except Exception:
        cfg = {}
    return bool(cfg.get('discovery_enabled', False))


def _current_user_id():
    payload = getattr(g, 'veroscholar_payload', None) or {}
    return payload.get('user_id')


def _get_workflow_engine():
    """复用主插件 routes.py 的全局 WorkflowEngine 获取方式。"""
    try:
        from ..routes import _get_workflow_engine as _engine
        return _engine()
    except Exception as e:
        logger.warning('workflow engine unavailable: %s', e)
        return None


def _loads(value, default=None):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return value


# ══════════════════════════════════════════════════════════════════
# 触发路径
# ══════════════════════════════════════════════════════════════════

def _async_generate(question, project_id, user_id):
    """DAG 异步路径：自动创建/复用"假设发现锦标赛"工作流并提交执行。

    返回 {instance_id, run_id, mode: 'dag'}。
    """
    with m.get_db() as conn:
        run_id = md.create_run(conn, question=question, user_id=user_id,
                               project_id=project_id, trigger='dag')
        wf_id = nd._get_or_create_hypothesis_workflow(conn)
        conn.commit()
    # DE-6：run 级成本核算起点。persist 节点现统一写 metrics.llm_calls，
    # 与 sync 路径对称，在提交 DAG 前重置计数（并发多 run 串扰见 M1 RunBudget）。
    ds.reset_llm_calls()
    try:
        engine = _get_workflow_engine()
        if not engine:
            raise RuntimeError('workflow engine unavailable')
        instance_id = engine.run_workflow(
            wf_id, trigger_type='manual', trigger_config={},
            initial_context={'question': question, 'user_id': user_id,
                             'project_id': project_id, 'run_id': run_id})
    except Exception:
        # 清理孤儿 run：异步路径失败即回退同步，先标记本次已建 run 失败，
        # 避免残留 status='running' 的孤立记录（幂等窗口外的换措辞触发也覆盖）。
        try:
            with m.get_db() as conn:
                md.finish_run(conn, run_id, status='failed',
                              error='async engine unavailable')
                conn.commit()
        except Exception:
            pass
        raise
    return jsonify({'success': True,
                    'data': {'mode': 'dag', 'instance_id': instance_id,
                             'run_id': run_id}})


def _run_pipeline(run_id, question, project_id, user_id):
    """执行完整发现流水线（节点顺序与 DAG 一致），所有产物落库。

    DE-5：由 sync 阻塞路径与后台线程路径共用，返回纯 dict（不触碰 Flask
    request/g 上下文，可安全在子线程执行）：
      {'ok': True,  'data': {mode, run_id, hypotheses, events, clusters}}
      {'ok': False, 'error': str}
    失败时对应节点已 finish_run(failed)，成功时 persist 节点已 finish_run。
    """
    ds.reset_llm_calls()  # run 级成本核算：从本 run 开始累计生产 LLM 调用

    def _emit(stage, status='info', detail=None):
        try:
            with m.get_db() as conn:
                md.add_event(conn, run_id=run_id, stage=stage,
                             status=status, detail=detail)
                conn.commit()
        except Exception:
            pass

    try:
        _emit('generate')
        gen = nd.handle_hypo_gen(
            {'config': {'num_hypotheses': 5}},
            {'context': {'question': question, 'user_id': user_id,
                         'project_id': project_id, 'run_id': run_id}})
        if not gen.get('success'):
            _emit('generate', 'failed', {'error': gen.get('error')})
            with m.get_db() as conn:
                md.finish_run(conn, run_id, status='failed',
                              error=gen.get('error'))
                conn.commit()
            return {'ok': False, 'error': gen.get('error')}
        ctx = {'question': question, 'user_id': user_id,
               'project_id': project_id, 'run_id': run_id,
               'candidate_hypotheses': gen.get('candidate_hypotheses')}
        _emit('evidence')
        ev = nd.handle_hypo_evidence({'config': {}}, {'context': ctx})
        ctx['evidence_pool'] = ev.get('evidence_pool', [])
        _emit('rank')
        rk = nd.handle_hypo_rank({'config': {}}, {'context': ctx})
        ctx['ranked_hypotheses'] = rk.get('ranked_hypotheses', [])
        # DE-7：rank 对局与 cluster 聚类归属注入 ctx，persist 才能落
        # hypothesis_votes(引擎审计) 与 hypotheses.cluster_id —— 此前被丢在两处。
        ctx['matches'] = rk.get('matches', [])
        _emit('cluster')
        cl = nd.handle_hypo_cluster({'config': {}}, {'context': ctx})
        ctx['cluster_assignments'] = cl.get('cluster_assignments', {})
        _emit('discuss')
        dg = nd.handle_hypo_discuss({'config': {}}, {'context': ctx})
        ctx['rounds'] = dg.get('rounds', [])
        _emit('judge')
        jg = nd.handle_hypo_judge({'config': {}}, {'context': ctx})
        ctx['judged_hypotheses'] = jg.get('judged_hypotheses', [])
        _emit('persist')
        ps = nd.handle_hypo_persist({'config': {}}, {'context': ctx})
        if not ps.get('success'):
            return {'ok': False, 'error': ps.get('error')}

        with m.get_db() as conn:
            hypotheses = md.list_hypotheses(conn, run_id)
            events = md.list_events(conn, run_id)
        return {'ok': True, 'data': {
            'mode': 'sync', 'run_id': run_id,
            'hypotheses': hypotheses, 'events': events,
            'clusters': cl.get('clusters', []),
        }}
    except Exception as e:
        logger.exception('pipeline failed (run_id=%s)', run_id)
        _emit('error', 'failed', {'error': str(e)})
        try:
            with m.get_db() as conn:
                md.finish_run(conn, run_id, status='failed', error=str(e))
                conn.commit()
        except Exception:
            pass
        return {'ok': False, 'error': str(e)}


def _create_run(question, project_id, user_id, trigger):
    """创建 run 记录并返回 run_id；失败抛异常由调用方处理。"""
    with m.get_db() as conn:
        run_id = md.create_run(conn, question=question, user_id=user_id,
                               project_id=project_id, trigger=trigger)
        conn.commit()
    return run_id


def _background_generate(question, project_id, user_id):
    """DE-5 降级路径：后台线程执行流水线，立即返回 run_id（不阻塞请求）。

    引擎不可用（桌面版/无 orchestrator）时替代旧的同步阻塞降级，避免整条
    LLM 流水线在网关超时（实测 322s）后被截断、前端误报失败而后台仍在执行。
    前端凭 run_id 轮询 /runs/<run_id> 直至 status 进入终态。
    """
    try:
        run_id = _create_run(question, project_id, user_id, 'manual')
    except Exception as e:
        logger.error('create run failed: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500

    def _worker():
        try:
            _run_pipeline(run_id, question, project_id, user_id)
        except Exception:
            logger.exception('background discovery worker crashed')

    threading.Thread(target=_worker, daemon=True,
                     name='veroscholar-disc-%s' % str(run_id)[:8]).start()
    return jsonify({'success': True, 'data': {
        'mode': 'background', 'run_id': run_id, 'status': 'running',
    }})


def _sync_generate(question, project_id, user_id):
    """强制同步路径（mode='sync'，调试/测试专用）：阻塞执行并返回完整产物。

    与 DAG 路径产出结构一致：{mode: 'sync', run_id, hypotheses, events}。
    """
    try:
        run_id = _create_run(question, project_id, user_id, 'manual')
    except Exception as e:
        logger.error('create run failed: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500
    result = _run_pipeline(run_id, question, project_id, user_id)
    if not result['ok']:
        return jsonify({'success': False, 'error': result['error']}), 500
    return jsonify({'success': True, 'data': result['data']})
