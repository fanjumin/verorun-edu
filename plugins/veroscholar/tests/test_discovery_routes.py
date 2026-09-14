#!/usr/bin/env python3
"""test_discovery_routes.py — Discovery Engine API 路由集成测试。

基于 Flask test client + 真实 veroscholar_bp（含 discovery 路由注册），
mock 鉴权（services.jwt_service）与数据层/节点层（routes_discovery 引用），
不连数据库、不调真实 LLM。

运行:
    cd F:\\Sites\\VeroRun
    python -m unittest plugins.veroscholar.tests.test_discovery_routes -v
"""

import sys
import os
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from flask import Flask

# ── 预置 fake services.jwt_service（测试隔离，同 test_routes.py 范式）──
# 注意：全量 discover 时 test_routes.py 也会创建同名桩，后导入者覆盖 sys.modules；
# 因此这里仅在缺失时创建，且 setUp/tearDown 一律从 sys.modules 动态解析，
# 避免持有陈旧模块引用导致鉴权桩失效。
import types

if 'services' not in sys.modules:
    _services = types.ModuleType('services')
    _jwt_stub = types.ModuleType('services.jwt_service')
    _jwt_stub.validate_token = lambda token: None
    _services.jwt_service = _jwt_stub
    sys.modules['services'] = _services
    sys.modules['services.jwt_service'] = _jwt_stub
del types

from plugins.veroscholar.routes import veroscholar_bp
from plugins.veroscholar.discovery import routes_discovery as rd

#: 合法 UUID 常量 — 路径参数须为 uuid 格式（routes 前置校验非法 → 400）
RUN_ID = '55555555-5555-4555-8555-555555555555'
HYP_ID = '66666666-6666-4666-8666-666666666666'
HYP_ID2 = '77777777-7777-4777-8777-777777777777'

RUN = {
    'id': RUN_ID, 'question': 'Q', 'project_id': None, 'user_id': 1,
    'trigger': 'manual', 'status': 'completed', 'config': '{}', 'error': '',
    'created_at': '2026-09-01', 'finished_at': '2026-09-01',
}
HYPO = {
    'id': HYP_ID, 'run_id': RUN_ID, 'statement': 'H1', 'score': 0.9,
    'evidence_ids': '["e1"]', 'cluster_id': 'c1', 'current_status': 'pending',
    'elo_rating': 1016.0, 'created_at': '2026-09-01', 'updated_at': '2026-09-01',
}
HYPO2 = dict(HYPO, id=HYP_ID2, statement='H2', elo_rating=984.0)
EVENT = {'stage': 'generate', 'status': 'info', 'detail': '{}',
         'created_at': '2026-09-01'}
DISC = {'protocol': 'discussion', 'question': 'Q', 'response': 'R',
        'meta': '{}', 'created_at': '2026-09-01'}
STATS = {'total_runs': 3, 'running_runs': 1, 'total_hypotheses': 9,
         'avg_hypothesis_score': 0.62}


class DummyCtx:
    def __init__(self, conn=None):
        self.conn = conn or mock.MagicMock()

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        return False


class AuthedClient:
    """包装 Flask test_client，自动注入管理员 Authorization 头。"""

    def __init__(self, client):
        self._client = client

    def _headers(self, kwargs):
        headers = dict(kwargs.pop('headers', {}))
        headers.setdefault('Authorization', 'Bearer test-token')
        return headers

    def get(self, *args, **kwargs):
        return self._client.get(*args, headers=self._headers(kwargs), **kwargs)

    def post(self, *args, **kwargs):
        return self._client.post(*args, headers=self._headers(kwargs), **kwargs)


class DiscoveryRouteBase(unittest.TestCase):
    """默认启用 discovery + 全部数据层/节点层打桩，测试按需覆盖。"""

    def setUp(self):
        # 从 sys.modules 动态解析桩模块（避免 discover 全量跑时持有陈旧引用）
        jwt_mod = sys.modules['services.jwt_service']
        self._real_validate = jwt_mod.validate_token
        jwt_mod.validate_token = lambda token: {
            'user_id': 1, 'is_admin': True, 'username': 'admin'}

        self.app = Flask(__name__)
        self.app.jinja_env.globals['_'] = lambda s: s
        self.app.register_blueprint(veroscholar_bp)
        self.app.config['TESTING'] = True
        self.client = AuthedClient(self.app.test_client())

        self._patchers = []
        self._enabled = mock.patch.object(rd, '_discovery_enabled',
                                          return_value=True)
        self._enabled.start()
        self._patch_models()

    def tearDown(self):
        sys.modules['services.jwt_service'].validate_token = self._real_validate
        # 逆序停止：同属性被测试内二次 patch 时（如 create_run/get_hypothesis），
        # 正序 stop 会先覆盖内层再还原成外层桩值 → mock 泄漏到其它测试模块
        for p in reversed(self._patchers):
            p.stop()
        self._enabled.stop()

    def _patch(self, obj, name, fn):
        p = mock.patch.object(obj, name, fn)
        p.start()
        self._patchers.append(p)
        return p

    def _patch_models(self):
        conn = mock.MagicMock()
        conn.commit.return_value = None
        conn.rollback.return_value = None
        self._conn = conn
        self._patch(rd.m, 'get_db', lambda: DummyCtx(conn))
        self._patch(rd.md, 'create_run', lambda conn, **k: RUN_ID)
        self._patch(rd.md, 'add_event', lambda *a, **k: None)
        self._patch(rd.md, 'finish_run', lambda *a, **k: None)
        self._patch(rd.md, 'list_runs',
                    lambda conn, uid, limit=20, offset=0: [dict(RUN)])
        self._patch(rd.md, 'count_runs', lambda conn, uid: 3)
        self._patch(rd.md, 'get_run', lambda conn, rid: dict(RUN))
        self._patch(rd.md, 'list_events', lambda conn, rid: [dict(EVENT)])
        self._patch(rd.md, 'list_hypotheses', lambda conn, rid: [dict(HYPO)])
        self._patch(rd.md, 'list_discussions', lambda conn, hid: [dict(DISC)])
        self._patch(rd.md, 'get_stats', lambda conn: dict(STATS))
        self._patch(rd.md, 'get_hypothesis', lambda conn, hid: dict(HYPO))
        self._patch(rd.md, 'list_ranked',
                    lambda conn, rid: [dict(HYPO), dict(HYPO2)])
        self._patch(rd.md, 'record_vote', lambda *a, **k: None)
        self._patch(rd.nd, 'handle_hypo_gen', lambda *a, **k: {
            'success': True,
            'candidate_hypotheses': [{'statement': 'H1', 'score': 0.8}]})
        self._patch(rd.nd, 'handle_hypo_evidence',
                    lambda *a, **k: {'success': True, 'evidence_pool': []})
        self._patch(rd.nd, 'handle_hypo_rank', lambda *a, **k: {
            'success': True, 'ranked_hypotheses': [
                {'statement': 'H1', 'score': 0.8, 'elo_rating': 1016.0}]})
        self._patch(rd.nd, 'handle_hypo_cluster',
                    lambda *a, **k: {'success': True, 'clusters': []})
        self._patch(rd.nd, 'handle_hypo_discuss',
                    lambda *a, **k: {'success': True, 'rounds': []})
        self._patch(rd.nd, 'handle_hypo_judge', lambda *a, **k: {
            'success': True, 'judged_hypotheses': [
                {'id': HYP_ID, 'statement': 'H1', 'score': 0.9}]})
        self._patch(rd.nd, 'handle_hypo_persist',
                    lambda *a, **k: {'success': True})
        self._patch(rd.nd, '_get_or_create_hypothesis_workflow',
                    lambda conn: 7)


class TestDiscoveryAuth(unittest.TestCase):
    def test_api_without_token_returns_401(self):
        app = Flask(__name__)
        app.jinja_env.globals['_'] = lambda s: s
        app.register_blueprint(veroscholar_bp)
        app.config['TESTING'] = True
        resp = app.test_client().get('/admin/veroscholar/api/v1/discovery/runs')
        self.assertEqual(resp.status_code, 401)


class TestDiscoveryDisabled(DiscoveryRouteBase):
    def test_trigger_disabled_returns_403(self):
        with mock.patch.object(rd, '_discovery_enabled', return_value=False):
            resp = self.client.post('/admin/veroscholar/api/v1/discovery/trigger',
                                    json={'question': 'Q'})
        self.assertEqual(resp.status_code, 403)

    def test_runs_disabled_returns_403(self):
        with mock.patch.object(rd, '_discovery_enabled', return_value=False):
            resp = self.client.get('/admin/veroscholar/api/v1/discovery/runs')
        self.assertEqual(resp.status_code, 403)

    def test_vote_disabled_returns_403(self):
        with mock.patch.object(rd, '_discovery_enabled', return_value=False):
            resp = self.client.post(
                '/admin/veroscholar/api/v1/discovery/hypotheses/%s/vote' % HYP_ID,
                json={'up': True})
        self.assertEqual(resp.status_code, 403)


class TestDiscoveryTrigger(DiscoveryRouteBase):
    def test_requires_question(self):
        resp = self.client.post('/admin/veroscholar/api/v1/discovery/trigger',
                                json={})
        self.assertEqual(resp.status_code, 400)

    def test_question_too_long(self):
        resp = self.client.post('/admin/veroscholar/api/v1/discovery/trigger',
                                json={'question': 'Q' * 301})
        self.assertEqual(resp.status_code, 400)

    def test_sync_mode_success(self):
        calls = {}

        def _create_run(conn, **kwargs):
            calls['kwargs'] = kwargs
            return RUN_ID

        self._patch(rd.md, 'create_run', _create_run)
        resp = self.client.post('/admin/veroscholar/api/v1/discovery/trigger',
                                json={'question': 'Q', 'mode': 'sync'})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()['data']
        self.assertEqual(data['mode'], 'sync')
        self.assertEqual(data['run_id'], RUN_ID)
        self.assertEqual(len(data['hypotheses']), 1)
        self.assertEqual(data['hypotheses'][0]['id'], HYP_ID)
        self.assertEqual(calls['kwargs']['trigger'], 'manual')
        self.assertEqual(calls['kwargs']['question'], 'Q')

    def test_sync_generate_failure_returns_500(self):
        self._patch(rd.nd, 'handle_hypo_gen',
                    lambda *a, **k: {'success': False,
                                     'error': 'LLM produced no hypotheses'})
        resp = self.client.post('/admin/veroscholar/api/v1/discovery/trigger',
                                json={'question': 'Q', 'mode': 'sync'})
        self.assertEqual(resp.status_code, 500)
        self.assertFalse(resp.get_json()['success'])

    def test_async_dag_path(self):
        engine = mock.MagicMock()
        engine.run_workflow.return_value = 42
        calls = {}

        def _create_run(conn, **kwargs):
            calls['trigger'] = kwargs.get('trigger')
            return RUN_ID

        self._patch(rd.md, 'create_run', _create_run)
        self._patch(rd, '_get_workflow_engine', lambda: engine)
        resp = self.client.post('/admin/veroscholar/api/v1/discovery/trigger',
                                json={'question': 'Q'})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()['data']
        self.assertEqual(data['mode'], 'dag')
        self.assertEqual(data['instance_id'], 42)
        self.assertEqual(data['run_id'], RUN_ID)
        self.assertEqual(calls['trigger'], 'dag')
        engine.run_workflow.assert_called_once()

    def test_async_unavailable_falls_back_to_background(self):
        # DE-5：引擎不可用时不再阻塞降级为 sync（会被网关 322s 超时截断），
        # 改为后台线程执行流水线并立即返回 run_id，前端轮询终态。
        started = {}

        def _fake_thread(target=None, **kwargs):
            started['target'] = target
            return mock.MagicMock()

        self._patch(rd, '_get_workflow_engine', lambda: None)
        self._patch(rd.threading, 'Thread', _fake_thread)
        resp = self.client.post('/admin/veroscholar/api/v1/discovery/trigger',
                                json={'question': 'Q'})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()['data']
        self.assertEqual(data['mode'], 'background')
        self.assertEqual(data['run_id'], RUN_ID)
        self.assertEqual(data['status'], 'running')
        self.assertIn('target', started, 'background worker not scheduled')
        # worker 立即执行时走完整流水线（节点全桩），不抛异常
        started['target']()

    def test_background_worker_failure_finishes_run_failed(self):
        # DE-5 回归：后台流水线内部失败时由 _run_pipeline 落 failed，
        # 不会让异常逃逸出线程（否则 run 永远停在 running）。
        self._patch(rd.nd, 'handle_hypo_gen',
                    lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
        result = rd._run_pipeline(RUN_ID, 'Q', None, 1)
        self.assertFalse(result['ok'])
        self.assertIn('boom', result['error'])


class TestDiscoveryRuns(DiscoveryRouteBase):
    def test_list_runs(self):
        resp = self.client.get('/admin/veroscholar/api/v1/discovery/runs')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()['data']
        self.assertEqual(data['total'], 3)
        self.assertEqual(len(data['runs']), 1)
        self.assertEqual(data['runs'][0]['id'], RUN_ID)

    def test_run_detail(self):
        resp = self.client.get(
            '/admin/veroscholar/api/v1/discovery/runs/%s' % RUN_ID)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()['data']
        self.assertEqual(data['question'], 'Q')
        self.assertEqual(len(data['events']), 1)

    def test_run_detail_not_found(self):
        self._patch(rd.md, 'get_run', lambda conn, rid: None)
        resp = self.client.get(
            '/admin/veroscholar/api/v1/discovery/runs/%s' % RUN_ID)
        self.assertEqual(resp.status_code, 404)

    def test_run_detail_invalid_uuid_returns_400(self):
        resp = self.client.get('/admin/veroscholar/api/v1/discovery/runs/nope')
        self.assertEqual(resp.status_code, 400)

    def test_hypotheses_parses_evidence_ids(self):
        resp = self.client.get(
            '/admin/veroscholar/api/v1/discovery/runs/%s/hypotheses' % RUN_ID)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()['data']
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['evidence_ids'], ['e1'])  # jsonb 已解析
        self.assertEqual(data[0]['discussions'][0]['protocol'], 'discussion')

    def test_discussions_groups_by_hypothesis(self):
        resp = self.client.get(
            '/admin/veroscholar/api/v1/discovery/runs/%s/discussions' % RUN_ID)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()['data']
        self.assertEqual(data[0]['hypothesis_id'], HYP_ID)
        self.assertEqual(len(data[0]['discussions']), 1)


class TestDiscoveryStatsVote(DiscoveryRouteBase):
    def test_stats(self):
        resp = self.client.get('/admin/veroscholar/api/v1/discovery/stats')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()['data']
        self.assertEqual(data['total_runs'], 3)
        self.assertEqual(data['avg_hypothesis_score'], 0.62)

    def test_vote_up_records_and_returns_ranking(self):
        resp = self.client.post(
            '/admin/veroscholar/api/v1/discovery/hypotheses/%s/vote' % HYP_ID,
            json={'up': True})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()['data']
        self.assertEqual(len(data), 2)

    def test_vote_hypothesis_not_found(self):
        self._patch(rd.md, 'get_hypothesis', lambda conn, hid: None)
        resp = self.client.post(
            '/admin/veroscholar/api/v1/discovery/hypotheses/%s/vote' % HYP_ID,
            json={'up': True})
        self.assertEqual(resp.status_code, 404)

    def test_vote_not_enough_peers(self):
        self._patch(rd.md, 'list_ranked', lambda conn, rid: [dict(HYPO)])
        resp = self.client.post(
            '/admin/veroscholar/api/v1/discovery/hypotheses/%s/vote' % HYP_ID,
            json={'up': True})
        self.assertEqual(resp.status_code, 400)

    def test_vote_invalid_uuid_returns_400(self):
        resp = self.client.post(
            '/admin/veroscholar/api/v1/discovery/hypotheses/nope/vote',
            json={'up': True})
        self.assertEqual(resp.status_code, 400)


class TestDiscoveryPage(DiscoveryRouteBase):
    def test_page_renders(self):
        resp = self.client.get('/admin/veroscholar/discovery')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Discovery Engine', resp.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
