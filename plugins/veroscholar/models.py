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
import re
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


def list_papers(conn, limit=50, offset=0, source_db='', year=None, q='',
                tag_id=None, status=None):
    """论文库列表：数据源 / 年份 / 关键词 / 标签 / 阅读状态筛选，按被引数降序。

    ILIKE 由 pg_trgm GIN 索引自动加速（migrations/v1.1.1_lib.sql）。
    """
    sql = (
        "SELECT id, doi, title, authors, abstract, venue, year,"
        " citation_count, pdf_url, source_db, external_id, reading_status,"
        " created_at"
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
    if tag_id:
        sql += (" AND EXISTS (SELECT 1 FROM paper_tags pt"
                " WHERE pt.paper_id = papers.id AND pt.tag_id = ?)")
        args.append(int(tag_id))
    if status:
        sql += " AND reading_status = ?"
        args.append(status)
    sql += " ORDER BY citation_count DESC NULLS LAST, year DESC NULLS LAST"
    sql += " LIMIT ? OFFSET ?"
    args += [int(limit), int(offset)]
    return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]


def count_papers(conn, source_db='', year=None, q='', tag_id=None,
                 status=None) -> int:
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
    if tag_id:
        sql += (" AND EXISTS (SELECT 1 FROM paper_tags pt"
                " WHERE pt.paper_id = papers.id AND pt.tag_id = ?)")
        args.append(int(tag_id))
    if status:
        sql += " AND reading_status = ?"
        args.append(status)
    row = conn.execute(sql, tuple(args)).fetchone()
    return int(row['c'])


# ══════════════════════════════════════════════════════════════════
# 标签
# ══════════════════════════════════════════════════════════════════

READING_STATUSES = ('unread', 'reading', 'read')


def list_tags(conn):
    """全部标签（按名称排序）。"""
    return [dict(r) for r in conn.execute(
        "SELECT id, name FROM tags ORDER BY name").fetchall()]


def paper_tags(conn, paper_id):
    """某论文的标签列表。"""
    return [dict(r) for r in conn.execute(
        "SELECT t.id, t.name FROM tags t"
        " JOIN paper_tags pt ON pt.tag_id = t.id"
        " WHERE pt.paper_id = ? ORDER BY t.name", (paper_id,)).fetchall()]


def add_tag(conn, paper_id, name):
    """给论文加标签（幂等：同名标签复用，重复关联忽略），返回 tag_id。"""
    name = name.strip()
    conn.execute(
        "INSERT INTO tags (name) VALUES (?)"
        " ON CONFLICT (name) DO NOTHING", (name,))
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
    if not row:
        return None
    tag_id = int(row['id'])
    conn.execute(
        "INSERT INTO paper_tags (paper_id, tag_id) VALUES (?, ?)"
        " ON CONFLICT (paper_id, tag_id) DO NOTHING",
        (paper_id, tag_id))
    return tag_id


def remove_tag(conn, paper_id, tag_id):
    """从论文移除标签。"""
    conn.execute(
        "DELETE FROM paper_tags WHERE paper_id = ? AND tag_id = ?",
        (paper_id, tag_id))


def set_reading_status(conn, paper_id, status):
    """更新论文阅读状态；非法状态返回 False。"""
    if status not in READING_STATUSES:
        return False
    conn.execute(
        "UPDATE papers SET reading_status = ?, updated_at = now()"
        " WHERE id = ?", (status, paper_id))
    return True


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
                  paper_ids, created_by, search_strategy=None) -> str:
    """创建综述文档，返回 id。"""
    if search_strategy:
        row = conn.execute(
            """INSERT INTO reviews (title, topic, structure, content, paper_ids,
                                    created_by, search_strategy)
               VALUES (?, ?, ?, ?, ?, ?, ?::jsonb) RETURNING id""",
            (title, topic or '', json.dumps(structure or {}, ensure_ascii=False),
             content or '', json.dumps(paper_ids or [], ensure_ascii=False),
             created_by,
             json.dumps(search_strategy, ensure_ascii=False) if isinstance(search_strategy, dict) else search_strategy)).fetchone()
    else:
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


def log_search_strategy(conn, strategy: dict, created_by=None) -> str:
    """记录检索策略并返回 strategy_id。"""
    row = conn.execute(
        "INSERT INTO search_strategies (strategy, created_by) VALUES (?, ?) RETURNING id",
        (json.dumps(strategy, ensure_ascii=False), created_by)).fetchone()
    return row['id']


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


# ══════════════════════════════════════════════════════════════════
# 引文导出格式（BibTeX / RIS）— 纯函数，供 routes 层调用
# ══════════════════════════════════════════════════════════════════

def _parse_authors(authors) -> list:
    """authors 字段可能是 JSON 字符串或已解析列表 → ['Name1', 'Name2']。"""
    if authors is None:
        return []
    if isinstance(authors, str):
        try:
            authors = json.loads(authors)
        except Exception:
            return []
    out = []
    for a in authors or []:
        if isinstance(a, dict):
            name = (a.get('name') or '').strip()
        else:
            name = str(a).strip()
        if name:
            out.append(name)
    return out


def _bibtex_key(title: str, year) -> str:
    """由标题前 6 词 + 年份生成 BibTeX 引用键（如 attention-is-all-you-2017）。"""
    words = re.findall(r'[A-Za-z0-9]+', title.lower())[:6]
    base = '-'.join(words) or 'paper'
    return '%s%s' % (base, year or '')


# BibTeX 特殊字符单次正则替换：每个匹配独立替换，替换文本不再被二次扫描，
# 从而避免先转义反斜杠再转义花括号时，把已插入的 \textbackslash{} 括号二次转义。
_BIBTEX_SPECIAL = re.compile(r'([\\&%#_{}])')
_BIBTEX_MAP = {
    '\\': r'\textbackslash{}',
    '&': r'\&',
    '%': r'\%',
    '#': r'\#',
    '_': r'\_',
    '{': r'\{',
    '}': r'\}',
}


def _escape_bibtex(text: str) -> str:
    """转义 BibTeX 特殊字符；单次替换，避免插入的反斜杠/花括号被二次转义。"""
    if not text:
        return ''
    return _BIBTEX_SPECIAL.sub(lambda m: _BIBTEX_MAP[m.group(1)], text)


def _is_preprint(paper: dict) -> bool:
    """判断论文是否为预印本（arXiv）。"""
    return (paper.get('source_db') == 'arxiv'
            or (paper.get('doi') or '').lower().startswith('10.48550/'))


def to_bibtex(paper: dict) -> str:
    """生成论文 BibTeX 条目；无标题返回空串。

    Args:
        paper: 论文行字典（含 title/authors/year/venue/doi/pdf_url）。
    """
    title = (paper.get('title') or '').strip()
    if not title:
        return ''
    authors = _parse_authors(paper.get('authors'))
    if _is_preprint(paper):
        eprint = (paper.get('external_id') or '').replace('arXiv:', '').strip() \
                 or (paper.get('doi') or '').replace('10.48550/arXiv.', '')
        lines = ['@misc{%s,' % _bibtex_key(title, paper.get('year')),
                 '  author = {%s},' % (' and '.join(authors) or 'Unknown'),
                 '  title = {%s},' % _escape_bibtex(title),
                 '  year = {%s},' % str(paper.get('year') or ''),
                 '  eprint = {%s},' % _escape_bibtex(eprint),
                 '  archivePrefix = {arXiv},',
                 '  doi = {%s},' % (paper.get('doi') or '').strip(),
                 '  url = {%s},' % (paper.get('pdf_url') or '').strip(),
                 '}']
        return '\n'.join(l for l in lines if not l.endswith('{},'))
    entry = {
        'author': ' and '.join(authors) or 'Unknown',
        'title': title,
        'journal': (paper.get('venue') or '').strip(),
        'year': str(paper.get('year') or ''),
        'doi': (paper.get('doi') or '').strip(),
        'url': (paper.get('pdf_url') or '').strip(),
    }
    lines = ['@article{%s,' % _bibtex_key(title, paper.get('year'))]
    for key in ('author', 'title', 'journal', 'year', 'doi', 'url'):
        value = entry[key]
        if value:
            lines.append('  %s = {%s},' % (key, _escape_bibtex(value)))
    lines.append('}')
    return '\n'.join(lines)


def to_ris(paper: dict) -> str:
    """生成论文 RIS 条目；无标题返回空串。"""
    title = (paper.get('title') or '').strip()
    if not title:
        return ''
    if _is_preprint(paper):
        lines = ['TY  - RPRT']
        for name in _parse_authors(paper.get('authors')):
            lines.append('AU  - ' + name)
        lines.append('TI  - ' + title)
        venue = (paper.get('venue') or '').strip()
        if venue:
            lines.append('JO  - ' + venue)
        if paper.get('year'):
            lines.append('PY  - ' + str(paper.get('year')))
        lines.append('DB  - arXiv')
        doi = (paper.get('doi') or '').strip()
        if doi:
            lines.append('DO  - ' + doi)
        pdf = (paper.get('pdf_url') or '').strip()
        if pdf:
            lines.append('UR  - ' + pdf)
        lines.append('ER  - ')
        return '\n'.join(lines)
    lines = ['TY  - JOUR']
    for name in _parse_authors(paper.get('authors')):
        lines.append('AU  - ' + name)
    lines.append('TI  - ' + title)
    venue = (paper.get('venue') or '').strip()
    if venue:
        lines.append('JO  - ' + venue)
    if paper.get('year'):
        lines.append('PY  - ' + str(paper.get('year')))
    doi = (paper.get('doi') or '').strip()
    if doi:
        lines.append('DO  - ' + doi)
    pdf = (paper.get('pdf_url') or '').strip()
    if pdf:
        lines.append('UR  - ' + pdf)
    lines.append('ER  - ')
    return '\n'.join(lines)


# ══════════════════════════════════════════════════════════════════
# AI 辅助数据访问（相关推荐 / 向量回填）
# ══════════════════════════════════════════════════════════════════

def search_related_papers(conn, vec_literal, exclude_id, limit=5):
    """按摘要向量余弦相似度返回相关论文（需 papers.embedding 非空）。

    Args:
        vec_literal: '[0.1,0.2,...]' 向量字符串
        exclude_id:  排除当前论文 id
    """
    sql = (
        "SELECT id, doi, title, venue, year, citation_count, pdf_url,"
        "       1 - (embedding <=> ?::vector) AS similarity"
        " FROM papers"
        " WHERE embedding IS NOT NULL AND id != ?"
        " ORDER BY embedding <=> ?::vector"
        " LIMIT ?"
    )
    rows = conn.execute(sql, (vec_literal, exclude_id, vec_literal, limit)).fetchall()
    return [dict(r) for r in rows]


def update_paper_embedding(conn, paper_id, vec_literal):
    """回填论文摘要向量（vec_literal 为 '[0.1,...]' 字符串）。"""
    conn.execute(
        "UPDATE papers SET embedding = ?::vector, updated_at = now()"
        " WHERE id = ?", (vec_literal, paper_id))


# ══════════════════════════════════════════════════════════════════
# 模块 B：PDF 全文
# ══════════════════════════════════════════════════════════════════

def create_fulltext_file(conn, paper_id, filename, size_bytes, sha256,
                         n_chunks, created_by) -> str:
    """重复 (paper_id, sha256) 时返回既有记录 id（幂等）。"""
    row = conn.execute(
        "SELECT id FROM fulltext_files WHERE paper_id = ? AND sha256 = ? LIMIT 1",
        (paper_id, sha256)).fetchone()
    if row:
        return row['id']
    row = conn.execute(
        """INSERT INTO fulltext_files (paper_id, filename, size_bytes, sha256,
                                       n_chunks, created_by)
           VALUES (?, ?, ?, ?, ?, ?) RETURNING id""",
        (paper_id, filename, int(size_bytes), sha256, int(n_chunks), created_by)
    ).fetchone()
    return row['id']


def add_fulltext_chunks(conn, file_id, paper_id, chunks: list) -> int:
    """写入分块（仅在首次解析时调用，幂等上传不重灌块）。"""
    for seq, c in enumerate(chunks):
        conn.execute(
            "INSERT INTO fulltext_chunks (paper_id, file_id, seq, page, content) "
            "VALUES (?, ?, ?, ?, ?)",
            (paper_id, file_id, seq, int(c.get('page') or 1), c['content']))
    return len(chunks)


def list_fulltext_chunks(conn, paper_id, limit=50, offset=0) -> list:
    rows = conn.execute(
        "SELECT seq, page, content FROM fulltext_chunks "
        "WHERE paper_id = ? ORDER BY seq LIMIT ? OFFSET ?",
        (paper_id, int(limit), int(offset))).fetchall()
    return [dict(r) for r in rows]


def list_fulltext_files(conn, paper_id) -> list:
    rows = conn.execute(
        "SELECT id, filename, size_bytes, n_chunks, status, created_at "
        "FROM fulltext_files WHERE paper_id = ? ORDER BY created_at DESC",
        (paper_id,)).fetchall()
    return [dict(r) for r in rows]
