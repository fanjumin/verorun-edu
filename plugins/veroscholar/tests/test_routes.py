#!/usr/bin/env python3
"""test_routes.py — 路由集成测试（Flask test client + mock 鉴权与数据层）。

运行:
    cd F:\\Sites\\VeroRun
    python -m unittest plugins.veroscholar.tests.test_routes -v
"""

import sys
import os
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from flask import Flask
from plugins.veroscholar.routes import veroscholar_bp
import plugins.veroscholar.routes as vroutes


# ── 预置 fake services.jwt_service（测试隔离） ──
# 生产代码中 routes.check_auth 惰性执行 `from services.jwt_service import validate_token`。
# 真实模块位于 auth-center/services/jwt_service.py，导入即读取 JWT_SECRET 环境变量
# 并依赖 PyJWT。测试用命名空间包 stub 替代，仅需可被替换的 validate_token。
import types

_services = types.ModuleType('services')
_jwt_stub = types.ModuleType('services.jwt_service')
_jwt_stub.validate_token = lambda token: None
_services.jwt_service = _jwt_stub
sys.modules['services'] = _services
sys.modules['services.jwt_service'] = _jwt_stub
services = sys.modules['services']
del types, _services, _jwt_stub


class DummyCtx:
    """mock get_db 的上下文管理器。"""

    def __init__(self, conn=None):
        self.conn = conn or mock.MagicMock()

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        return False


class AuthedClient:
    """包装 Flask test_client，自动注入管理员 Authorization 头。

    routes.check_auth 仅在请求含 token 时才调用 validate_token，
    因此 mock 前必须让每个请求携带 token。用包装类实现与 Flask
    版本无关（旧版 test_client 不接受 cls / environ_base 参数）。
    直接委托同名方法，避免把 URL 误传为 werkzeug 的 base_url。
    """

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

    def put(self, *args, **kwargs):
        return self._client.put(*args, headers=self._headers(kwargs), **kwargs)

    def delete(self, *args, **kwargs):
        return self._client.delete(*args, headers=self._headers(kwargs), **kwargs)

    def patch(self, *args, **kwargs):
        return self._client.patch(*args, headers=self._headers(kwargs), **kwargs)


class RouteTestBase(unittest.TestCase):
    def setUp(self):
        # 1) 鉴权：让 validate_token 永远返回管理员 payload
        self._real_validate = services.jwt_service.validate_token
        services.jwt_service.validate_token = lambda token: {
            'user_id': 1, 'is_admin': True, 'username': 'admin'}

        # 2) Flask app + blueprint
        self.app = Flask(__name__)
        self.app.jinja_env.globals['_'] = lambda s: s
        self.app.register_blueprint(veroscholar_bp)
        self.app.config['TESTING'] = True
        self.client = AuthedClient(self.app.test_client())

    def tearDown(self):
        services.jwt_service.validate_token = self._real_validate

    def _patch_db(self, **fakes):
        """把 routes 引用的 models 函数替换为假实现。"""
        patchers = []
        for name, fn in fakes.items():
            p = mock.patch.object(vroutes.m, name, fn)
            p.start()
            patchers.append(p)
        self.addCleanup(lambda: [p.stop() for p in patchers])
        return patchers


class TestAuth(unittest.TestCase):
    def test_api_without_token_returns_401(self):
        app = Flask(__name__)
        app.jinja_env.globals['_'] = lambda s: s
        app.register_blueprint(veroscholar_bp)
        app.config['TESTING'] = True
        client = app.test_client()
        resp = client.get('/admin/veroscholar/api/v1/stats')
        self.assertEqual(resp.status_code, 401)

    def test_page_without_token_redirects_to_login(self):
        app = Flask(__name__)
        app.jinja_env.globals['_'] = lambda s: s
        app.register_blueprint(veroscholar_bp)
        app.config['TESTING'] = True
        client = app.test_client()
        resp = client.get('/admin/veroscholar/dashboard')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/admin/login', resp.headers.get('Location', ''))

    def test_static_is_exempt(self):
        app = Flask(__name__)
        app.jinja_env.globals['_'] = lambda s: s
        app.register_blueprint(veroscholar_bp)
        app.config['TESTING'] = True
        client = app.test_client()
        # 不存在的静态文件也走免鉴权，返回 404 而非 401/302
        resp = client.get('/admin/veroscholar/static/js/does-not-exist.js')
        self.assertEqual(resp.status_code, 404)


class TestStatsEndpoint(RouteTestBase):
    def test_stats(self):
        self._patch_db(
            get_db=lambda: DummyCtx(),
            get_stats=lambda conn: {
                'total_papers': 10, 'total_notes': 2,
                'total_projects': 1, 'searches_24h': 4},
        )
        resp = self.client.get('/admin/veroscholar/api/v1/stats')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['data']['total_papers'], 10)


class TestPapersEndpoint(RouteTestBase):
    def test_list_papers(self):
        fake_paper = {
            'id': 'p1', 'doi': '10.1/a', 'title': 'T', 'authors': '[{"name": "A"}]',
            'abstract': 'abs', 'venue': 'J', 'year': 2024,
            'citation_count': 3, 'pdf_url': '', 'source_db': 'arxiv',
            'external_id': '123', 'created_at': '2026-01-01',
        }
        self._patch_db(
            get_db=lambda: DummyCtx(),
            list_papers=lambda conn, *a, **k: [fake_paper],
            count_papers=lambda conn, *a, **k: 1,
        )
        resp = self.client.get('/admin/veroscholar/api/v1/papers?q=attention')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['total'], 1)
        # authors jsonb 被解析为数组
        self.assertEqual(data['data'][0]['authors'], [{'name': 'A'}])

    def test_paper_detail_not_found(self):
        self._patch_db(
            get_db=lambda: DummyCtx(),
            get_paper=lambda conn, pid: None,
        )
        resp = self.client.get('/admin/veroscholar/api/v1/papers/nope')
        self.assertEqual(resp.status_code, 404)


class TestSearchEndpoint(RouteTestBase):
    def test_search_requires_keywords(self):
        resp = self.client.post('/admin/veroscholar/api/v1/search', json={'keywords': ''})
        self.assertEqual(resp.status_code, 400)

    def test_search_calls_multi_source(self):
        with mock.patch.object(vroutes, 'run_multi_source_search',
                               return_value=([{'doi': '10.1/x'}], {})) as msearch:
            resp = self.client.post(
                '/admin/veroscholar/api/v1/search',
                json={'keywords': 'attention', 'sources': ['arxiv'], 'limit': 10})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['count'], 1)
        msearch.assert_called_once()


class TestProjectsEndpoint(RouteTestBase):
    def test_create_project(self):
        self._patch_db(
            get_db=lambda: DummyCtx(),
            create_project=lambda conn, name, desc, uid: 'proj-1',
        )
        resp = self.client.post(
            '/admin/veroscholar/api/v1/projects',
            json={'name': 'NLP Research', 'description': 'd'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['id'], 'proj-1')

    def test_create_project_requires_name(self):
        resp = self.client.post('/admin/veroscholar/api/v1/projects', json={})
        self.assertEqual(resp.status_code, 400)


class TestReviewEndpoint(RouteTestBase):
    def test_create_review(self):
        self._patch_db(
            get_db=lambda: DummyCtx(),
            create_review=lambda conn, title, topic, structure, content, pids, uid: 'rev-1',
        )
        resp = self.client.post(
            '/admin/veroscholar/api/v1/reviews',
            json={'title': 'Survey', 'topic': 'Transformers'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['id'], 'rev-1')


class TestPages(RouteTestBase):
    def test_dashboard_page(self):
        self._patch_db(
            get_db=lambda: DummyCtx(),
            get_stats=lambda conn: {'total_papers': 0, 'total_notes': 0,
                                    'total_projects': 0, 'searches_24h': 0},
            list_projects=lambda conn, **k: [],
            list_papers=lambda conn, *a, **k: [],
            count_papers=lambda conn, *a, **k: 0,
        )
        resp = self.client.get('/admin/veroscholar/dashboard')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'VeroScholar', resp.data)

    def test_search_page(self):
        resp = self.client.get('/admin/veroscholar/search')
        self.assertEqual(resp.status_code, 200)

    def test_review_page(self):
        resp = self.client.get('/admin/veroscholar/review')
        self.assertEqual(resp.status_code, 200)


if __name__ == '__main__':
    unittest.main()
