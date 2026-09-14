#!/usr/bin/env python3
"""test_discovery_nodes.py — Discovery DAG 节点处理器单元测试。

运行:
    cd F:\\Sites\\VeroRun
    python -m unittest plugins.veroscholar.tests.test_discovery_nodes -v
"""

import sys
import os
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from plugins.veroscholar.discovery import nodes as nd


class DummyCtx:
    def __init__(self, conn=None):
        self.conn = conn or mock.MagicMock()

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        return False


def _ctx(**fields):
    return {'context': fields}


class TestCollectUpstream(unittest.TestCase):
    def test_direct_context_field(self):
        val = nd._collect_upstream({'context': {'question': 'Q'}}, 'question')
        self.assertEqual(val, 'Q')

    def test_from_node_output(self):
        input_data = {'context': {'node_n1_output': {'candidate_hypotheses': ['a']}}}
        val = nd._collect_upstream(input_data, 'candidate_hypotheses')
        self.assertEqual(val, ['a'])

    def test_missing_returns_none(self):
        self.assertIsNone(nd._collect_upstream({'context': {}}, 'nope'))


class TestHypoGen(unittest.TestCase):
    def test_disabled_returns_failure(self):
        with mock.patch.object(nd, '_discovery_enabled', return_value=False):
            out = nd.handle_hypo_gen({'config': {}}, _ctx(question='Q'))
        self.assertFalse(out['success'])

    def test_missing_question(self):
        with mock.patch.object(nd, '_discovery_enabled', return_value=True):
            out = nd.handle_hypo_gen({'config': {}}, _ctx())
        self.assertFalse(out['success'])

    def test_success(self):
        hypos = [{'statement': 'H1', 'score': 0.8}]
        with mock.patch.object(nd, '_discovery_enabled', return_value=True), \
                mock.patch.object(nd.ds, 'generate_hypotheses',
                                  return_value=hypos) as mg:
            out = nd.handle_hypo_gen({'config': {}}, _ctx(question='Q'))
        self.assertTrue(out['success'])
        self.assertEqual(out['candidate_hypotheses'], hypos)
        mg.assert_called_once_with('Q', n=5)

    def test_llm_empty_returns_failure(self):
        with mock.patch.object(nd, '_discovery_enabled', return_value=True), \
                mock.patch.object(nd.ds, 'generate_hypotheses', return_value=[]):
            out = nd.handle_hypo_gen({'config': {}}, _ctx(question='Q'))
        self.assertFalse(out['success'])


class TestHypoEvidence(unittest.TestCase):
    def test_missing_question(self):
        with mock.patch.object(nd, '_discovery_enabled', return_value=True):
            out = nd.handle_hypo_evidence({'config': {}}, _ctx())
        self.assertFalse(out['success'])

    def test_search_results_in_pool(self):
        paper = {'doi': '10.1/x', 'title': 'T', 'year': 2024, 'source': 'arxiv'}
        with mock.patch.object(nd, '_discovery_enabled', return_value=True), \
                mock.patch('plugins.veroscholar.workflow.run_multi_source_search',
                           return_value=([paper], {})) as ms, \
                mock.patch.object(nd.m, 'get_db', side_effect=Exception('no db')):
            out = nd.handle_hypo_evidence({'config': {}}, _ctx(question='Q'))
        self.assertTrue(out['success'])
        self.assertGreaterEqual(out['count'], 1)
        self.assertEqual(out['evidence_pool'][0]['type'], 'paper')
        ms.assert_called_once()


class TestHypoRank(unittest.TestCase):
    def test_no_upstream(self):
        out = nd.handle_hypo_rank({'config': {}}, _ctx())
        self.assertFalse(out['success'])

    def test_success(self):
        hypos = [{'statement': 'H1'}, {'statement': 'H2'}]
        with mock.patch.object(nd.ds, 'elo_tournament',
                               return_value=hypos) as mt:
            out = nd.handle_hypo_rank(
                {'config': {}}, _ctx(question='Q', candidate_hypotheses=hypos))
        self.assertTrue(out['success'])
        self.assertEqual(out['count'], 2)
        mt.assert_called_once()


class TestHypoCluster(unittest.TestCase):
    def test_no_upstream(self):
        out = nd.handle_hypo_cluster({'config': {}}, _ctx())
        self.assertFalse(out['success'])

    def test_groups_by_keyword(self):
        hypos = [
            {'statement': 'Transformer attention scales', 'id': 'a'},
            {'statement': 'Transformer deep scaling', 'id': 'b'},
            {'statement': 'Photonic crystal biosensors', 'id': 'c'},
        ]
        out = nd.handle_hypo_cluster({'config': {}},
                                     _ctx(ranked_hypotheses=hypos))
        self.assertTrue(out['success'])
        # 'transformer' 与 'photonic' 两组（max-word 聚类）
        self.assertEqual(out['cluster_count'], 2)
        total = sum(c['size'] for c in out['clusters'])
        self.assertEqual(total, 3)


class TestHypoDiscuss(unittest.TestCase):
    def test_no_upstream(self):
        out = nd.handle_hypo_discuss({'config': {}}, _ctx())
        self.assertFalse(out['success'])

    def test_success(self):
        hypos = [{'statement': 'H1', 'id': 'a'}]
        with mock.patch.object(nd.ds, 'generate_hypotheses',
                               return_value=[{'statement': 'G1', 'score': 0.5}]), \
                mock.patch.object(nd.ds, 'critique_hypothesis',
                                  return_value={'critique': 'C',
                                                'weaknesses': ['w']}), \
                mock.patch.object(nd.ds, 'evolve_hypothesis',
                                  return_value={'statement': 'E1',
                                                'rationale': 'r'}):
            out = nd.handle_hypo_discuss(
                {'config': {}}, _ctx(ranked_hypotheses=hypos))
        self.assertTrue(out['success'])
        self.assertEqual(out['round_count'], 3)  # 每假设 3 轮


class TestHypoJudge(unittest.TestCase):
    def test_no_upstream(self):
        out = nd.handle_hypo_judge({'config': {}}, _ctx())
        self.assertFalse(out['success'])

    def test_success(self):
        hypos = [{'statement': 'H1', 'id': 'a', 'score': 0.5, 'elo_rating': 1040.0}]
        with mock.patch.object(nd, '_judge_one',
                               return_value={'score': 0.9, 'verdict': 'strong',
                                             'rationale': 'r'}):
            out = nd.handle_hypo_judge({'config': {}},
                                       _ctx(ranked_hypotheses=hypos))
        self.assertTrue(out['success'])
        self.assertEqual(out['judged_hypotheses'][0]['score'], 0.9)
        # N1：judge 透传锦标赛 Elo，persist 落库即真实排名
        self.assertEqual(out['judged_hypotheses'][0]['elo_rating'], 1040.0)


class TestHypoPersist(unittest.TestCase):
    def test_missing_question(self):
        out = nd.handle_hypo_persist({'config': {}}, _ctx())
        self.assertFalse(out['success'])

    def test_success_with_existing_run(self):
        conn = mock.MagicMock()
        conn.execute.side_effect = lambda *a, **k: mock.MagicMock()
        with mock.patch.object(nd.m, 'get_db', return_value=DummyCtx(conn)), \
                mock.patch.object(nd.md, 'create_hypotheses',
                                  return_value=[{'id': 'h1', 'statement': 'S'}]) as mc, \
                mock.patch.object(nd.md, 'add_event'), \
                mock.patch.object(nd.md, 'add_discussion'), \
                mock.patch.object(nd.md, 'save_memory'), \
                mock.patch.object(nd.md, 'finish_run') as mf:
            out = nd.handle_hypo_persist(
                {'config': {}},
                _ctx(question='Q', run_id='run-1', user_id=1,
                     judged_hypotheses=[{'id': 'h1', 'statement': 'S',
                                         'score': 0.9}]))
        self.assertTrue(out['success'])
        self.assertEqual(out['run_id'], 'run-1')
        self.assertEqual(out['hypotheses'], 1)
        mf.assert_called_once()
        mc.assert_called_once()

    def test_create_run_when_missing(self):
        conn = mock.MagicMock()
        conn.execute.side_effect = lambda *a, **k: mock.MagicMock()
        with mock.patch.object(nd.m, 'get_db', return_value=DummyCtx(conn)), \
                mock.patch.object(nd.md, 'create_run',
                                  return_value='new-run') as cr, \
                mock.patch.object(nd.md, 'create_hypotheses',
                                  return_value=[{'id': 'h1', 'statement': 'S'}]), \
                mock.patch.object(nd.md, 'add_event'), \
                mock.patch.object(nd.md, 'add_discussion'), \
                mock.patch.object(nd.md, 'save_memory'), \
                mock.patch.object(nd.md, 'finish_run'):
            out = nd.handle_hypo_persist(
                {'config': {}}, _ctx(question='Q', user_id=1,
                                     ranked_hypotheses=[{'statement': 'S'}]))
        self.assertTrue(out['success'])
        self.assertEqual(out['run_id'], 'new-run')
        cr.assert_called_once()

    def test_discussion_written_with_real_id_not_cross_product(self):
        """P0-1 回归：讨论须绑定已落库假设的 id，且按归属写 N×1（非 N×M 交叉积）。"""
        conn = mock.MagicMock()
        discussions = []

        def _add_discussion(conn, *, hypothesis_id, protocol, question, response,
                            meta=None):
            discussions.append({'id': hypothesis_id, 'round': meta.get('round')})

        # 2 条假设入库；rounds 仅属假设 A（2 轮）—— 交叉积应为 2×2=4，正确 N×1 应为 2
        with mock.patch.object(nd.m, 'get_db', return_value=DummyCtx(conn)), \
                mock.patch.object(nd.md, 'create_hypotheses', return_value=[
                    {'id': 'hA', 'statement': 'A'},
                    {'id': 'hB', 'statement': 'B'}]), \
                mock.patch.object(nd.md, 'add_event'), \
                mock.patch.object(nd.md, 'add_discussion',
                                  side_effect=_add_discussion), \
                mock.patch.object(nd.md, 'save_memory'), \
                mock.patch.object(nd.md, 'finish_run'):
            out = nd.handle_hypo_persist(
                {'config': {}},
                _ctx(question='Q', run_id='run-1', user_id=1,
                     judged_hypotheses=[{'statement': 'A'}, {'statement': 'B'}],
                     rounds=[
                         {'hypothesis': 'A', 'round': 1, 'generate': 'g1'},
                         {'hypothesis': 'A', 'round': 2, 'generate': 'g2'},
                     ]))
        self.assertTrue(out['success'])
        self.assertEqual(len(discussions), 2)          # N×1，非 2×2
        for d in discussions:
            self.assertEqual(d['id'], 'hA')            # 绑定真实假设 id，非 None
        self.assertEqual({d['round'] for d in discussions}, {1, 2})

    def test_persist_stores_judge_elo_with_audit_only_votes(self):
        """N1+N2 回归：judge 透传 elo 落库；persist 对局仅审计留痕（update_elo=False），
        不重放计分 —— 杜绝锦标赛 Elo 与 DB 重算双重叠加。"""
        conn = mock.MagicMock()
        conn.execute.side_effect = lambda *a, **k: mock.MagicMock()
        seen = {}
        votes = []

        def _create(conn, run_id, hps):
            seen['elo'] = [h.get('elo_rating') for h in hps]
            return [{'id': 'r1', 'statement': 'H1'},
                    {'id': 'r2', 'statement': 'H2'}]

        def _record_vote(conn, *, run_id, winner_id, loser_id, voter='engine',
                         update_elo=True):
            votes.append({'winner_id': winner_id, 'loser_id': loser_id,
                          'voter': voter, 'update_elo': update_elo})

        with mock.patch.object(nd.m, 'get_db', return_value=DummyCtx(conn)), \
                mock.patch.object(nd.md, 'create_hypotheses', side_effect=_create), \
                mock.patch.object(nd.md, 'add_event'), \
                mock.patch.object(nd.md, 'add_discussion'), \
                mock.patch.object(nd.md, 'record_vote', side_effect=_record_vote), \
                mock.patch.object(nd.md, 'save_memory'), \
                mock.patch.object(nd.md, 'finish_run'):
            out = nd.handle_hypo_persist(
                {'config': {}},
                _ctx(question='Q', run_id='run-1', user_id=1,
                     judged_hypotheses=[
                         {'statement': 'H1', 'score': 0.9, 'elo_rating': 1030.5},
                         {'statement': 'H2', 'score': 0.7, 'elo_rating': 969.5}],
                     matches=[{'winner': 'H1', 'loser': 'H2'}]))
        self.assertTrue(out['success'])
        self.assertEqual(seen['elo'], [1030.5, 969.5])  # 锦标赛 Elo 全量透传落库
        self.assertEqual(len(votes), 1)                  # 对局留痕 1 条
        self.assertEqual(votes[0]['winner_id'], 'r1')
        self.assertEqual(votes[0]['loser_id'], 'r2')
        self.assertEqual(votes[0]['voter'], 'engine')
        self.assertFalse(votes[0]['update_elo'])         # 审计化，不重放计分

    def test_long_statement_discussion_not_lost(self):
        """N4 回归：statement 超 2000 字符时，落库截断与 rounds 归属键须同口径，
        讨论记录不得静默丢失。"""
        conn = mock.MagicMock()
        long_a = 'A' * 2100
        discussions = []

        def _create(conn, run_id, hps):
            # 仿真真实 md.create_hypotheses 的落库截断（strip + 2000）
            return [{'id': 'hA',
                     'statement': (hps[0].get('statement') or '').strip()[:2000]}]

        def _add_discussion(conn, *, hypothesis_id, protocol, question, response,
                            meta=None):
            discussions.append({'id': hypothesis_id, 'round': meta.get('round')})

        with mock.patch.object(nd.m, 'get_db', return_value=DummyCtx(conn)), \
                mock.patch.object(nd.md, 'create_hypotheses', side_effect=_create), \
                mock.patch.object(nd.md, 'add_event'), \
                mock.patch.object(nd.md, 'add_discussion',
                                  side_effect=_add_discussion), \
                mock.patch.object(nd.md, 'save_memory'), \
                mock.patch.object(nd.md, 'finish_run'):
            out = nd.handle_hypo_persist(
                {'config': {}},
                _ctx(question='Q', run_id='run-1', user_id=1,
                     judged_hypotheses=[{'statement': long_a, 'score': 0.8}],
                     rounds=[{'hypothesis': long_a, 'round': 1,
                              'generate': 'g1', 'critique': 'c',
                              'evolved': 'e'}]))
        self.assertTrue(out['success'])
        self.assertEqual(len(discussions), 1)            # 不再静默丢失
        self.assertEqual(discussions[0]['id'], 'hA')     # 命中截断后落库行
        self.assertEqual(discussions[0]['round'], 1)

    def test_empty_hypotheses_marks_failed_not_false_success(self):
        """DE-6 语义更新（原 P1-2 回归）：空假设 persist 不再假报 completed ——
        返回失败、置 run=failed、透出原因，且不抛 NameError。"""
        finished = {}

        def _finish(conn, run_id, status='completed', error=None):
            finished['status'] = status
            finished['error'] = error

        with mock.patch.object(nd.m, 'get_db',
                               return_value=DummyCtx(mock.MagicMock())), \
                mock.patch.object(nd.md, 'create_hypotheses', return_value=[]), \
                mock.patch.object(nd.md, 'add_event'), \
                mock.patch.object(nd.md, 'add_discussion'), \
                mock.patch.object(nd.md, 'save_memory'), \
                mock.patch.object(nd.md, 'update_run_metrics'), \
                mock.patch.object(nd.md, 'finish_run', side_effect=_finish):
            out = nd.handle_hypo_persist(
                {'config': {}}, _ctx(question='Q', run_id='run-1', user_id=1))
        self.assertFalse(out['success'])
        self.assertEqual(out['run_id'], 'run-1')
        self.assertEqual(finished['status'], 'failed')
        self.assertIn('no hypotheses produced', finished['error'])

    def test_empty_hypotheses_reports_upstream_errors(self):
        """DE-6 回归（DAG 形态）：上游节点 success=False 且零产物时，persist 置
        run=failed 并把各上游失败原因拼进 error，避免静默假成功。"""
        finished = {}

        def _finish(conn, run_id, status='completed', error=None):
            finished['status'] = status
            finished['error'] = error

        with mock.patch.object(nd.m, 'get_db',
                               return_value=DummyCtx(mock.MagicMock())), \
                mock.patch.object(nd.md, 'create_hypotheses', return_value=[]), \
                mock.patch.object(nd.md, 'add_event'), \
                mock.patch.object(nd.md, 'add_discussion'), \
                mock.patch.object(nd.md, 'save_memory'), \
                mock.patch.object(nd.md, 'update_run_metrics'), \
                mock.patch.object(nd.md, 'finish_run', side_effect=_finish):
            out = nd.handle_hypo_persist(
                {'config': {}},
                {'context': {
                    'question': 'Q', 'run_id': 'run-1', 'user_id': 1,
                    'node_n1_output': {'success': False,
                                       'error': 'LLM produced no hypotheses',
                                       'warning': 'timeout'},
                    'node_n3_output': {'success': False,
                                       'error': 'no candidate hypotheses upstream'}}})
        self.assertFalse(out['success'])
        self.assertIn('no hypotheses produced', out['error'])
        self.assertIn('node_n1_output: LLM produced no hypotheses', out['error'])
        self.assertEqual(finished['status'], 'failed')

    def test_cluster_assignments_written_back(self):
        """DE-7 回归：persist 消费 cluster 节点的 cluster_assignments，把 cluster_id
        写回假设，create_hypotheses 收到的记录含归属簇（sync/DAG 双路径共用此逻辑）。"""
        conn = mock.MagicMock()
        seen = {}

        def _create(conn, run_id, hps):
            seen['cluster_ids'] = [h.get('cluster_id') for h in hps]
            return [{'id': 'h1', 'statement': 'Transformer attention scales'},
                    {'id': 'h2', 'statement': 'Transformer deep scaling'}]

        with mock.patch.object(nd.m, 'get_db', return_value=DummyCtx(conn)), \
                mock.patch.object(nd.md, 'create_hypotheses', side_effect=_create), \
                mock.patch.object(nd.md, 'add_event'), \
                mock.patch.object(nd.md, 'add_discussion'), \
                mock.patch.object(nd.md, 'save_memory'), \
                mock.patch.object(nd.md, 'update_run_metrics'), \
                mock.patch.object(nd.md, 'finish_run'):
            out = nd.handle_hypo_persist(
                {'config': {}},
                _ctx(question='Q', run_id='run-1', user_id=1,
                     judged_hypotheses=[
                         {'statement': 'Transformer attention scales',
                          'score': 0.9},
                         {'statement': 'Transformer deep scaling', 'score': 0.8}],
                     cluster_assignments={
                         'Transformer attention scales': 'transfor',
                         'Transformer deep scaling': 'transfor'}))
        self.assertTrue(out['success'])
        self.assertEqual(seen['cluster_ids'], ['transfor', 'transfor'])

    def test_persist_records_llm_calls_metric(self):
        """DE-6 回归：persist 统一把结构指标 + llm_calls 写入 run metrics
        （DAG 与 sync 一致；此前 DAG 路径从不记录 llm_calls）。"""
        conn = mock.MagicMock()
        metrics_calls = []

        def _update_metrics(conn, run_id, metrics):
            metrics_calls.append(dict(metrics))

        with mock.patch.object(nd.m, 'get_db', return_value=DummyCtx(conn)), \
                mock.patch.object(nd.md, 'create_hypotheses',
                                  return_value=[{'id': 'h1', 'statement': 'S'}]), \
                mock.patch.object(nd.md, 'add_event'), \
                mock.patch.object(nd.md, 'add_discussion'), \
                mock.patch.object(nd.md, 'save_memory'), \
                mock.patch.object(nd.md, 'update_run_metrics',
                                  side_effect=_update_metrics), \
                mock.patch.object(nd.md, 'finish_run'):
            out = nd.handle_hypo_persist(
                {'config': {}},
                _ctx(question='Q', run_id='run-1', user_id=1,
                     judged_hypotheses=[{'statement': 'S', 'score': 0.9}]))
        self.assertTrue(out['success'])
        self.assertEqual(len(metrics_calls), 1)
        self.assertEqual(metrics_calls[0]['hypotheses'], 1)
        self.assertIn('llm_calls', metrics_calls[0])


class TestBlueprint(unittest.TestCase):
    def test_default_blueprint_has_7_nodes(self):
        bp = nd._default_hypothesis_blueprint()
        self.assertEqual(len(bp['nodes']), 7)
        self.assertEqual(len(bp['edges']), 6)

    def test_get_or_create_workflow_reuses_existing(self):
        conn = mock.MagicMock()
        conn.execute.return_value.fetchone.return_value = {'id': 42}
        wid = nd._get_or_create_hypothesis_workflow(conn)
        self.assertEqual(wid, 42)
        sqls = [c[0] for c in conn.execute.call_args_list]
        self.assertNotIn('INSERT INTO workflow_definitions', sqls)

    def test_get_or_create_workflow_insert_uses_chained_fetchone(self):
        """P0-2 回归：INSERT 分支链式 conn.execute(...).fetchone()（PgConnection 无独立 fetchone）。"""
        conn = mock.MagicMock()
        seen = {}

        def _execute(sql, params=None):
            if sql.startswith('SELECT'):
                cur = mock.MagicMock()
                cur.fetchone.return_value = None   # 未命中 → 进入 INSERT
                return cur
            cur = mock.MagicMock()
            cur.fetchone.return_value = {'id': 99}
            seen['insert'] = True
            return cur

        conn.execute.side_effect = _execute
        wid = nd._get_or_create_hypothesis_workflow(conn)
        self.assertEqual(wid, 99)
        self.assertTrue(seen.get('insert'))

    def test_judge_one_uses_ds_review_independent_score(self):
        """P2-7 回归：_judge_one 走 ds.review_hypothesis 独立评分，非自我 baseline 虚高。"""
        with mock.patch.object(nd.ds, 'review_hypothesis', return_value={
                'score': 0.6, 'rationale': 'strong'}):
            out = nd._judge_one({'statement': 'H', 'score': 0.5}, [])
        self.assertNotEqual(out['verdict'], 'Judge unavailable')
        self.assertEqual(out['verdict'], 'strong')
        self.assertEqual(out['score'], 0.6)


if __name__ == '__main__':
    unittest.main()
