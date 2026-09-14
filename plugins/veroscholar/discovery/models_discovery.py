#!/usr/bin/env python3
"""Discovery Engine 数据层（v1.11.0）。

独立 schema `veroscholar` 内 6 张表（见 migrations/v1.4.0_discovery.sql）：
  discovery_runs          一轮发现运行（含启动器/阶段/输入问题）
  hypotheses              假设主表（证据池/聚类/判定/最终状态）
  hypothesis_votes        Elo 锦标赛计分
  hypothesis_discussions  讨论记录（问题/生成回复）
  discovery_events        节点级事件流（阶段/耗时/错误）
  discovery_memory        长期记忆（可复用提示词；M1 预算/回流预留）

约定：
  - 所有 SQL 使用 `?` 占位符（由连接池统一转换为 %s）
  - commit 由调用方负责（get_db() 关闭连接时自动回滚，不自动提交）
  - 迁移文件位于 migrations/v1.4.0_discovery.sql（字典序在全文之后）
"""

import json
import logging
import uuid
from datetime import datetime

from .. import models as m

logger = logging.getLogger('veroscholar.discovery.models')


def _jsonb(value):
    """序列化为 JSONB 参数：PG 用 psycopg2.extras.Json（jsonb 原生类型），
    其他适配层（FakeConn/非 PG 回退）转 JSON 字符串，保证两态可写。"""
    try:
        from psycopg2.extras import Json  # 存在 psycopg2 → 原生 JSONB
        return Json(value or {})
    except Exception:
        return json.dumps(value or {}, ensure_ascii=False)

# ══════════════════════════════════════════════════════════════════
# 运行生命周期
# ══════════════════════════════════════════════════════════════════

def create_run(conn, *, question, user_id, project_id=None, trigger='manual',
               config=None) -> str:
    """创建一轮发现运行，返回 run_id。

    冲突检测：同一 user_id + 同一 question（截断 300 字符）在 60 秒内存在
    running 运行则直接复用（防重复触发，幂等）。
    """
    run_id = str(uuid.uuid4())
    question = (question or '').strip()
    if not question:
        raise ValueError('question is required')
    question = question[:300]

    recent = conn.execute(
        'SELECT id FROM discovery_runs '
        'WHERE user_id = ? AND question = ? AND status = ? '
        'AND created_at > NOW() - INTERVAL \'60 seconds\' '
        'ORDER BY created_at DESC LIMIT 1',
        (user_id, question, 'running')).fetchone()
    if recent:
        return recent['id']

    conn.execute(
        'INSERT INTO discovery_runs '
        '(id, question, project_id, user_id, trigger, status, config) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        (run_id, question, project_id, user_id, trigger, 'running',
         _jsonb(config or {})))
    return run_id


def finish_run(conn, run_id, status='completed', error=None) -> None:
    """结束一轮运行（completed / failed / cancelled）。"""
    conn.execute(
        'UPDATE discovery_runs SET status = ?, error = ?, finished_at = NOW() '
        'WHERE id = ?',
        (status, error, run_id))


def list_runs(conn, user_id, limit=20, offset=0) -> list:
    """列出当前用户的历史运行（倒序）。"""
    rows = conn.execute(
        'SELECT id, question, status, trigger, created_at, finished_at '
        'FROM discovery_runs WHERE user_id = ? '
        'ORDER BY created_at DESC LIMIT ? OFFSET ?',
        (user_id, limit, offset)).fetchall()
    return [_row_dict(r) for r in rows]


def get_run(conn, run_id) -> dict:
    """运行详情（含阶段与错误信息）。"""
    row = conn.execute(
        'SELECT * FROM discovery_runs WHERE id = ?', (run_id,)).fetchone()
    return _row_dict(row) if row else None


def count_runs(conn, user_id) -> int:
    """运行总数（分页用）。"""
    row = conn.execute(
        'SELECT COUNT(*) AS c FROM discovery_runs WHERE user_id = ?',
        (user_id,)).fetchone()
    return int(row['c']) if row else 0


# ══════════════════════════════════════════════════════════════════
# 假设 CRUD
# ══════════════════════════════════════════════════════════════════

def create_hypotheses(conn, run_id, hypotheses: list) -> list:
    """批量写入假设，返回带 id 的完整记录列表。"""
    out = []
    for h in hypotheses:
        hid = str(uuid.uuid4())
        conn.execute(
            'INSERT INTO hypotheses '
            '(id, run_id, statement, context, variables, relationship, '
            ' null_hypothesis, falsification_test, score, evidence_ids, '
            ' cluster_id, current_status, elo_rating, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NOW())',
            (hid, run_id, (h.get('statement') or '').strip()[:2000],
             h.get('context') or '',
             _jsonb(h.get('variables') or []),
             h.get('relationship') or '',
             h.get('null_hypothesis') or '',
             h.get('falsification_test') or '',
             h.get('score') or 0.0,
             _jsonb(h.get('evidence_ids') or []),
             h.get('cluster_id'), h.get('current_status') or 'pending',
             h.get('elo_rating') or 1000.0))
        out.append(_row_dict(conn.execute(
            'SELECT * FROM hypotheses WHERE id = ?', (hid,)).fetchone()))
    return out


def list_hypotheses(conn, run_id) -> list:
    """一轮运行下的全部假设（按 Elo 降序）。"""
    rows = conn.execute(
        'SELECT * FROM hypotheses WHERE run_id = ? '
        'ORDER BY elo_rating DESC, created_at ASC', (run_id,)).fetchall()
    return [_row_dict(r) for r in rows]


def get_hypothesis(conn, hypothesis_id) -> dict:
    """单条假设详情。"""
    row = conn.execute(
        'SELECT * FROM hypotheses WHERE id = ?', (hypothesis_id,)).fetchone()
    return _row_dict(row) if row else None


def update_hypothesis(conn, hypothesis_id, **fields) -> None:
    """白名单字段更新（防止 SQL 注入 / 越权写任意列）。"""
    allowed = {'statement', 'score', 'evidence_ids', 'cluster_id',
               'current_status', 'elo_rating', 'context', 'variables',
               'relationship', 'null_hypothesis', 'falsification_test'}
    cols = []
    vals = []
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError('field not allowed: %s' % k)
        if k in ('evidence_ids', 'variables'):
            v = _jsonb(v or [])
        cols.append('%s = ?' % k)
        vals.append(v)
    if not cols:
        return
    vals.append(hypothesis_id)
    conn.execute(
        'UPDATE hypotheses SET %s WHERE id = ?' % ', '.join(cols), vals)


# ══════════════════════════════════════════════════════════════════
# Elo 计分（锦标赛）
# ══════════════════════════════════════════════════════════════════

def record_vote(conn, *, run_id, winner_id, loser_id, voter='engine',
                update_elo=True) -> None:
    """记录一次 Elo 对局（winner>loser）。

    去重域按 (run_id, winner_id, loser_id, voter) 划分（N6，唯一键含 voter）：
    - 引擎审计行（voter='engine'）与人工票（voter='user:<uid>'）各行其道，
      人工票不再被引擎对局的去重记录静默吞掉；
    - 同 voter 重复记录同一有向对局时静默去重，保证幂等。
    - update_elo=True（默认）：按 Elo 公式更新双方 rating（人工投票等交互路径）
    - update_elo=False：仅审计留痕（引擎对局，voter='engine'），不重放计分；
      Elo 以 judge 透传的锦标赛快照落库为准，杜绝双重计分
    """
    dup = conn.execute(
        'SELECT id FROM hypothesis_votes '
        'WHERE run_id = ? AND winner_id = ? AND loser_id = ? AND voter = ?',
        (run_id, winner_id, loser_id, voter)).fetchone()
    if dup:
        return
    conn.execute(
        'INSERT INTO hypothesis_votes '
        '(id, run_id, winner_id, loser_id, voter, created_at) '
        'VALUES (?, ?, ?, ?, ?, NOW())',
        (str(uuid.uuid4()), run_id, winner_id, loser_id, voter))
    if not update_elo:
        return

    w = get_hypothesis(conn, winner_id)
    l = get_hypothesis(conn, loser_id)
    if not w or not l:
        return
    wa = float(w.get('elo_rating') or 1000.0)
    la = float(l.get('elo_rating') or 1000.0)
    ew = 1.0 / (1.0 + 10 ** ((la - wa) / 400.0))
    new_w = wa + 32.0 * (1.0 - ew)
    new_l = la + 32.0 * (0.0 - (1.0 - ew))
    update_hypothesis(conn, winner_id, elo_rating=round(new_w, 1))
    update_hypothesis(conn, loser_id, elo_rating=round(new_l, 1))


def list_ranked(conn, run_id) -> list:
    """按 Elo 降序返回假设（最终排行榜）。"""
    return list_hypotheses(conn, run_id)


# ══════════════════════════════════════════════════════════════════
# 讨论（假设推演）
# ══════════════════════════════════════════════════════════════════

def add_discussion(conn, *, hypothesis_id, protocol, question, response,
                   meta=None) -> None:
    """写入一条讨论记录（含 LLM 原始回复与元信息）。"""
    conn.execute(
        'INSERT INTO hypothesis_discussions '
        '(id, hypothesis_id, protocol, question, response, meta, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?, NOW())',
        (str(uuid.uuid4()), hypothesis_id, protocol, question, response,
         _jsonb(meta or {})))


def list_discussions(conn, hypothesis_id) -> list:
    """某假设的全部讨论记录（正序）。"""
    rows = conn.execute(
        'SELECT protocol, question, response, meta, created_at '
        'FROM hypothesis_discussions WHERE hypothesis_id = ? '
        'ORDER BY created_at ASC', (hypothesis_id,)).fetchall()
    return [_row_dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════
# 事件流（工作流节点审计）
# ══════════════════════════════════════════════════════════════════

def add_event(conn, *, run_id, stage, status='info', detail=None) -> None:
    """记录一条节点/阶段事件（运行审计 + 前端进度）。"""
    conn.execute(
        'INSERT INTO discovery_events '
        '(id, run_id, stage, status, detail, created_at) '
        'VALUES (?, ?, ?, ?, ?, NOW())',
        (str(uuid.uuid4()), run_id, stage, status,
         _jsonb(detail or {})))


def list_events(conn, run_id) -> list:
    """运行事件流（正序）。"""
    rows = conn.execute(
        'SELECT stage, status, detail, created_at '
        'FROM discovery_events WHERE run_id = ? '
        'ORDER BY created_at ASC', (run_id,)).fetchall()
    return [_row_dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════
# 长期记忆（M1 预算/回流预留，当前仅存储不消费）
# ══════════════════════════════════════════════════════════════════

def save_memory(conn, *, run_id, memory_type, content) -> None:
    """写入一条长期记忆（幂等：同 run + type 覆盖更新）。"""
    conn.execute(
        'INSERT INTO discovery_memory (run_id, memory_type, content, updated_at) '
        'VALUES (?, ?, ?, NOW()) '
        'ON CONFLICT (run_id, memory_type) DO UPDATE SET '
        'content = EXCLUDED.content, updated_at = NOW()',
        (run_id, memory_type, content))


def list_memory(conn, run_id) -> list:
    """读取一轮运行的长期记忆。"""
    rows = conn.execute(
        'SELECT memory_type, content FROM discovery_memory WHERE run_id = ? '
        'ORDER BY updated_at ASC', (run_id,)).fetchall()
    return [_row_dict(r) for r in rows]


def update_run_metrics(conn, run_id, metrics: dict) -> None:
    """合并写入运行指标（metrics JSON，可多段累加，如结构统计 + LLM 调用数）。"""
    cur = {}
    row = conn.execute(
        'SELECT metrics FROM discovery_runs WHERE id = ?', (run_id,)).fetchone()
    if row and row.get('metrics'):
        try:
            cur = json.loads(row.get('metrics')) if isinstance(row.get('metrics'), str) else dict(row.get('metrics') or {})
        except Exception:
            cur = {}
    cur.update(metrics or {})
    conn.execute(
        'UPDATE discovery_runs SET metrics = ? WHERE id = ?',
        (_jsonb(cur or {}), run_id))


# ══════════════════════════════════════════════════════════════════
# 统计
# ══════════════════════════════════════════════════════════════════

def get_stats(conn) -> dict:
    """全局发现引擎统计（忽略 user_id 过滤，用于插件总览）。"""
    total_runs = conn.execute(
        'SELECT COUNT(*) AS c FROM discovery_runs').fetchone()
    running_runs = conn.execute(
        "SELECT COUNT(*) AS c FROM discovery_runs WHERE status = 'running'"
    ).fetchone()
    total_hypotheses = conn.execute(
        'SELECT COUNT(*) AS c FROM hypotheses').fetchone()
    avg_score = conn.execute(
        'SELECT COALESCE(AVG(score), 0.0) AS c FROM hypotheses').fetchone()
    return {
        'total_runs': int(total_runs['c']) if total_runs else 0,
        'running_runs': int(running_runs['c']) if running_runs else 0,
        'total_hypotheses': int(total_hypotheses['c']) if total_hypotheses else 0,
        'avg_hypothesis_score': round(float(avg_score['c']), 2) if avg_score else 0.0,
    }


# ══════════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════════

def _row_dict(row) -> dict:
    """将 psycopg2 Row 转为 dict（兼容 dict 直传测试桩）。"""
    if hasattr(row, 'keys'):
        return dict(row)
    return dict(row or {})


def to_epoch(value) -> float:
    """datetime → 秒级时间戳；非 datetime 原样返回（兼容测试桩）。"""
    if isinstance(value, datetime):
        return value.timestamp()
    return value
