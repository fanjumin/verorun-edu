#!/usr/bin/env python3
"""VeroScholar 数据层 — 独立 schema 连接管理 + 迁移。

遵循插件标准 §9.1 / §11.2（v1.5.1 连接生命周期强制规范）：
  - 通过 plugins/_base/db.py 的 get_pooled_connection() 从共享连接池借取连接
  - 设置 search_path TO veroscholar, public 后使用
  - 退出即归还池（池内自动 rollback + 重置 search_path TO public）
  - 严禁在模块级/线程局部缓存连接指针（曾导致 PG 连接数打满）
"""

import json
import logging
import os
from contextlib import contextmanager

from plugins._base.db import get_pooled_connection

SCHEMA = 'veroscholar'

logger = logging.getLogger('veroscholar')


@contextmanager
def get_db():
    """从共享池借取连接，并设置 search_path = veroscholar。

    用法:
        with get_db() as conn:
            conn.execute("SELECT ...")
            conn.commit()
    退出时自动归还池（含 rollback + 重置 search_path）。
    """
    conn = get_pooled_connection()
    try:
        conn.execute("SET search_path TO %s, public" % SCHEMA)
        yield conn
    finally:
        try:
            conn.close()
        except Exception as e:
            logger.warning('veroscholar conn close failed: %s', e)


def get_schema_version() -> str:
    """读取当前 schema 版本；无记录返回 0.0.0。"""
    with get_db() as conn:
        try:
            row = conn.execute(
                "SELECT version FROM schema_version"
                " ORDER BY applied_at DESC LIMIT 1"
            ).fetchone()
            return row['version'] if row else '0.0.0'
        except Exception:
            return '0.0.0'


def migrate(from_version: str, to_version: str) -> bool:
    """幂等执行 migrations/*.sql，每个文件记录到 schema_version。

    执行顺序：先建 schema + schema_version 表，再逐个执行未应用的文件；
    单文件失败整体回滚（同一事务）。
    """
    with get_db() as conn:
        try:
            conn.execute("CREATE SCHEMA IF NOT EXISTS %s" % SCHEMA)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                " version varchar(64) PRIMARY KEY,"
                " applied_at timestamptz NOT NULL DEFAULT now())"
            )
            # 兼容旧库版本列过窄的情况（varchar(16) 容不下长文件名）
            conn.execute(
                "ALTER TABLE IF EXISTS schema_version"
                " ALTER COLUMN version TYPE varchar(64)"
            )
            conn.execute("SET search_path TO %s, public" % SCHEMA)

            migrations_dir = os.path.join(os.path.dirname(__file__), 'migrations')
            for fname in sorted(os.listdir(migrations_dir)):
                if not fname.endswith('.sql'):
                    continue
                applied = conn.execute(
                    "SELECT 1 FROM schema_version WHERE version = ?", (fname,)
                ).fetchone()
                if applied:
                    continue
                fpath = os.path.join(migrations_dir, fname)
                with open(fpath, 'r', encoding='utf-8') as f:
                    conn.execute(f.read())
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (fname,)
                )
                logger.info('migration applied: %s', fname)
            conn.commit()
            return True
        except Exception as e:
            logger.error('migration failed: %s', e)
            conn.rollback()
            return False


# ══════════════════════════════════════════════════════════════════
# 论文
# ══════════════════════════════════════════════════════════════════

def upsert_paper(conn, item: dict) -> str:
    """按 DOI 幂等写入论文，返回论文 id。

    无 DOI 时回退为「标题+年份」查重；重复时更新引用数等信息。
    """
    doi = (item.get('doi') or '').strip()
    authors = json.dumps(item.get('authors') or [], ensure_ascii=False)
    title = (item.get('title') or '').strip()
    year = item.get('year')

    if doi:
        conn.execute(
            """INSERT INTO papers
               (doi, title, authors, abstract, venue, year, citation_count,
                pdf_url, source_db, external_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (doi) DO UPDATE SET
                 title = EXCLUDED.title,
                 authors = EXCLUDED.authors,
                 abstract = EXCLUDED.abstract,
                 venue = EXCLUDED.venue,
                 year = EXCLUDED.year,
                 citation_count = EXCLUDED.citation_count,
                 pdf_url = EXCLUDED.pdf_url,
                 source_db = EXCLUDED.source_db,
                 external_id = EXCLUDED.external_id,
                 updated_at = now()""",
            (doi, title, authors, item.get('abstract') or '', item.get('venue') or '',
             year, int(item.get('citation_count') or 0), item.get('pdf_url') or '',
             item.get('source_db') or '', item.get('external_id') or ''))
        row = conn.execute("SELECT id FROM papers WHERE doi = ?", (doi,)).fetchone()
        return row['id']

    # 无 DOI 兜底：标题+年份
    row = conn.execute(
        "SELECT id FROM papers WHERE title = ?"
        " AND year IS NOT DISTINCT FROM ? LIMIT 1", (title, year)).fetchone()
    if row:
        return row['id']
    conn.execute(
        """INSERT INTO papers
           (title, authors, abstract, venue, year, citation_count, pdf_url, source_db, external_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (title, authors, item.get('abstract') or '', item.get('venue') or '',
         year, int(item.get('citation_count') or 0), item.get('pdf_url') or '',
         item.get('source_db') or '', item.get('external_id') or ''))
    row = conn.execute(
        "SELECT id FROM papers WHERE title = ?"
        " AND year IS NOT DISTINCT FROM ? LIMIT 1", (title, year)).fetchone()
    return row['id']


def get_paper(conn, paper_id: str) -> dict:
    """按 id 获取论文。"""
    row = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
    return dict(row) if row else None


def get_paper_by_doi(conn, doi: str) -> dict:
    """按 DOI 获取论文。"""
    row = conn.execute("SELECT * FROM papers WHERE doi = ?", (doi,)).fetchone()
    return dict(row) if row else None


def list_papers(conn, limit=50, offset=0, source_db='', year=None, q=''):
    """论文库列表：支持数据源 / 年份 / 关键词筛选，按被引数降序。"""
    sql = (
        "SELECT id, doi, title, authors, abstract, venue, year,"
        " citation_count, pdf_url, source_db, external_id, created_at"
        " FROM papers WHERE 1 = 1"
    )
    args = []
    if source_db:
        sql += " AND source_db = ?"
        args.append(source_db)
    if year:
        sql += " AND year = ?"
        args.append(int(year))
    if q:
        sql += " AND (title ILIKE ? OR abstract ILIKE ?)"
        like = f'%{q}%'
        args += [like, like]
    sql += " ORDER BY citation_count DESC NULLS LAST, year DESC NULLS LAST"
    sql += " LIMIT ? OFFSET ?"
    args += [int(limit), int(offset)]
    return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]


def count_papers(conn, source_db='', year=None, q='') -> int:
    """论文总数（与 list_papers 同条件）。"""
    sql = "SELECT COUNT(*) AS c FROM papers WHERE 1 = 1"
    args = []
    if source_db:
        sql += " AND source_db = ?"
        args.append(source_db)
    if year:
        sql += " AND year = ?"
        args.append(int(year))
    if q:
        sql += " AND (title ILIKE ? OR abstract ILIKE ?)"
        like = f'%{q}%'
        args += [like, like]
    row = conn.execute(sql, tuple(args)).fetchone()
    return int(row['c'])


# ══════════════════════════════════════════════════════════════════
# 项目
# ══════════════════════════════════════════════════════════════════

def create_project(conn, name: str, description: str, created_by,
                   team_members=None) -> str:
    """创建研究项目，返回项目 id。"""
    members = team_members or []
    if created_by and created_by not in members:
        members.append(created_by)
    row = conn.execute(
        """INSERT INTO projects (name, description, created_by, team_members)
           VALUES (?, ?, ?, ?) RETURNING id""",
        (name, description or '', created_by,
         json.dumps(members, ensure_ascii=False))).fetchone()
    return row['id']


def list_projects(conn, limit=100):
    """列出所有项目（教育版单管理端，无需按成员过滤）。"""
    sql = (
        "SELECT id, name, description, status, team_members, created_by, created_at"
        " FROM projects ORDER BY created_at DESC LIMIT ?"
    )
    return [dict(r) for r in conn.execute(sql, (int(limit),)).fetchall()]


def get_project(conn, project_id: str) -> dict:
    """按 id 获取项目。"""
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return dict(row) if row else None


def add_paper_to_project(conn, project_id: str, paper_id: str) -> bool:
    """把论文加入项目；已存在则忽略。返回是否新增。"""
    cur = conn.execute(
        "INSERT INTO project_papers (project_id, paper_id)"
        " VALUES (?, ?) ON CONFLICT DO NOTHING",
        (project_id, paper_id))
    return cur.rowcount > 0


def list_project_papers(conn, project_id: str):
    """列出项目内的全部论文。"""
    sql = (
        "SELECT p.id, p.doi, p.title, p.authors, p.abstract, p.venue, p.year,"
        " p.citation_count, p.pdf_url, p.source_db, pp.added_at"
        " FROM papers p JOIN project_papers pp ON pp.paper_id = p.id"
        " WHERE pp.project_id = ? ORDER BY pp.added_at DESC"
    )
    return [dict(r) for r in conn.execute(sql, (project_id,)).fetchall()]


# ══════════════════════════════════════════════════════════════════
# 笔记 / 批注
# ══════════════════════════════════════════════════════════════════

def add_annotation(conn, paper_id: str, user_id, note_type: str,
                   content: str, vec_literal=None) -> str:
    """添加论文笔记；vec_literal 为 '[0.1,0.2,...]' 字符串或 None。"""
    if vec_literal:
        row = conn.execute(
            """INSERT INTO annotations (paper_id, user_id, note_type, content, embedding)
               VALUES (?, ?, ?, ?, ?::vector) RETURNING id""",
            (paper_id, user_id, note_type, content, vec_literal)).fetchone()
    else:
        row = conn.execute(
            """INSERT INTO annotations (paper_id, user_id, note_type, content)
               VALUES (?, ?, ?, ?) RETURNING id""",
            (paper_id, user_id, note_type, content)).fetchone()
    return row['id']


def list_annotations(conn, paper_id: str):
    """列出某篇论文的全部笔记。"""
    sql = (
        "SELECT id, paper_id, user_id, note_type, content, created_at"
        " FROM annotations WHERE paper_id = ? ORDER BY created_at DESC"
    )
    return [dict(r) for r in conn.execute(sql, (paper_id,)).fetchall()]


# ══════════════════════════════════════════════════════════════════
# 综述
# ══════════════════════════════════════════════════════════════════

def create_review(conn, title: str, topic: str, structure, content: str,
                  paper_ids, created_by) -> str:
    """创建综述文档，返回 id。"""
    row = conn.execute(
        """INSERT INTO reviews (title, topic, structure, content, paper_ids, created_by)
           VALUES (?, ?, ?, ?, ?, ?) RETURNING id""",
        (title, topic or '', json.dumps(structure or {}, ensure_ascii=False),
         content or '', json.dumps(paper_ids or [], ensure_ascii=False), created_by)).fetchone()
    return row['id']


def get_review(conn, review_id: str) -> dict:
    """按 id 获取综述。"""
    row = conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
    return dict(row) if row else None


def list_reviews(conn, created_by=None, limit=50):
    """列出综述列表；created_by 为 None 时列出全部。"""
    sql = (
        "SELECT id, title, topic, status, created_by, created_at, updated_at"
        " FROM reviews WHERE 1 = 1"
    )
    args = []
    if created_by:
        sql += " AND created_by = ?"
        args.append(created_by)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]


def update_review_content(conn, review_id: str, content: str, status: str = None) -> bool:
    """更新综述内容/状态。"""
    cur = conn.execute(
        """UPDATE reviews SET content = ?,
           status = COALESCE(?, status), updated_at = now() WHERE id = ?""",
        (content or '', status, review_id))
    return cur.rowcount > 0


# ══════════════════════════════════════════════════════════════════
# 审计 / 统计
# ══════════════════════════════════════════════════════════════════

def log_search(conn, query: str, source_db: str, result_count: int, user_id=None) -> None:
    """记录一次检索（供统计仪表盘使用）。"""
    conn.execute(
        "INSERT INTO search_logs (query, source_db, result_count, user_id)"
        " VALUES (?, ?, ?, ?)",
        (query or '', source_db or '', int(result_count or 0), user_id))


def get_stats(conn) -> dict:
    """插件仪表盘统计。"""
    stats = {
        'total_papers': 0,
        'total_notes': 0,
        'total_projects': 0,
        'searches_24h': 0,
    }
    try:
        stats['total_papers'] = int(conn.execute(
            "SELECT COUNT(*) AS c FROM papers").fetchone()['c'])
        stats['total_notes'] = int(conn.execute(
            "SELECT COUNT(*) AS c FROM annotations").fetchone()['c'])
        stats['total_projects'] = int(conn.execute(
            "SELECT COUNT(*) AS c FROM projects").fetchone()['c'])
        stats['searches_24h'] = int(conn.execute(
            "SELECT COUNT(*) AS c FROM search_logs"
            " WHERE created_at >= now() - interval '24 hours'").fetchone()['c'])
    except Exception as e:
        logger.warning('get_stats partial failure: %s', e)
    return stats
