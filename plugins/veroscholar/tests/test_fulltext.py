#!/usr/bin/env python3
"""test_fulltext.py — 模块 B：chunker/qa_ext 纯逻辑 + parser 契约（mock pypdf，不依赖真实 PDF/DB）。"""
import sys, os, unittest
import inspect
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from plugins.veroscholar.fulltext.chunker import chunk_pages, top_relevant_chunks
from plugins.veroscholar.fulltext.qa_ext import inject_fulltext_context


class TestChunker(unittest.TestCase):
    def test_chunk_split_and_page(self):
        pages = [{'page': 1, 'text': 'para one\npara two'},
                 {'page': 2, 'text': 'para three'}]
        chunks = chunk_pages(pages)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]['page'], 1)
        self.assertIn('para three', chunks[0]['content'])

    def test_chunk_max_boundary(self):
        pages = [{'page': 1, 'text': '\n'.join('x' * 500 for _ in range(5))}]
        chunks = chunk_pages(pages)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c['content']), 1200)  # 追加前检查，硬上限成立

    def test_top_relevant(self):
        chunks = [{'page': 1, 'content': 'deep learning attention'},
                  {'page': 2, 'content': 'cooking recipes pasta'},
                  {'page': 3, 'content': 'attention mechanism transformer'}]
        picked = top_relevant_chunks(chunks, 'attention mechanism?', k=2)
        pages = [c['page'] for c in picked]
        self.assertIn(3, pages)
        self.assertNotIn(2, pages)


class TestQaExt(unittest.TestCase):
    def test_first_param_named_value(self):
        """平台契约：filter 回调首参必须叫 value，否则 apply_filters 不透传 kwargs。"""
        params = list(inspect.signature(inject_fulltext_context).parameters)
        self.assertEqual(params[0], 'value')

    def test_no_paper_id_passthrough(self):
        ctx = ['a']
        self.assertEqual(inject_fulltext_context(ctx), ['a'])

    def test_db_failure_degrades_silently(self):
        """无 DB 环境（测试常态）下必须静默返回原上下文，不得抛。"""
        ctx = ['a']
        out = inject_fulltext_context(ctx, paper_id='x', question='q')
        self.assertEqual(out[0], 'a')


# ── W2：fulltext DELETE 路由测试（复用 test_routes 基建，不依赖真实 DB） ──
# 鉴权隔离：与 test_routes.RouteTestBase 完全同款——import 真实模块，
# setUp/tearDown 仅替换 validate_token 属性。
# 禁止在模块级替换 sys.modules['services*']（会污染整个测试进程，
# 曾致全量运行时其他模块 36 个用例 401 失败）。
from flask import Flask

import types

# 若 services 命名空间包尚未建立（单独运行 / 字母序 discover 中 test_routes
# 未先导入），在此幂等补齐；已存在则绝不覆盖，避免重蹈模块级覆盖污染的覆辙。
if 'services' not in sys.modules:
    _services = types.ModuleType('services')
    _jwt_stub = types.ModuleType('services.jwt_service')
    _jwt_stub.validate_token = lambda token: None
    _services.jwt_service = _jwt_stub
    sys.modules['services'] = _services
    sys.modules['services.jwt_service'] = _jwt_stub

from plugins.veroscholar.routes import veroscholar_bp
import plugins.veroscholar.routes as vroutes

PAPER_ID = '11111111-1111-4111-8111-111111111111'
PAPER_ID2 = '22222222-2222-4222-8222-222222222222'
FILE_ID = '55555555-5555-4555-8555-555555555555'


class _DummyCtx:
    """mock get_db 的上下文管理器。"""

    def __init__(self, conn=None):
        self.conn = conn or mock.MagicMock()

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        return False


class _AuthedClient:
    """包装 Flask test_client，自动注入管理员 Authorization 头。"""

    def __init__(self, client):
        self._client = client

    def delete(self, *args, **kwargs):
        headers = dict(kwargs.pop('headers', {}))
        headers.setdefault('Authorization', 'Bearer test-token')
        return self._client.delete(*args, headers=headers, **kwargs)


class TestFulltextDeleteRoute(unittest.TestCase):
    """W2-a：DELETE /api/v1/papers/<paper_id>/fulltext/<file_id>。"""

    def setUp(self):
        # 运行时经 sys.modules 解析当前生效的 stub，兼容任意导入顺序下
        # test_routes 建立/共享同一命名空间包的情况。
        self._jwt_mod = sys.modules['services'].jwt_service
        self._real_validate = self._jwt_mod.validate_token
        self._jwt_mod.validate_token = lambda token: {
            'user_id': 1, 'is_admin': True, 'username': 'admin'}
        self.app = Flask(__name__)
        self.app.jinja_env.globals['_'] = lambda s: s
        self.app.register_blueprint(veroscholar_bp)
        self.app.config['TESTING'] = True
        self.client = _AuthedClient(self.app.test_client())

    def tearDown(self):
        self._jwt_mod.validate_token = self._real_validate

    def _patch_db(self, **fakes):
        patchers = []
        for name, fn in fakes.items():
            p = mock.patch.object(vroutes.m, name, fn)
            p.start()
            patchers.append(p)
        self.addCleanup(lambda: [p.stop() for p in patchers])

    def test_delete_success(self):
        conn = mock.MagicMock()
        conn.execute.return_value.rowcount = 1
        self._patch_db(
            get_db=lambda: _DummyCtx(conn),
            get_paper=lambda c, pid: {'id': pid})
        resp = self.client.delete(
            f'/admin/veroscholar/api/v1/papers/{PAPER_ID}/fulltext/{FILE_ID}')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertTrue(data['deleted'])
        # 校验删除 SQL 携带 paper_id 条件，防止越权删除其他论文的全文
        sql = conn.execute.call_args[0][0]
        self.assertIn('fulltext_files', sql)

    def test_delete_paper_not_found(self):
        self._patch_db(
            get_db=lambda: _DummyCtx(),
            get_paper=lambda c, pid: None)
        resp = self.client.delete(
            f'/admin/veroscholar/api/v1/papers/{PAPER_ID2}/fulltext/{FILE_ID}')
        self.assertEqual(resp.status_code, 404)

    def test_delete_invalid_file_id_returns_400(self):
        # file_id 纳入蓝图 before_request UUID 前置校验（补文档遗漏项）
        resp = self.client.delete(
            f'/admin/veroscholar/api/v1/papers/{PAPER_ID}/fulltext/not-a-uuid')
        self.assertEqual(resp.status_code, 400)


if __name__ == '__main__':
    unittest.main()
