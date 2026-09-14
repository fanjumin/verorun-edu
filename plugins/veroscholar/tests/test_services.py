#!/usr/bin/env python3
"""test_services.py — services/ai 层真实执行回归。

与 test_routes.py（在路由边界 mock 整个 ai 函数）不同，本模块真正执行
ai.chat_answer / translate / related_papers 的函数体，从而覆盖：

  1. 函数内延迟导入（services/ai.py 曾误写 `from . import models`，仅
     在调用期抛 ImportError —— 回归测试缺陷②，P0）
  2. LLM / embedding 调用链（_chat / embed_text）
  3. 数据层调用参数（get_paper / list_annotations / search_related_papers）

运行:
    cd F:\\Sites\\VeroRun
    python -m unittest plugins.veroscholar.tests.test_services -v
"""

import sys
import os
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from plugins.veroscholar import models
from plugins.veroscholar.services import ai


class _FakeConn:
    """占位连接：数据层函数全部被 mock，无需真实数据库。"""


class ChatAnswerTest(unittest.TestCase):
    """chat_answer：摘要 + 笔记 → LLM 生成。"""

    def setUp(self):
        self.conn = _FakeConn()
        self.paper = {'id': 'p1', 'title': 'Attention Is All You Need',
                      'abstract': 'We propose the Transformer.'}

    def test_chat_answer_grounds_on_abstract_and_notes(self):
        # 走到函数体即验证 `from .. import models` 导入正确（缺陷②回归点）
        with mock.patch.object(models, 'get_paper', return_value=self.paper), \
             mock.patch.object(models, 'list_annotations',
                               return_value=[{'content': 'note about x'}]), \
             mock.patch.object(ai, '_chat',
                               return_value='材料中未提及') as mchat:
            out = ai.chat_answer(self.conn, 'p1', 'what method?')
        self.assertEqual(out, '材料中未提及')
        msgs = mchat.call_args[0][0]
        self.assertIn('We propose the Transformer.', msgs[1]['content'])
        self.assertIn('note about x', msgs[1]['content'])

    def test_chat_answer_paper_not_found(self):
        with mock.patch.object(models, 'get_paper', return_value=None):
            with self.assertRaises(RuntimeError):
                ai.chat_answer(self.conn, 'p1', 'q')

    def test_chat_answer_no_material(self):
        with mock.patch.object(models, 'get_paper',
                               return_value={'id': 'p1', 'title': 'T',
                                             'abstract': '   '}), \
             mock.patch.object(models, 'list_annotations', return_value=[]):
            with self.assertRaises(RuntimeError):
                ai.chat_answer(self.conn, 'p1', 'q')


class TranslateTest(unittest.TestCase):
    """translate：摘要 → 目标语言。"""

    def setUp(self):
        self.conn = _FakeConn()
        self.paper = {'id': 'p1', 'title': 'T', 'abstract': 'An abstract here.'}

    def test_translate_ok(self):
        with mock.patch.object(models, 'get_paper', return_value=self.paper), \
             mock.patch.object(ai, '_chat',
                               return_value='摘要翻译结果') as mchat:
            out = ai.translate(self.conn, 'p1', 'zh')
        self.assertEqual(out, '摘要翻译结果')
        self.assertIn('An abstract here.', mchat.call_args[0][0][1]['content'])

    def test_translate_paper_not_found(self):
        with mock.patch.object(models, 'get_paper', return_value=None):
            with self.assertRaises(RuntimeError):
                ai.translate(self.conn, 'p1', 'zh')

    def test_translate_no_abstract(self):
        with mock.patch.object(models, 'get_paper',
                               return_value={'id': 'p1', 'title': 'T',
                                             'abstract': ''}):
            with self.assertRaises(RuntimeError):
                ai.translate(self.conn, 'p1', 'zh')


class RelatedPapersTest(unittest.TestCase):
    """related_papers：摘要向量 → vector_backend 分档检索（pgvector/local/keyword）。

    A3：桩目标从旧 models.search_related_papers 更新为 vector_backend——
    ai.related_papers 已重构走分档后端，旧桩不在调用路径上，
    导致真实 SQL 打到 _FakeConn（AttributeError，夹具过期）。
    """

    def setUp(self):
        self.conn = _FakeConn()
        self.paper = {'id': 'p1', 'title': 'Attention', 'abstract': '...'}

    def test_related_ok(self):
        from plugins.veroscholar.services import vector_backend
        related = [{'id': 'p2', 'title': 'R', 'similarity': 0.9}]
        with mock.patch.object(models, 'get_paper', return_value=self.paper), \
             mock.patch.object(ai, 'embed_text',
                               return_value='[0.1, 0.2]'), \
             mock.patch.object(vector_backend, 'update_paper_vector') as mupd, \
             mock.patch.object(vector_backend, 'related_papers',
                               return_value=related) as msearch:
            out = ai.related_papers(self.conn, 'p1', 5)
        self.assertEqual(out, related)
        mupd.assert_called_once_with(self.conn, 'p1', '[0.1, 0.2]')
        msearch.assert_called_once_with(self.conn, 'p1', '[0.1, 0.2]', 5)

    def test_related_paper_not_found(self):
        with mock.patch.object(models, 'get_paper', return_value=None):
            with self.assertRaises(RuntimeError):
                ai.related_papers(self.conn, 'p1', 5)

    def test_related_embedding_unavailable(self):
        # 桌面科研版约定：embedding 不可用不抛错，vec=None 交后端降档处理
        from plugins.veroscholar.services import vector_backend
        with mock.patch.object(models, 'get_paper', return_value=self.paper), \
             mock.patch.object(ai, 'embed_text', return_value=None), \
             mock.patch.object(vector_backend, 'related_papers',
                               return_value=[]) as mr:
            out = ai.related_papers(self.conn, 'p1', 5)
        self.assertEqual(out, [])
        mr.assert_called_once_with(self.conn, 'p1', None, 5)


class LlmTargetTest(unittest.TestCase):
    """_default_llm_target / _chat 透传：线上 503 根因修复的回归防护。

    根因：UnifiedLLM.chat 未传 provider/model 时 _resolve_model 必抛
    'Cannot resolve model'。修复后 _chat 必须显式透传平台默认模型。
    """

    def test_default_target_reads_system_config(self):
        rows = [{'key': 'ai_text_provider', 'value': 'deepseek'},
                {'key': 'ai_text_model', 'value': 'deepseek-v4-flash'}]
        fake_conn = mock.MagicMock()
        fake_conn.execute.return_value.fetchall.return_value = rows
        with mock.patch.object(models, 'get_db',
                               return_value=_ctx(fake_conn)):
            provider, model = ai._default_llm_target()
        self.assertEqual((provider, model), ('deepseek', 'deepseek-v4-flash'))

    def test_default_target_fallback_without_db(self):
        """DB 不可达（测试环境常态）→ 回退网关默认注册项，不得抛。"""
        provider, model = ai._default_llm_target()
        self.assertEqual((provider, model), ('deepseek', 'deepseek-chat'))

    def test_chat_passes_provider_and_model(self):
        fake_llm = mock.MagicMock()
        fake_llm.chat.return_value = 'answer'
        with mock.patch.object(ai, '_get_llm', return_value=fake_llm), \
             mock.patch.object(ai, '_default_llm_target',
                               return_value=('deepseek', 'deepseek-v4-flash')):
            out = ai._chat([{'role': 'user', 'content': 'hi'}])
        self.assertEqual(out, 'answer')
        kwargs = fake_llm.chat.call_args[1]
        self.assertEqual(kwargs.get('provider'), 'deepseek')
        self.assertEqual(kwargs.get('model'), 'deepseek-v4-flash')

    def test_chat_wraps_errors_as_runtimeerror(self):
        fake_llm = mock.MagicMock()
        fake_llm.chat.side_effect = ValueError('Cannot resolve model')
        with mock.patch.object(ai, '_get_llm', return_value=fake_llm), \
             mock.patch.object(ai, '_default_llm_target',
                               return_value=('deepseek', 'deepseek-chat')):
            with self.assertRaises(RuntimeError):
                ai._chat([{'role': 'user', 'content': 'hi'}])


def _ctx(fake_conn):
    """把假连接包成 with 语法可用的上下文。"""
    import contextlib

    @contextlib.contextmanager
    def _inner():
        yield fake_conn
    return _inner()


if __name__ == '__main__':
    unittest.main()
