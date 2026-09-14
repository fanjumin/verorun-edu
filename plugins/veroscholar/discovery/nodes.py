#!/usr/bin/env python3
"""Discovery DAG 节点处理器（v1.11.0）。

7 个节点类型（统一注册进 WorkflowEngine，与现有 veroscholar_search 并列）：
  veroscholar_discovery_gen       生成候选假设
  veroscholar_discovery_evidence  多源检索证据召回
  veroscholar_discovery_rank      Elo 排序
  veroscholar_discovery_cluster   关键词聚类（结构化聚簇，无向量依赖）
  veroscholar_discovery_discuss   假设推演（generate → critique → evolve）
  veroscholar_discovery_judge     最终裁决（LLM 打分）
  veroscholar_discovery_persist   落库 + 结束运行

节点间数据传递走引擎的 `context.node_<id>_output` 持久化，跨节点通过
`_collect_upstream()` 按"证据池/候选假设"等语义字段回溯获取，节点之间
不需要强耦合的输出 schema。

门禁：discovery_enabled 关闭时 handle_hypo_gen 入口直接返回失败，
其余节点仅在没有上游输入时惰性返回空结果（不报错、不落库）。
"""

import json
import logging
import time

from .. import models as m
from . import models_discovery as md
from . import discuss as ds

logger = logging.getLogger('veroscholar.discovery.nodes')


# ══════════════════════════════════════════════════════════════════
# 节点注册
# ══════════════════════════════════════════════════════════════════

def get_discovery_dag_nodes() -> dict:
    """返回 {节点类型: 处理器}，由插件基类合并进 register_dag_nodes。"""
    return {
        'veroscholar_discovery_gen': handle_hypo_gen,
        'veroscholar_discovery_evidence': handle_hypo_evidence,
        'veroscholar_discovery_rank': handle_hypo_rank,
        'veroscholar_discovery_cluster': handle_hypo_cluster,
        'veroscholar_discovery_discuss': handle_hypo_discuss,
        'veroscholar_discovery_judge': handle_hypo_judge,
        'veroscholar_discovery_persist': handle_hypo_persist,
    }


# ══════════════════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════════════════

def _load_discovery_config() -> dict:
    """读取插件配置（供 discovery_enabled 等门禁判断）。"""
    try:
        from flask import current_app
        pm = current_app.extensions.get('plugin_manager')
        if pm:
            cfg = pm.get_config('veroscholar') or {}
            return cfg if isinstance(cfg, dict) else {}
    except Exception:
        pass
    return {}


def _discovery_enabled() -> bool:
    """discovery 总开关（默认关闭）。"""
    return bool(_load_discovery_config().get('discovery_enabled', False))


# ══════════════════════════════════════════════════════════════════
# 公共工具
# ══════════════════════════════════════════════════════════════════

def _ctx(input_data: dict) -> dict:
    """从 input_data 提取 context（兼容缺省）。"""
    ctx = input_data.get('context') or {}
    return ctx if isinstance(ctx, dict) else {}


def _collect_upstream(input_data: dict, field: str):
    """沿上游节点输出回溯某个语义字段。

    引擎在 context 中持久化 `node_<id>_output`（每个已完成节点的返回值），
    并额外以 `node_<prev>_output` 直传最近上游。本函数遍历两者，返回
    第一个包含指定字段的值；未找到返回 None。
    """
    ctx = _ctx(input_data)
    if field in ctx:
        return ctx.get(field)
    for key, value in ctx.items():
        if key.startswith('node_') and key.endswith('_output'):
            if isinstance(value, dict) and field in value:
                return value.get(field)
    return None


def _escape_like(s: str) -> str:
    """转义 LIKE 模式中的通配符，避免用户输入注入 %/_(仅作为字面量匹配)。"""
    return str(s or '').replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def _stmt_key(text) -> str:
    """statement 归一化键：与落库截断口径一致（strip + 2000 字符），
    保证超长假设的讨论/对局按 statement 归属时不因截断不一致而静默丢失。"""
    return (text or '').strip()[:2000]


def _pick_relevant_chunk(chunks: list, question: str) -> str:
    """从全文分块中挑一段最相关的作为证据（简单关键词命中计分）。"""
    if not chunks:
        return ''
    best = ''
    best_score = 0
    keywords = [k for k in question.replace('?', '').split() if len(k) > 1]
    for c in chunks:
        content = (c.get('content') or '') if isinstance(c, dict) else str(c)
        score = sum(1 for k in keywords if k.lower() in content.lower())
        if score > best_score:
            best_score = score
            best = content
    return best or ((chunks[0].get('content') or '')
                    if isinstance(chunks[0], dict) else str(chunks[0]))


# ══════════════════════════════════════════════════════════════════
# n1 生成候选假设
# ══════════════════════════════════════════════════════════════════

def handle_hypo_gen(node_def: dict, input_data: dict) -> dict:
    """Generator：从研究问题生成候选假设。

    入参（node_def.config 或 input_data.context）:
      question  研究问题
      num_hypotheses  生成条数（默认 5）
    """
    if not _discovery_enabled():
        return {'success': False, 'error': 'Discovery engine is disabled'}
    ctx = _ctx(input_data)
    question = (node_def.get('config') or {}).get('question') or ctx.get('question')
    if not question:
        return {'success': False, 'error': 'question is required'}
    try:
        num = int((node_def.get('config') or {}).get('num_hypotheses')
                  or ctx.get('num_hypotheses') or 5)
    except (TypeError, ValueError):
        num = 5
    num = max(1, min(num, 10))

    start = time.time()
    hypotheses = ds.generate_hypotheses(question, n=num)
    if not hypotheses:
        # P2-8：透出 LLM 根因，便于定位是建模失败还是生成空（区别于"禁用"）
        return {'success': False, 'error': 'LLM produced no hypotheses',
                'warning': ds.last_error() or 'empty LLM output'}
    return {
        'success': True,
        'candidate_hypotheses': hypotheses,
        'count': len(hypotheses),
        'question': question,
        'duration_ms': int((time.time() - start) * 1000),
    }


# ══════════════════════════════════════════════════════════════════
# n2 证据召回（多源检索 + 全文分块补充）
# ══════════════════════════════════════════════════════════════════

def handle_hypo_evidence(node_def: dict, input_data: dict) -> dict:
    """Evidence Recall：围绕研究问题多源检索，并补充知识库全文分块。"""
    if not _discovery_enabled():
        return {'success': False, 'error': 'Discovery engine is disabled'}
    ctx = _ctx(input_data)
    question = ctx.get('question') or ''
    if not question:
        return {'success': False, 'error': 'question is required'}

    cfg = _load_discovery_config()
    per_source = (node_def.get('config') or {}).get('per_source') \
        or cfg.get('discovery_per_source') or 6
    try:
        per_source = int(per_source)
    except (TypeError, ValueError):
        per_source = 6

    evidence = []
    try:
        from ..workflow import run_multi_source_search
        results, _errors = run_multi_source_search(
            question, limit=30, per_source=per_source,
            user_id=ctx.get('user_id'))
        for r in results[:20]:
            evidence.append({
                'type': 'paper',
                'doi': r.get('doi'),
                'title': r.get('title'),
                'year': r.get('year'),
                'source': r.get('source'),
            })
    except Exception as e:
        logger.warning('evidence search failed: %s', e)

    # 全文分块补充（论文问答同款注入路径）
    try:
        chunks = []
        pattern = '%' + _escape_like(question[:80]) + '%'
        with m.get_db() as conn:
            rows = conn.execute(
                'SELECT seq, page, content FROM fulltext_chunks '
                'WHERE paper_id IN ('
                '  SELECT id FROM papers '
                '  WHERE title ILIKE ? ESCAPE \'\\\' '
                '    OR abstract ILIKE ? ESCAPE \'\\\''
                ') ORDER BY paper_id, seq LIMIT 50',
                (pattern, pattern)).fetchall()
            chunks = [{'seq': r['seq'], 'page': r['page'], 'content': r['content']}
                      for r in rows]
        if chunks:
            evidence.append({
                'type': 'chunk',
                'text': _pick_relevant_chunk(chunks, question),
            })
    except Exception as e:
        logger.warning('fulltext chunk recall failed: %s', e)

    return {
        'success': True,
        'evidence_pool': evidence,
        'count': len(evidence),
    }


# ══════════════════════════════════════════════════════════════════
# n3 Elo 排序
# ══════════════════════════════════════════════════════════════════

def handle_hypo_rank(node_def: dict, input_data: dict) -> dict:
    """Rank：对候选假设执行 Elo 锦标赛排序。"""
    hypotheses = _collect_upstream(input_data, 'candidate_hypotheses')
    question = _ctx(input_data).get('question') or ''
    if not hypotheses:
        return {'success': False, 'error': 'no candidate hypotheses upstream'}
    try:
        rounds = int((node_def.get('config') or {}).get('n_rounds')
                     or _load_discovery_config().get('discovery_elo_rounds') or 3)
    except (TypeError, ValueError):
        rounds = 3
    matches = []
    ranked = ds.elo_tournament(
        hypotheses, question, n_rounds=rounds,
        match_sink=lambda w, l: matches.append({
            'winner': (w.get('statement') or '').strip(),
            'loser': (l.get('statement') or '').strip()}))
    return {
        'success': True,
        'ranked_hypotheses': ranked,
        'count': len(ranked),
        'matches': matches,
    }


# ══════════════════════════════════════════════════════════════════
# n4 关键词聚类（结构化，无向量依赖）
# ══════════════════════════════════════════════════════════════════

def _cluster_key(statement: str) -> str:
    """从假设文本提取聚类键：最长连续字母数字词组的低 8 位词根。"""
    import re
    words = re.findall(r"[a-zA-Z\u4e00-\u9fff]+", statement or '')
    if not words:
        return 'uncategorized'
    best = max(words, key=len)
    return best[:8].lower() or 'uncategorized'


def handle_hypo_cluster(node_def: dict, input_data: dict) -> dict:
    """Cluster：按主题关键词将假设聚为若干簇（每簇 ≥1 条，带方法簇统计）。

    DE-7：额外返回 `cluster_assignments`（{statement 归一键: cluster_id}），
    供 persist 在落库时按假设文本把 cluster_id 写回 hypotheses。
    """
    hypotheses = _collect_upstream(input_data, 'ranked_hypotheses')
    if not hypotheses:
        return {'success': False, 'error': 'no ranked hypotheses upstream'}

    clusters = {}
    for h in hypotheses:
        key = _cluster_key(h.get('statement') or '')
        clusters.setdefault(key, []).append(h)

    cluster_list = []
    assignments = {}
    for key, members in clusters.items():
        cluster_list.append({
            'cluster_id': key,
            'hypotheses': members,
            'method': key,
            'size': len(members),
        })
        for h in members:
            assignments[_stmt_key(h.get('statement'))] = key
    return {
        'success': True,
        'clusters': cluster_list,
        'cluster_assignments': assignments,
        'cluster_count': len(cluster_list),
    }


# ══════════════════════════════════════════════════════════════════
# n5 假设推演（讨论协议）
# ══════════════════════════════════════════════════════════════════

def handle_hypo_discuss(node_def: dict, input_data: dict) -> dict:
    """Discuss：对每条假设执行 generate → critique → evolve 三轮推演。"""
    hypotheses = _collect_upstream(input_data, 'ranked_hypotheses')
    evidence = _collect_upstream(input_data, 'evidence_pool') or []
    if not hypotheses:
        return {'success': False, 'error': 'no ranked hypotheses upstream'}

    rounds = []
    for h in hypotheses[:5]:
        for r in range(1, 4):
            generated = ds.generate_hypotheses(
                h.get('statement') or '', n=1, tier='fast')
            if not generated:
                continue
            critique = ds.critique_hypothesis(h, evidence)
            evolved = ds.evolve_hypothesis(
                {'statement': generated[0].get('statement', '')}, critique)
            rounds.append({
                'hypothesis': h.get('statement') or '',
                'round': r,
                'generate': generated[0].get('statement', ''),
                'critique': critique.get('critique', ''),
                'evolved': evolved.get('statement', ''),
            })
    return {
        'success': True,
        'rounds': rounds,
        'round_count': len(rounds),
    }


# ══════════════════════════════════════════════════════════════════
# n6 最终裁决
# ══════════════════════════════════════════════════════════════════

def handle_hypo_judge(node_def: dict, input_data: dict) -> dict:
    """Judge：LLM 对每条假设独立评分（0-1），并给出最终裁决文本。"""
    hypotheses = _collect_upstream(input_data, 'ranked_hypotheses')
    evidence = _collect_upstream(input_data, 'evidence_pool') or []
    question = _ctx(input_data).get('question') or ''
    if not hypotheses:
        return {'success': False, 'error': 'no ranked hypotheses upstream'}

    judged = []
    for h in hypotheses:
        verdict = _judge_one(h, evidence, question)
        judged.append({
            'id': h.get('id'),
            'statement': h.get('statement', ''),
            'score': round(verdict.get('score', 0.5), 3),
            # N1：透传锦标赛 Elo，persist 落库即真实排名（DB 不再重放计分）
            'elo_rating': h.get('elo_rating', 1000.0),
            'verdict': verdict.get('verdict', ''),
            'rationale': verdict.get('rationale', ''),
            'context': h.get('context', ''),
            'variables': h.get('variables', []),
            'relationship': h.get('relationship', ''),
            'null_hypothesis': h.get('null_hypothesis', ''),
            'falsification_test': h.get('falsification_test', ''),
        })
    judged.sort(key=lambda x: x['score'], reverse=True)
    return {
        'success': True,
        'judged_hypotheses': judged,
        'count': len(judged),
    }


def _judge_one(h: dict, evidence: list, question: str = '') -> dict:
    """单条假设独立评分（0-1，走 review_hypothesis）；失败时按初始 score 降级。

    去除 v1.11.0 初版的"自身 vs 自身+(baseline)"自我比较——那会使分数恒 +0.05 虚高。
    """
    try:
        data = ds.review_hypothesis(h, question or h.get('statement', ''))
        score = float(data.get('score'))
        return {'score': round(max(0.0, min(1.0, score)), 3),
                'verdict': data.get('rationale', ''),
                'rationale': data.get('rationale', '')}
    except Exception as e:
        logger.warning('judge fallback: %s', e)
        return {'score': float(h.get('score') or 0.5),
                'verdict': 'Judge unavailable', 'rationale': str(e)}


# ══════════════════════════════════════════════════════════════════
# n7 落库 + 结束运行
# ══════════════════════════════════════════════════════════════════

def _collect_upstream_errors(input_data: dict) -> list:
    """收集上游各节点的失败原因（DAG 场景；引擎不终止失败节点，由 persist 兜底判真）。"""
    errs = []
    for key, value in _ctx(input_data).items():
        if key.startswith('node_') and key.endswith('_output') \
                and isinstance(value, dict) and value.get('success') is False:
            msg = value.get('error') or value.get('warning') or 'unknown node failure'
            if msg:
                errs.append('%s: %s' % (key, msg))
    return errs


def handle_hypo_persist(node_def: dict, input_data: dict) -> dict:
    """Persist：将本轮发现结果写入 discovery 表并结束运行。

    - 不存在 run_id 时自动创建（兼容直接调用 / 独立 DAG 场景）
    - 所有写入在同一事务内，任一步失败整体回滚并标记 failed
    - DE-6：零产物（无任何假设落库）不再假报 completed —— 置 failed 并透出
      上游节点失败原因，杜绝"工作台显示成功却无产物"的静默假成功。
    - DE-7：消费 cluster 节点的 cluster_assignments，把 cluster_id 写回假设。
    """
    ctx = _ctx(input_data)
    question = ctx.get('question') or ''
    if not question:
        return {'success': False, 'error': 'question is required'}
    hypotheses = _collect_upstream(input_data, 'judged_hypotheses') \
        or _collect_upstream(input_data, 'ranked_hypotheses') \
        or _collect_upstream(input_data, 'candidate_hypotheses')
    rounds = _collect_upstream(input_data, 'rounds') or []
    matches = _collect_upstream(input_data, 'matches') or []
    # DE-7：聚类归属表（{statement 归一键: cluster_id}），落库时并入假设记录
    cluster_assign = _collect_upstream(input_data, 'cluster_assignments') or {}
    if isinstance(cluster_assign, dict) and cluster_assign and hypotheses:
        for h in hypotheses:
            if isinstance(h, dict):
                cid = cluster_assign.get(_stmt_key(h.get('statement')))
                if cid:
                    h['cluster_id'] = cid

    run_id = ctx.get('run_id')
    created = []
    try:
        with m.get_db() as conn:
            if not run_id:
                run_id = md.create_run(
                    conn, question=question,
                    user_id=ctx.get('user_id'), project_id=ctx.get('project_id'))
            try:
                if hypotheses:
                    created = md.create_hypotheses(conn, run_id, hypotheses)
                    for h in created:
                        md.add_event(
                            conn, run_id=run_id, stage='persist',
                            status='info', detail={'hypothesis_id': h['id'],
                                                   'statement': h.get('statement')})
                # 讨论/对局按 statement 归属写入（N 条轮次 → N 条记录，非 N×M 交叉积）：
                # 仅对已落库的 created 行匹配；_stmt_key 与落库截断口径一致，超长假设不静默丢归属。
                by_stmt = {_stmt_key(c.get('statement')): c for c in created} \
                    if created else {}
                discussion_count = 0
                if by_stmt and rounds:
                    for r in rounds:
                        hh = by_stmt.get(_stmt_key(r.get('hypothesis')))
                        if not hh:
                            continue
                        md.add_discussion(
                            conn, hypothesis_id=hh['id'], protocol='discussion',
                            question=question, response=json.dumps(
                                r, ensure_ascii=False),
                            meta={'round': r.get('round')})
                        discussion_count += 1
                # P1-7 对局留痕：Elo 锦标赛非平局结果落 hypothesis_votes（statement 映射 real id）
                if by_stmt and matches:
                    for mt in matches:
                        w = by_stmt.get(_stmt_key(mt.get('winner')))
                        l = by_stmt.get(_stmt_key(mt.get('loser')))
                        if w and l:
                            # N2：DB 对局仅审计留痕（vote 行幂等去重，voter='engine'），
                            # 不再重放计分 —— Elo 以 judge 透传的锦标赛快照为准，杜绝双重计分。
                            md.record_vote(
                                conn, run_id=run_id, winner_id=w['id'],
                                loser_id=l['id'], voter='engine', update_elo=False)
                # P1-6 run 级核算：结构指标 + 本 run LLM 生产调用数落 discovery_runs.metrics
                # 注：计数为进程内近似值（sync 单请求精确；DAG 同进程串行时准确，
                # 并发多 run 同进程会串扰 —— 精确核算留待 M1 RunBudget）。
                md.update_run_metrics(
                    conn, run_id,
                    {'hypotheses': len(created or []),
                     'discussions': discussion_count,
                     'tournament_matches': len(matches or []),
                     'llm_calls': ds.llm_calls()})
                if created:
                    best = hypotheses[0]
                    md.save_memory(
                        conn, run_id=run_id, memory_type='best_hypothesis',
                        content=json.dumps({'statement': best.get('statement'),
                                            'score': best.get('score')},
                                           ensure_ascii=False))
                if not created:
                    # DE-6：零产物 = 失败而非假成功。收集上游失败根因透出，
                    # 使 async/DAG 工作台显示 failed 而非 completed。
                    up_errors = _collect_upstream_errors(input_data)
                    reason = 'no hypotheses produced'
                    if up_errors:
                        reason += '; upstream errors: %s' % ' | '.join(up_errors)
                    md.finish_run(conn, run_id, status='failed', error=reason)
                    md.add_event(conn, run_id=run_id, stage='failed',
                                 status='failed', detail={'error': reason})
                    conn.commit()
                    return {'success': False, 'error': reason, 'run_id': run_id}
                md.finish_run(conn, run_id, status='completed')
                md.add_event(conn, run_id=run_id, stage='completed',
                             status='info',
                             detail={'hypotheses': len(created or [])})
                conn.commit()
            except Exception as e:
                md.finish_run(conn, run_id, status='failed', error=str(e))
                conn.commit()
                logger.error('persist failed for run %s: %s', run_id, e)
                return {'success': False, 'error': str(e), 'run_id': run_id}
        return {'success': True, 'run_id': run_id,
                'hypotheses': len(created or [])}
    except Exception as e:
        logger.error('persist transaction failed: %s', e)
        return {'success': False, 'error': str(e)}


# ══════════════════════════════════════════════════════════════════
# 假设锦标赛工作流定义（WF1）
# ══════════════════════════════════════════════════════════════════

def _default_hypothesis_blueprint() -> dict:
    """从 workflows/hypothesis_tournament.json 加载蓝图，失败时返回内联兜底。"""
    try:
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            'workflows', 'hypothesis_tournament.json')
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get('nodes'):
            return data
    except Exception as e:
        logger.warning('hypothesis blueprint load failed, using fallback: %s', e)
    return {
        'nodes': [
            {'id': 'n1', 'type': 'veroscholar_discovery_gen',
             'name': 'Hypothesis Generation',
             'config': {'num_hypotheses': 5}},
            {'id': 'n2', 'type': 'veroscholar_discovery_evidence',
             'name': 'Evidence Recall', 'config': {}},
            {'id': 'n3', 'type': 'veroscholar_discovery_rank',
             'name': 'Hypothesis Ranking', 'config': {}},
            {'id': 'n4', 'type': 'veroscholar_discovery_cluster',
             'name': 'Cluster Hypotheses', 'config': {}},
            {'id': 'n5', 'type': 'veroscholar_discovery_discuss',
             'name': 'Hypothesis Discussion', 'config': {}},
            {'id': 'n6', 'type': 'veroscholar_discovery_judge',
             'name': 'Final Judgment', 'config': {}},
            {'id': 'n7', 'type': 'veroscholar_discovery_persist',
             'name': 'Persist Results', 'config': {}},
        ],
        'edges': [
            {'from': 'n1', 'to': 'n2'},
            {'from': 'n2', 'to': 'n3'},
            {'from': 'n3', 'to': 'n4'},
            {'from': 'n4', 'to': 'n5'},
            {'from': 'n5', 'to': 'n6'},
            {'from': 'n6', 'to': 'n7'},
        ],
        'settings': {'timeout_minutes': 60, 'max_concurrency': 1},
    }


def _get_or_create_hypothesis_workflow(conn) -> int:
    """按名称查找"假设发现锦标赛"工作流定义，不存在则插入。

    返回 workflow_definitions.id（BIGINT）。
    幂等：表已存在同名定义时直接复用；并发首次创建靠唯一索引兜底。
    """
    row = conn.execute(
        "SELECT id FROM workflow_definitions "
        "WHERE name = ? AND is_active = 1 "
        "ORDER BY id DESC LIMIT 1", ('Hypothesis Discovery Tournament',)).fetchone()
    if row:
        return int(row['id'])

    blueprint = _default_hypothesis_blueprint()
    row = conn.execute(
        'INSERT INTO workflow_definitions '
        '(name, description, version, is_active, agent_type, definition, '
        ' triggers, max_concurrency, timeout_minutes, on_error, created_by) '
        'VALUES (?, ?, 1, 1, \'system\', ?, ?, 1, 60, \'pause\', 0) '
        'RETURNING id',
        ('Hypothesis Discovery Tournament',
         'Generate → evidence recall → Elo rank → cluster → discuss → judge → persist',
         json.dumps(blueprint, ensure_ascii=False),
         json.dumps([], ensure_ascii=False))).fetchone()
    if row and row.get('id'):
        return int(row['id'])
    raise RuntimeError('failed to create hypothesis workflow definition')
