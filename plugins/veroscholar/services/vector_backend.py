#!/usr/bin/env python3
"""VeroScholar 向量后端抽象层（科研版桌面降级 — 方案 §6.4 / §10 pgvector_fallback）。

三档能力，按运行环境自动选档：
  T2 pgvector   — 科研服务器版：papers.embedding vector(1536) + ivfflat 余弦索引
  T1 本地向量   — 桌面（无 pgvector 二进制）：paper_vectors 侧表存 JSON 向量，
                  numpy 平面余弦 Top-K（sqlite-vec 为 Node 侧方案，Python 插件取 numpy
                  ——requirements 已含 numpy>=1.24，桌面 venv 零新增依赖）
  T0 纯关键词   — pg_trgm trigram 相似度（桌面捆绑 PG 自带，中英文均可用）

设计约束：
  - 探测缓存：每连接首次探测后进程级缓存（pg_extension 查询代价可忽略，仍防抖）
  - 全部公开函数自带降级：单档失败自动落到下一档，绝不让 related 链路 5xx
  - 侧表 paper_vectors 自愈：CREATE TABLE IF NOT EXISTS，老库（v1.0.0 已应用）
    升级后首次写入时自动补建，无需迁移版本号
"""
import json
import logging
import math
import threading

logger = logging.getLogger('veroscholar.vector_backend')

#: 探测结果缓存（进程级；PG 会话内 extension/column 不会中途出现或消失到需要更高频度）
_detection_lock = threading.Lock()
_detection_cache = {}

_LOCAL_TABLE = """
CREATE TABLE IF NOT EXISTS paper_vectors (
    paper_id   uuid PRIMARY KEY,
    dim        integer NOT NULL,
    vec        jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
)
"""


def _schema_has_column(conn, table, column):
    row = conn.execute(
        "SELECT 1 FROM information_schema.columns"
        " WHERE table_schema = 'veroscholar' AND table_name = ? AND column_name = ?"
        " LIMIT 1", (table, column)).fetchone()
    return row is not None


def detect_backend(conn) -> str:
    """探测当前库可用的最高向量档位：'pgvector' | 'local' | 'keyword'。"""
    key = id(conn)
    with _detection_lock:
        cached = _detection_cache.get(key)
    if cached:
        return cached
    tier = 'keyword'
    try:
        pgvector = conn.execute(
            "SELECT 1 FROM pg_extension WHERE extname = 'vector' LIMIT 1").fetchone()
        if pgvector and _schema_has_column(conn, 'papers', 'embedding'):
            tier = 'pgvector'
        else:
            # 扩展在但列缺失（老库）→ 仍可走 T1 本地向量
            tier = 'local'
    except Exception:
        tier = 'keyword'
    # T1 需要 numpy（桌面 venv 已含；缺失时退 T0）
    if tier == 'local':
        try:
            import numpy  # noqa: F401
        except Exception:
            tier = 'keyword'
    with _detection_lock:
        _detection_cache[key] = tier
    logger.info('vector backend tier: %s', tier)
    return tier


def _parse_vec_literal(vec_literal):
    """'[0.1,0.2,...]' → list[float]；非法返回 None。"""
    if not vec_literal:
        return None
    try:
        body = str(vec_literal).strip().strip('[]')
        return [float(x) for x in body.split(',') if x.strip()]
    except Exception:
        return None


def _ensure_local_table(conn):
    try:
        conn.execute(_LOCAL_TABLE)
        conn.commit()
    except Exception as e:
        logger.warning('paper_vectors table ensure failed: %s', e)


def update_paper_vector(conn, paper_id, vec_literal) -> bool:
    """把摘要向量写入当前档位对应的存储；keyword 档为 no-op。"""
    vec = _parse_vec_literal(vec_literal)
    if vec is None:
        return False
    tier = detect_backend(conn)
    try:
        if tier == 'pgvector':
            conn.execute(
                "UPDATE papers SET embedding = ?::vector, updated_at = now()"
                " WHERE id = ?", (vec_literal, paper_id))
            conn.commit()
            return True
        if tier == 'local':
            _ensure_local_table(conn)
            conn.execute(
                "INSERT INTO paper_vectors (paper_id, dim, vec) VALUES (?, ?, ?)"
                " ON CONFLICT (paper_id) DO UPDATE SET"
                " dim = EXCLUDED.dim, vec = EXCLUDED.vec, updated_at = now()",
                (paper_id, len(vec), json.dumps(vec)))
            conn.commit()
            return True
    except Exception as e:
        logger.warning('update_paper_vector(%s) failed on %s: %s',
                       paper_id, tier, e)
        try:
            conn.rollback()
        except Exception:
            pass
    return False


def related_papers(conn, paper_id, vec_literal, limit=5):
    """相似论文检索：按档位走 pgvector SQL / numpy 余弦 / pg_trgm 关键词。"""
    tier = detect_backend(conn)
    if tier == 'pgvector' and vec_literal:
        return _related_pgvector(conn, paper_id, vec_literal, limit)
    if tier == 'local' and vec_literal:
        return _related_local(conn, paper_id, vec_literal, limit)
    return related_papers_keyword(conn, paper_id, limit)


def _related_pgvector(conn, paper_id, vec_literal, limit):
    sql = (
        "SELECT id, doi, title, venue, year, citation_count, pdf_url,"
        "       1 - (embedding <=> ?::vector) AS similarity"
        " FROM papers"
        " WHERE embedding IS NOT NULL AND id != ?"
        " ORDER BY embedding <=> ?::vector"
        " LIMIT ?"
    )
    rows = conn.execute(sql, (vec_literal, paper_id, vec_literal, limit)).fetchall()
    return [dict(r) for r in rows]


def _related_local(conn, paper_id, vec_literal, limit):
    """numpy 平面余弦：paper_vectors 侧表全量拉取（桌面文献量级 ≤ 数千篇足够）。"""
    import numpy as np
    query = _parse_vec_literal(vec_literal)
    if not query:
        return related_papers_keyword(conn, paper_id, limit)
    _ensure_local_table(conn)
    rows = conn.execute(
        "SELECT p.id, p.doi, p.title, p.venue, p.year, p.citation_count, p.pdf_url,"
        "       pv.vec AS vec"
        " FROM paper_vectors pv JOIN papers p ON p.id = pv.paper_id"
        " WHERE pv.paper_id != ?", (paper_id,)).fetchall()
    q = np.array(query, dtype='float32')
    qn = float(np.linalg.norm(q)) or 1.0
    scored = []
    for r in rows:
        cand = _parse_vec_literal(r['vec'] if not isinstance(r['vec'], str)
                                  else r['vec'])
        if not cand or len(cand) != len(query):
            continue
        v = np.array(cand, dtype='float32')
        denom = float(np.linalg.norm(v))
        if denom == 0.0 or qn == 0.0 or math.isnan(denom):
            continue
        sim = float(np.dot(q, v) / (qn * denom))
        scored.append((sim, r))
    scored.sort(key=lambda t: t[0], reverse=True)
    out = []
    for sim, r in scored[: int(limit)]:
        d = dict(r)
        d.pop('vec', None)
        d['similarity'] = round(sim, 6)
        out.append(d)
    return out


def related_papers_keyword(conn, paper_id, limit=5):
    """T0 关键词降档：pg_trgm 对 title/abstract 做 trigram 相似度排序。

    语义上弱于向量余弦，但保住「相关论文」入口在桌面纯本地环境的可用性；
    pg_trgm 不可用时退化为按引用数排序的近期论文（仍不为空）。
    """
    anchor = conn.execute(
        "SELECT title, abstract FROM papers WHERE id = ? LIMIT 1", (paper_id,)
    ).fetchone()
    if not anchor:
        return []
    title = (anchor['title'] or '')[:512]
    abstract = (anchor['abstract'] or '')[:2048]
    try:
        rows = conn.execute(
            "SELECT id, doi, title, venue, year, citation_count, pdf_url,"
            "       GREATEST(similarity(title, ?), similarity(abstract, ?)) AS similarity"
            " FROM papers"
            " WHERE id != ?"
            " ORDER BY similarity DESC, citation_count DESC NULLS LAST"
            " LIMIT ?", (title, abstract, paper_id, int(limit))).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning('trgm related fallback failed: %s', e)
        rows = conn.execute(
            "SELECT id, doi, title, venue, year, citation_count, pdf_url"
            " FROM papers WHERE id != ?"
            " ORDER BY citation_count DESC NULLS LAST, year DESC NULLS LAST"
            " LIMIT ?", (paper_id, int(limit))).fetchall()
        return [dict(r) for r in rows]


def search_papers_keyword(conn, query, limit=30):
    """T0 关键词检索（供 RAG 合并/降级复用）：trgm 加速的 ILIKE + 相似度混合排序。"""
    q = (query or '').strip()
    if not q:
        return []
    like = f'%{q}%'
    try:
        rows = conn.execute(
            "SELECT id, doi, title, venue, year, citation_count, pdf_url,"
            "       similarity(title, ?) AS similarity"
            " FROM papers"
            " WHERE title ILIKE ? OR abstract ILIKE ?"
            " ORDER BY similarity DESC, citation_count DESC NULLS LAST"
            " LIMIT ?", (q[:512], like, like, int(limit))).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        rows = conn.execute(
            "SELECT id, doi, title, venue, year, citation_count, pdf_url"
            " FROM papers WHERE title ILIKE ? OR abstract ILIKE ?"
            " ORDER BY citation_count DESC NULLS LAST"
            " LIMIT ?", (like, like, int(limit))).fetchall()
        return [dict(r) for r in rows]
