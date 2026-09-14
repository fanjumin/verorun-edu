#!/usr/bin/env python3
"""test_discovery_models.py — Discovery 数据层单元测试（FakeConn 模拟，不连数据库）。

运行:
    cd F:\\Sites\\VeroRun
    python -m unittest plugins.veroscholar.tests.test_discovery_models -v
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from plugins.veroscholar.discovery import models_discovery as md


class FakeRow(dict):
    pass


class FakeCursor:
    def __init__(self, row=None, rows=None, rowcount=1, description=None):
        self._row = row
        self._rows = rows if rows is not None else []
        self.rowcount = rowcount
        self.description = description

    def fetchone(self):
        if self._rows:
            return self._rows[0]
        return self._row

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class FakeConn:
    """按 SQL 关键字分发的假连接，记录调用供断言。"""

    def __init__(self):
        self.calls = []
        self.inserted = {}
        self.recent_running = False  # 60 秒内同人同问 running 去重开关
        self.last_hypo = None

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if 'INSERT INTO discovery_runs' in sql:
            rid = params[0]
            self.inserted.setdefault('run', rid)
            return FakeCursor(row=FakeRow(id=rid))
        if 'AND status = ?' in sql:
            if self.recent_running:
                return FakeCursor(row=FakeRow(id='run-dup'))
            return FakeCursor(row=None)
        if "WHERE status = 'running'" in sql:
            return FakeCursor(row=FakeRow(c=1))
        if 'FROM discovery_runs' in sql and 'WHERE user_id' in sql \
                and 'COUNT(*)' not in sql:
            return FakeCursor(rows=[
                FakeRow(id='run-1', question='Q', status='completed',
                        trigger='manual', created_at='2026-09-01',
                        finished_at='2026-09-01')])
        if 'FROM discovery_runs' in sql and 'COUNT(*)' in sql:
            return FakeCursor(row=FakeRow(c=3))
        if 'FROM discovery_runs' in sql and 'WHERE id = ?' in sql:
            return FakeCursor(row=FakeRow(
                id='run-1', question='Q', project_id=None, user_id=1,
                trigger='manual', status='completed', config='{}',
                error='', created_at='2026-09-01', finished_at='2026-09-01'))
        if 'FROM discovery_runs' in sql and 'LIMIT' in sql:
            return FakeCursor(row=FakeRow(
                id='run-1', question='Q', project_id=None, user_id=1,
                trigger='manual', status='completed', config='{}',
                error='', created_at='2026-09-01', finished_at='2026-09-01'))
        if 'INSERT INTO hypotheses' in sql:
            hid = params[0]
            self.inserted.setdefault('hypo', hid)
            self.last_hypo = {
                'id': hid, 'run_id': params[1], 'statement': params[2],
                'score': params[3], 'evidence_ids': params[4],
                'cluster_id': params[5], 'current_status': params[6],
                'elo_rating': params[7],
                'created_at': '2026-09-01', 'updated_at': '2026-09-01'}
            return FakeCursor(row=None)
        if 'SELECT * FROM hypotheses WHERE id = ?' in sql:
            return FakeCursor(row=FakeRow(
                id=self.last_hypo['id'] if self.last_hypo else 'hypo-1',
                run_id='run-1',
                statement=self.last_hypo['statement'] if self.last_hypo else 'S',
                score=0.8, evidence_ids='["e1"]', cluster_id='c1',
                current_status='pending', elo_rating=1000.0,
                created_at='2026-09-01', updated_at='2026-09-01'))
        if 'SELECT * FROM hypotheses WHERE run_id = ?' in sql:
            return FakeCursor(rows=[
                FakeRow(id='hypo-2', run_id='run-1', statement='S2', score=0.5,
                        evidence_ids='[]', cluster_id='', current_status='pending',
                        elo_rating=1100.0, created_at='2026-09-01',
                        updated_at='2026-09-01'),
                FakeRow(id='hypo-1', run_id='run-1', statement='S1', score=0.8,
                        evidence_ids='["e1"]', cluster_id='c1',
                        current_status='pending', elo_rating=1050.0,
                        created_at='2026-09-01', updated_at='2026-09-01')])
        if 'UPDATE hypotheses SET' in sql:
            return FakeCursor(row=None)
        if 'SELECT id FROM hypothesis_votes' in sql:
            return FakeCursor(row=None)
        if 'INSERT INTO hypothesis_votes' in sql:
            return FakeCursor(row=None)
        if 'INSERT INTO hypothesis_discussions' in sql:
            return FakeCursor(row=None)
        if 'FROM hypothesis_discussions' in sql:
            return FakeCursor(rows=[
                FakeRow(protocol='discussion', question='Q', response='R',
                        meta='{}', created_at='2026-09-01')])
        if 'INSERT INTO discovery_events' in sql:
            return FakeCursor(row=None)
        if 'FROM discovery_events' in sql:
            return FakeCursor(rows=[
                FakeRow(stage='generate', status='info', detail='{}',
                        created_at='2026-09-01')])
        if 'INSERT INTO discovery_memory' in sql:
            return FakeCursor(row=None)
        if 'FROM discovery_memory' in sql:
            return FakeCursor(rows=[
                FakeRow(memory_type='best_hypothesis', content='{"x": 1}',
                        updated_at='2026-09-01')])
        if 'COUNT(*) AS c FROM discovery_runs' in sql:
            return FakeCursor(row=FakeRow(c=4))
        if "WHERE status = 'running'" in sql:
            return FakeCursor(row=FakeRow(c=1))
        if 'COUNT(*) AS c FROM hypotheses' in sql:
            return FakeCursor(row=FakeRow(c=9))
        if 'AVG(score)' in sql:
            return FakeCursor(row=FakeRow(c=0.62))
        return FakeCursor(row=None)

    def commit(self):
        pass

    def rollback(self):
        pass


class TestCreateRun(unittest.TestCase):
    def test_create_run_inserts_and_returns_uuid(self):
        conn = FakeConn()
        rid = md.create_run(conn, question='What causes X?', user_id=1)
        self.assertEqual(rid, conn.inserted.get('run'))
        insert = [c for c in conn.calls if c[0].startswith('INSERT INTO discovery_runs')]
        self.assertEqual(len(insert), 1)
        self.assertEqual(insert[0][1][1], 'What causes X?')

    def test_create_run_raises_on_empty_question(self):
        conn = FakeConn()
        with self.assertRaises(ValueError):
            md.create_run(conn, question='   ', user_id=1)

    def test_create_run_reuses_recent_running(self):
        conn = FakeConn()
        conn.recent_running = True
        rid = md.create_run(conn, question='Q', user_id=1)
        self.assertEqual(rid, 'run-dup')  # 命中 60 秒内 running 去重
        inserts = [c for c in conn.calls if c[0].startswith('INSERT INTO discovery_runs')]
        self.assertEqual(len(inserts), 0)


class TestRunQueries(unittest.TestCase):
    def test_finish_run(self):
        conn = FakeConn()
        md.finish_run(conn, 'run-1', status='completed')
        update = [c for c in conn.calls if c[0].startswith('UPDATE discovery_runs')]
        self.assertEqual(update[0][1][0], 'completed')
        self.assertEqual(update[0][1][2], 'run-1')

    def test_list_runs(self):
        conn = FakeConn()
        runs = md.list_runs(conn, 1, limit=5)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]['id'], 'run-1')
        self.assertEqual(runs[0]['status'], 'completed')

    def test_get_run(self):
        conn = FakeConn()
        run = md.get_run(conn, 'run-1')
        self.assertEqual(run['question'], 'Q')

    def test_count_runs(self):
        conn = FakeConn()
        self.assertEqual(md.count_runs(conn, 1), 3)


class TestHypotheses(unittest.TestCase):
    def test_create_hypotheses_batch(self):
        conn = FakeConn()
        out = md.create_hypotheses(conn, 'run-1', [{'statement': 'S1', 'score': 0.9}])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['id'], conn.inserted.get('hypo'))
        self.assertEqual(out[0]['statement'], 'S1')

    def test_list_hypotheses_sorted_by_elo(self):
        conn = FakeConn()
        hypos = md.list_hypotheses(conn, 'run-1')
        self.assertEqual(len(hypos), 2)
        # 假连接按插入序返回，断言字段可用即可
        self.assertEqual(hypos[0]['id'], 'hypo-2')

    def test_update_hypothesis_whitelist(self):
        conn = FakeConn()
        md.update_hypothesis(conn, 'hypo-1', score=0.9)
        with self.assertRaises(ValueError):
            md.update_hypothesis(conn, 'hypo-1', evil_column='x')

    def test_get_hypothesis(self):
        conn = FakeConn()
        h = md.get_hypothesis(conn, 'hypo-1')
        self.assertEqual(h['statement'], 'S')


class TestVotes(unittest.TestCase):
    def test_record_vote_updates_elo(self):
        conn = FakeConn()
        md.record_vote(conn, run_id='run-1', winner_id='hypo-1',
                       loser_id='hypo-2')
        updates = [c for c in conn.calls if c[0].startswith('UPDATE hypotheses SET')]
        self.assertEqual(len(updates), 2)

    def test_record_vote_dedup(self):
        conn = FakeConn()

        class _DupCursor(FakeCursor):
            pass

        # 第二次调用命中去重查询
        state = {'dup': False}

        def _execute(sql, params=None):
            conn.calls.append((sql, params))
            if 'SELECT id FROM hypothesis_votes' in sql:
                if state['dup']:
                    return FakeCursor(row=FakeRow(id='v1'))
                state['dup'] = True
                return FakeCursor(row=None)
            if 'INSERT INTO hypothesis_votes' in sql:
                return FakeCursor(row=None)
            if 'SELECT * FROM hypotheses WHERE id = ?' in sql:
                return FakeCursor(row=FakeRow(
                    id=params[0], run_id='run-1', statement='S', score=0.5,
                    evidence_ids='[]', cluster_id='', current_status='pending',
                    elo_rating=1000.0, created_at='2026-09-01',
                    updated_at='2026-09-01'))
            if 'UPDATE hypotheses SET' in sql:
                return FakeCursor(row=None)
            return FakeCursor(row=None)

        conn.execute = _execute
        md.record_vote(conn, run_id='run-1', winner_id='hypo-1', loser_id='hypo-2')
        md.record_vote(conn, run_id='run-1', winner_id='hypo-1', loser_id='hypo-2')
        inserts = [c for c in conn.calls
                   if c[0].startswith('INSERT INTO hypothesis_votes')]
        self.assertEqual(len(inserts), 1)

    def test_record_vote_audit_only_skips_elo_update(self):
        """N2 回归：update_elo=False 仅写审计行（voter='engine'），不重放计分。"""
        conn = FakeConn()
        md.record_vote(conn, run_id='run-1', winner_id='hypo-1',
                       loser_id='hypo-2', voter='engine', update_elo=False)
        inserts = [c for c in conn.calls
                   if c[0].startswith('INSERT INTO hypothesis_votes')]
        updates = [c for c in conn.calls if c[0].startswith('UPDATE hypotheses SET')]
        self.assertEqual(len(inserts), 1)   # 审计对局行保留
        self.assertEqual(len(updates), 0)   # 不触发 Elo 重算

    def test_engine_audit_row_does_not_swallow_manual_vote(self):
        """N6 回归：引擎对局留痕(voter='engine')后，同对局人工票(voter='user:1')
        不得被去重早退吞掉 —— 去重域按 voter 划分，人工票照常插行并更新 Elo。"""
        conn = FakeConn()
        # 按 (winner, loser, voter) 记录已存在行，仿真 UNIQUE(run,winner,loser,voter)
        rows = set()

        def _execute(sql, params=None):
            conn.calls.append((sql, params))
            if 'SELECT id FROM hypothesis_votes' in sql:
                key = (params[1], params[2], params[3])
                return FakeCursor(row=FakeRow(id='v1') if key in rows else None)
            if 'INSERT INTO hypothesis_votes' in sql:
                rows.add((params[2], params[3], params[4]))
                return FakeCursor(row=None)
            if 'SELECT * FROM hypotheses WHERE id = ?' in sql:
                return FakeCursor(row=FakeRow(
                    id=params[0], run_id='run-1', statement='S', score=0.5,
                    evidence_ids='[]', cluster_id='', current_status='pending',
                    elo_rating=1000.0, created_at='2026-09-01',
                    updated_at='2026-09-01'))
            if 'UPDATE hypotheses SET' in sql:
                return FakeCursor(row=None)
            return FakeCursor(row=None)

        conn.execute = _execute
        # ① 引擎审计留痕（不重放计分）
        md.record_vote(conn, run_id='run-1', winner_id='hypo-1', loser_id='hypo-2',
                       voter='engine', update_elo=False)
        # ② 同对局人工票 —— 修前在去重处早退（1 insert / 0 update），修后照常生效
        md.record_vote(conn, run_id='run-1', winner_id='hypo-1', loser_id='hypo-2',
                       voter='user:1')
        inserts = [c for c in conn.calls
                   if c[0].startswith('INSERT INTO hypothesis_votes')]
        updates = [c for c in conn.calls if c[0].startswith('UPDATE hypotheses SET')]
        self.assertEqual(len(inserts), 2)   # 引擎审计行 + 人工票行并存
        self.assertEqual(len(updates), 2)   # 人工票真正触发 Elo 更新，未被吞


class TestDiscussionsEventsMemory(unittest.TestCase):
    def test_add_and_list_discussions(self):
        conn = FakeConn()
        md.add_discussion(conn, hypothesis_id='hypo-1', protocol='discussion',
                          question='Q', response='R')
        rows = md.list_discussions(conn, 'hypo-1')
        self.assertEqual(rows[0]['protocol'], 'discussion')

    def test_add_and_list_events(self):
        conn = FakeConn()
        md.add_event(conn, run_id='run-1', stage='generate')
        rows = md.list_events(conn, 'run-1')
        self.assertEqual(rows[0]['stage'], 'generate')

    def test_save_and_list_memory(self):
        conn = FakeConn()
        md.save_memory(conn, run_id='run-1', memory_type='best_hypothesis',
                       content='{"x": 1}')
        rows = md.list_memory(conn, 'run-1')
        self.assertEqual(rows[0]['memory_type'], 'best_hypothesis')


class TestStatsAndUtils(unittest.TestCase):
    def test_get_stats(self):
        conn = FakeConn()
        s = md.get_stats(conn)
        self.assertEqual(s['total_runs'], 3)
        self.assertEqual(s['running_runs'], 1)
        self.assertEqual(s['total_hypotheses'], 9)
        self.assertEqual(s['avg_hypothesis_score'], 0.62)

    def test_row_dict(self):
        self.assertEqual(md._row_dict({'a': 1}), {'a': 1})
        self.assertEqual(md._row_dict(FakeRow(a=1)), {'a': 1})
        self.assertEqual(md._row_dict(None), {})

    def test_to_epoch(self):
        from datetime import datetime
        dt = datetime(2026, 1, 1)
        self.assertEqual(md.to_epoch(dt), dt.timestamp())
        self.assertEqual(md.to_epoch('x'), 'x')


if __name__ == '__main__':
    unittest.main()
