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


#: sentinel：区分「用默认样例」与「显式返回 None（not found）」
_MISSING = object()

#: 合法 UUID 常量 — 路径参数须为 uuid 格式（routes 前置校验非法 → 400）
PAPER_ID = '11111111-1111-4111-8111-111111111111'
PAPER_ID2 = '22222222-2222-4222-8222-222222222222'   # 合法但不存在
REVIEW_ID = '33333333-3333-4333-8333-333333333333'
REVIEW_ID2 = '44444444-4444-4444-8444-444444444444'  # 合法但不存在


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
        resp = self.client.get(f'/admin/veroscholar/api/v1/papers/{PAPER_ID2}')
        self.assertEqual(resp.status_code, 404)

    def test_paper_detail_invalid_uuid_returns_400(self):
        resp = self.client.get('/admin/veroscholar/api/v1/papers/not-a-uuid')
        self.assertEqual(resp.status_code, 400)


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
            create_review=lambda conn, title, topic, structure, content, pids, uid, **kw: 'rev-1',
        )
        resp = self.client.post(
            '/admin/veroscholar/api/v1/reviews',
            json={'title': 'Survey', 'topic': 'Transformers'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['id'], 'rev-1')


class TestVerifyReviewEndpoint(RouteTestBase):
    def test_verify_review_success(self):
        self._patch_db(
            get_db=lambda: DummyCtx(),
            get_review=lambda conn, rid: {
                'id': 'rev-1', 'content': 'see 10.9999/nope',
                'paper_ids': '["p1", "gone"]'},
        )
        report = {
            'total_references': 1,
            'references': [
                {'doi': '10.9999/nope', 'registry_status': 'unregistered',
                 'notice_doi': None, 'in_library': False},
            ],
            'library_papers': 1,
            'missing_paper_ids': ['gone'],
        }
        with mock.patch.object(vroutes, 'verify_citations',
                               return_value=report) as mverify:
            resp = self.client.get(f'/admin/veroscholar/api/v1/reviews/{REVIEW_ID}/verify')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['data']['references'][0]['registry_status'], 'unregistered')
        self.assertEqual(data['data']['references'][0]['doi'], '10.9999/nope')
        mverify.assert_called_once()

    def test_verify_review_not_found(self):
        self._patch_db(
            get_db=lambda: DummyCtx(),
            get_review=lambda conn, rid: None,
        )
        resp = self.client.get(f'/admin/veroscholar/api/v1/reviews/{REVIEW_ID2}/verify')
        self.assertEqual(resp.status_code, 404)


class TestExportEndpoint(RouteTestBase):
    _paper = {
        'id': 'p1', 'doi': '10.1234/foo', 'title': 'Attention Is All You Need',
        'authors': '[{"name": "Vaswani, Ashish"}, {"name": "Shazeer, Noam"}]',
        'venue': 'NeurIPS', 'year': 2017, 'pdf_url': '',
        'source_db': 'semantic_scholar', 'external_id': 'x',
        'created_at': '2026-01-01',
    }

    def _patch_paper(self, paper=_MISSING):
        self._patch_db(
            get_db=lambda: DummyCtx(),
            get_paper=lambda conn, pid: paper if paper is not _MISSING else self._paper,
        )

    def test_export_bibtex(self):
        self._patch_paper()
        resp = self.client.get(f'/admin/veroscholar/api/v1/papers/{PAPER_ID}/export?format=bib')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'@article', resp.data)
        self.assertIn(b'10.1234/foo', resp.data)
        self.assertIn(b'Vaswani, Ashish', resp.data)
        self.assertEqual(resp.content_type, 'text/plain; charset=utf-8')

    def test_export_ris(self):
        self._patch_paper()
        resp = self.client.get(f'/admin/veroscholar/api/v1/papers/{PAPER_ID}/export?format=ris')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'TY  - JOUR', resp.data)
        self.assertIn(b'DO  - 10.1234/foo', resp.data)

    def test_export_default_format_is_bib(self):
        self._patch_paper()
        resp = self.client.get(f'/admin/veroscholar/api/v1/papers/{PAPER_ID}/export')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'@article', resp.data)

    def test_export_invalid_format(self):
        self._patch_paper()
        resp = self.client.get(f'/admin/veroscholar/api/v1/papers/{PAPER_ID}/export?format=docx')
        self.assertEqual(resp.status_code, 400)

    def test_export_not_found(self):
        self._patch_paper(None)
        resp = self.client.get(f'/admin/veroscholar/api/v1/papers/{PAPER_ID2}/export')
        self.assertEqual(resp.status_code, 404)

    def test_export_paper_without_title(self):
        self._patch_paper({k: (v if k != 'title' else '') for k, v in self._paper.items()})
        resp = self.client.get(f'/admin/veroscholar/api/v1/papers/{PAPER_ID}/export?format=bib')
        self.assertEqual(resp.status_code, 422)


class TestPaperAIFeatures(RouteTestBase):
    def test_chat_returns_answer(self):
        self._patch_db(get_db=lambda: DummyCtx())
        with mock.patch.object(vroutes.ai, 'chat_answer',
                               return_value='本文采用 Transformer 架构') as mchat:
            resp = self.client.post(
                f'/admin/veroscholar/api/v1/papers/{PAPER_ID}/chat',
                json={'question': '本文方法是什么?'})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['data']['answer'], '本文采用 Transformer 架构')
        mchat.assert_called_once()

    def test_chat_requires_question(self):
        resp = self.client.post(
            f'/admin/veroscholar/api/v1/papers/{PAPER_ID}/chat', json={})
        self.assertEqual(resp.status_code, 400)

    def test_chat_llm_unavailable(self):
        self._patch_db(get_db=lambda: DummyCtx())
        with mock.patch.object(vroutes.ai, 'chat_answer',
                               side_effect=RuntimeError('LLM unavailable')):
            resp = self.client.post(
                f'/admin/veroscholar/api/v1/papers/{PAPER_ID}/chat',
                json={'question': 'q'})
        self.assertEqual(resp.status_code, 503)

    def test_translate_returns_translation(self):
        self._patch_db(get_db=lambda: DummyCtx())
        with mock.patch.object(vroutes.ai, 'translate',
                               return_value='这是一篇关于注意力的论文') as mtr:
            resp = self.client.post(
                f'/admin/veroscholar/api/v1/papers/{PAPER_ID}/translate',
                json={'target_lang': 'zh'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['data']['translation'],
                         '这是一篇关于注意力的论文')
        mtr.assert_called_once()

    def test_translate_unavailable(self):
        self._patch_db(get_db=lambda: DummyCtx())
        with mock.patch.object(vroutes.ai, 'translate',
                               side_effect=RuntimeError('LLM unavailable')):
            resp = self.client.post(
                f'/admin/veroscholar/api/v1/papers/{PAPER_ID}/translate', json={})
        self.assertEqual(resp.status_code, 503)

    def test_related_returns_papers(self):
        related = [{'id': 'p2', 'title': 'Related Work', 'similarity': 0.9}]
        self._patch_db(get_db=lambda: DummyCtx())
        with mock.patch.object(vroutes.ai, 'related_papers',
                               return_value=related) as mrel:
            resp = self.client.get(
                f'/admin/veroscholar/api/v1/papers/{PAPER_ID}/related?limit=5')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['data'][0]['id'], 'p2')
        mrel.assert_called_once()


class TestLibraryTagsAndStatus(RouteTestBase):
    """标签系统 + 阅读状态 + 库筛选端点。"""

    _tags = [{'id': 1, 'name': 'AI'}, {'id': 2, 'name': 'NLP'}]

    def test_paper_tags_list(self):
        self._patch_db(
            get_db=lambda: DummyCtx(),
            get_paper=lambda conn, pid: {'id': pid},
            paper_tags=lambda conn, pid: self._tags)
        resp = self.client.get(f'/admin/veroscholar/api/v1/papers/{PAPER_ID}/tags')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['data'][0]['name'], 'AI')

    def test_paper_tags_not_found(self):
        self._patch_db(
            get_db=lambda: DummyCtx(),
            get_paper=lambda conn, pid: None)
        resp = self.client.get(f'/admin/veroscholar/api/v1/papers/{PAPER_ID2}/tags')
        self.assertEqual(resp.status_code, 404)

    def test_add_tag(self):
        self._patch_db(
            get_db=lambda: DummyCtx(),
            get_paper=lambda conn, pid: {'id': pid},
            add_tag=lambda conn, pid, name: 1,
            paper_tags=lambda conn, pid: self._tags)
        resp = self.client.post(
            f'/admin/veroscholar/api/v1/papers/{PAPER_ID}/tags', json={'name': 'AI'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()['data']), 2)

    def test_add_tag_requires_name(self):
        resp = self.client.post(
            f'/admin/veroscholar/api/v1/papers/{PAPER_ID}/tags', json={})
        self.assertEqual(resp.status_code, 400)

    def test_add_tag_too_long(self):
        resp = self.client.post(
            f'/admin/veroscholar/api/v1/papers/{PAPER_ID}/tags',
            json={'name': 'x' * 65})
        self.assertEqual(resp.status_code, 400)

    def test_remove_tag(self):
        self._patch_db(
            get_db=lambda: DummyCtx(),
            get_paper=lambda conn, pid: {'id': pid},
            remove_tag=lambda conn, pid, tid: None,
            paper_tags=lambda conn, pid: self._tags[:1])
        resp = self.client.delete(
            f'/admin/veroscholar/api/v1/papers/{PAPER_ID}/tags/1')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()['data']), 1)

    def test_set_status(self):
        self._patch_db(
            get_db=lambda: DummyCtx(),
            get_paper=lambda conn, pid: {'id': pid},
            set_reading_status=lambda conn, pid, s: True)
        resp = self.client.patch(
            f'/admin/veroscholar/api/v1/papers/{PAPER_ID}/status',
            json={'status': 'reading'})
        self.assertEqual(resp.status_code, 200)

    def test_set_status_invalid(self):
        self._patch_db(
            get_db=lambda: DummyCtx(),
            get_paper=lambda conn, pid: {'id': pid},
            set_reading_status=lambda conn, pid, s: False)
        resp = self.client.patch(
            f'/admin/veroscholar/api/v1/papers/{PAPER_ID}/status',
            json={'status': 'whatever'})
        self.assertEqual(resp.status_code, 400)

    def test_list_tags(self):
        self._patch_db(get_db=lambda: DummyCtx(),
                       list_tags=lambda conn: self._tags)
        resp = self.client.get('/admin/veroscholar/api/v1/tags')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()['data']), 2)

    def test_list_papers_passes_tag_and_status(self):
        captured = {}

        def fake_list(conn, limit, offset, source_db, year, q, tag_id,
                      status):
            captured.update(tag_id=tag_id, status=status)
            return []

        self._patch_db(
            get_db=lambda: DummyCtx(),
            list_papers=fake_list,
            count_papers=lambda conn, *a, **k: 0)
        resp = self.client.get(
            '/admin/veroscholar/api/v1/papers?tag=3&status=reading')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured['tag_id'], '3')
        self.assertEqual(captured['status'], 'reading')


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
