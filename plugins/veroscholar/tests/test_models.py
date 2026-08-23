#!/usr/bin/env python3
"""test_models.py — 数据层单元测试（FakeConn 模拟连接，不连数据库）。

运行:
    cd F:\\Sites\\VeroRun
    python -m unittest plugins.veroscholar.tests.test_models -v
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from plugins.veroscholar import models as m


class FakeRow(dict):
    pass


class FakeCursor:
    """execute 的返回值：可 fetchone / fetchall / 带 rowcount。"""

    def __init__(self, row=None, rows=None, rowcount=1):
        self._row = row
        self._rows = rows if rows is not None else []
        self.rowcount = rowcount

    def fetchone(self):
        if self._rows:
            return self._rows[0]
        return self._row

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class FakeConn:
    """按 SQL 关键字分发返回结果的假连接，记录调用供断言。"""

    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if 'SELECT id FROM papers WHERE doi' in sql:
            return FakeCursor(row=FakeRow(id='paper-1'))
        if 'SELECT id FROM papers WHERE title' in sql:
            return FakeCursor(row=FakeRow(id='paper-2'))
        if 'RETURNING id' in sql:
            return FakeCursor(row=FakeRow(id='proj-1'))
        if 'COUNT(*) AS c FROM papers' in sql:
            return FakeCursor(row=FakeRow(c=7))
        if 'COUNT(*) AS c FROM annotations' in sql:
            return FakeCursor(row=FakeRow(c=3))
        if 'COUNT(*) AS c FROM projects' in sql:
            return FakeCursor(row=FakeRow(c=2))
        if 'COUNT(*) AS c FROM search_logs' in sql:
            return FakeCursor(row=FakeRow(c=1))
        return FakeCursor(row=None)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class TestUpsertPaper(unittest.TestCase):
    def test_insert_with_doi_uses_on_conflict(self):
        conn = FakeConn()
        item = {
            'doi': '10.1/abc',
            'title': 'Test Paper',
            'authors': [{'name': 'A'}],
            'abstract': 'abs',
            'venue': 'J',
            'year': 2024,
            'citation_count': 5,
            'pdf_url': 'https://pdf',
            'source_db': 'openalex',
            'external_id': 'W1',
        }
        pid = m.upsert_paper(conn, item)
        self.assertEqual(pid, 'paper-1')
        insert_call = conn.calls[0]
        self.assertIn('ON CONFLICT (doi) DO UPDATE', insert_call[0])
        # params: doi, title, authors, abstract, venue, year, citation, pdf, source, external
        self.assertEqual(insert_call[1][0], '10.1/abc')
        self.assertEqual(insert_call[1][2], '[{"name": "A"}]')

    def test_without_doi_matches_by_title_year(self):
        conn = FakeConn()
        # 第一次 SELECT 命中已有 → 直接返回 id，不 INSERT
        pid = m.upsert_paper(conn, {'title': 'Existing', 'year': 2020})
        self.assertEqual(pid, 'paper-2')
        self.assertEqual(len([c for c in conn.calls if 'INSERT' in c[0]]), 0)


class TestProject(unittest.TestCase):
    def test_create_project_adds_creator_to_members(self):
        conn = FakeConn()
        pid = m.create_project(conn, 'Proj', 'desc', 99, [1, 2])
        self.assertEqual(pid, 'proj-1')
        sql, params = conn.calls[0]
        self.assertIn('INSERT INTO projects', sql)
        # team_members json 包含 created_by=99
        self.assertIn('99', params[3])

    def test_add_paper_to_project_returns_rowcount(self):
        conn = FakeConn()
        added = m.add_paper_to_project(conn, 'proj-1', 'paper-1')
        self.assertTrue(added)
        sql, params = conn.calls[0]
        self.assertIn('ON CONFLICT DO NOTHING', sql)
        self.assertEqual(params, ('proj-1', 'paper-1'))


class TestStats(unittest.TestCase):
    def test_get_stats_counts(self):
        conn = FakeConn()
        stats = m.get_stats(conn)
        self.assertEqual(stats['total_papers'], 7)
        self.assertEqual(stats['total_notes'], 3)
        self.assertEqual(stats['total_projects'], 2)
        self.assertEqual(stats['searches_24h'], 1)


class TestAnnotation(unittest.TestCase):
    def test_add_annotation_without_vector(self):
        conn = FakeConn()
        aid = m.add_annotation(conn, 'paper-1', 1, 'summary', 'note')
        self.assertEqual(aid, 'proj-1')  # RETURNING id 分发
        sql, params = conn.calls[0]
        self.assertIn('INSERT INTO annotations', sql)
        self.assertEqual(params[:4], ('paper-1', 1, 'summary', 'note'))

    def test_add_annotation_with_vector(self):
        conn = FakeConn()
        m.add_annotation(conn, 'paper-1', 1, 'summary', 'note', vec_literal='[0.1,0.2]')
        sql, params = conn.calls[0]
        self.assertIn('?::vector', sql)
        self.assertEqual(params[4], '[0.1,0.2]')


if __name__ == '__main__':
    unittest.main()
