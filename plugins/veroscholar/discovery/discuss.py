#!/usr/bin/env python3
"""Discovery 假设讨论协议（v1.11.0）。

四个角色 + Elo 锦标赛：
  Generator    — 从研究问题发散候选假设
  Critic       — 对单条假设找漏洞与反证
  Evolver      — 依据批判改进假设
  Comparator   — 两两对比，供 Elo 计分

LLM 调用统一走 `_chat()`：
  - 测试环境通过 `LLM_FACTORY` 注入替身（纯函数替换，不 mock 全局模块）
  - 生产环境走 `agent_matrix.model_resolver.resolve_model_args`（tier 策略）
    + `agent_matrix.engine.UnifiedLLM`（与 stock_analysis.discuss_research 同款范式）
  - `model → model_name` 映射为 H-1 修正：兼容配置仅提供 model 的场景
"""

import json
import logging

logger = logging.getLogger('veroscholar.discovery.discuss')

# 测试注入缝：`LLM_FACTORY(protocol, question, prompt, tier, temperature) -> str`
LLM_FACTORY = None

# LLM 调用计数（run 级成本核算；仅生产路径计次，测试注入不计数）
_llm_calls = 0
# 最近一次 LLM 错误摘要（P2-8 异常可见性：`_chat` 仍返回 ''，但保留根因供调用方透出）
_last_error = ''


def reset_llm_calls() -> None:
    """清零调用计数（每次发现运行开始时调用）。"""
    global _llm_calls, _last_error
    _llm_calls = 0
    _last_error = ''


def llm_calls() -> int:
    """返回自最近一次 reset 以来的 LLM 生产调用次数。"""
    return _llm_calls


def last_error() -> str:
    """返回自最近一次 reset 以来的最近一次 LLM 错误摘要（无错误时为空串）。"""
    return _last_error

_TIER_DEFAULT = 'economy'
_TIERS = ('fast', 'economy', 'balanced', 'quality')

# 关键节点从 LLM JSON 中提取第一个合法 JSON 对象（容忍 markdown 围栏与前后噪音）
_JSON_START = ('{', '[')


def _chat(protocol: str, question: str, prompt: str, tier: str,
          temperature: float = 0.7) -> str:
    """调用 LLM 并返回原始文本。"""
    if LLM_FACTORY is not None:
        return LLM_FACTORY(protocol, question, prompt, tier, temperature)
    # ── 生产路径 ──
    def _fail(msg: str) -> str:
        global _last_error
        _last_error = str(msg)
        logger.error('chat failed protocol=%s tier=%s: %s', protocol, tier, msg)
        return ''
    try:
        from agent_matrix.model_resolver import resolve_model_args
        from agent_matrix.engine import UnifiedLLM
    except Exception as e:  # agent_matrix 不可用时不阻断，降级返回空
        return _fail('LLM modules unavailable: %s' % e)
    try:
        cfg = resolve_model_args({'strategy': 'tier', 'tier': tier})
        # H-1：tier 命中时返回 provider_model_id（UnifiedLLM 原生支持）；
        # 未命中时 cfg 仅含 model，需映射为 model_name。
        if not cfg.get('model_name') and cfg.get('model'):
            cfg['model_name'] = cfg.pop('model')
        if not (cfg.get('provider_model_id') or cfg.get('model_name')):
            return _fail('cannot resolve model for tier=%s' % tier)
        llm = UnifiedLLM(config=cfg)
    except Exception as e:
        return _fail('%s: %s' % (type(e).__name__, e))
    # 生产路径 LLM 调用（DE-4b）：
    # - 显式 max_tokens=16384：默认 4096 常把长 JSON 结构输出截断成空/残缺
    # - 空响应或异常重试一次（仅生产路径；测试注入 LLM_FACTORY 不走此逻辑，
    #   保持"单次调用"的 mock 语义）。每 attempt 各计一次 llm_calls。
    global _llm_calls
    last_exc = None
    for _attempt in (1, 2):
        _llm_calls += 1
        try:
            raw = llm.chat(
                messages=[{'role': 'user', 'content': prompt}],
                temperature=temperature, module='veroscholar',
                max_tokens=16384) or ''
        except Exception as e:
            last_exc = e
            if _attempt == 1:
                continue  # 服务/网络瞬时故障重试一次
            break
        if raw.strip():
            return raw
        # 空响应：再试一次后仍空才判定失败
    if last_exc is not None:
        return _fail('%s: %s' % (type(last_exc).__name__, last_exc))
    return _fail('empty LLM response after retry (tier=%s)' % tier)


# ══════════════════════════════════════════════════════════════════
# 角色函数
# ══════════════════════════════════════════════════════════════════

def generate_hypotheses(question: str, n: int = 5, tier: str = None) -> list:
    """Generator：从研究问题生成 n 条候选假设。

    返回 [{statement, score, context, variables, relationship,
          null_hypothesis, falsification_test}]；LLM 失败/JSON 解析失败时返回 []。
    """
    tier = tier or _TIER_DEFAULT
    prompt = (
        f'You are a research advisor. Given the research question below, '
        f'generate exactly {n} distinct, falsifiable scientific hypotheses. '
        f'Each should be concrete, testable and non-trivial. For every one, '
        f'also provide the key variables, the proposed relationship, the null '
        f'hypothesis, and a falsification test.\n\n'
        f'Research question: {question}\n\n'
        f'Respond ONLY with a JSON object in this exact shape:\n'
        f'{{"hypotheses": [{{"statement": "...", "score": 0.7, '
        f'"context": "...", "variables": ["v1", "v2"], '
        f'"relationship": "IV predicts DV", '
        f'"null_hypothesis": "...", "falsification_test": "..."}}]}}\n'
        f'Use a score between 0 and 1 indicating expected plausibility.'
    )
    raw = _chat('generate', question, prompt, tier)
    data = _extract_json(raw)
    if not isinstance(data, dict) or 'hypotheses' not in data:
        return []
    out = []
    for item in data.get('hypotheses') or []:
        stmt = str(item.get('statement') or '').strip()
        if not stmt:
            continue
        try:
            score = float(item.get('score') or 0.5)
        except (TypeError, ValueError):
            score = 0.5
        out.append({
            'statement': stmt,
            'score': max(0.0, min(1.0, score)),
            'context': str(item.get('context') or ''),
            'variables': [str(v) for v in (item.get('variables') or []) if v],
            'relationship': str(item.get('relationship') or ''),
            'null_hypothesis': str(item.get('null_hypothesis') or ''),
            'falsification_test': str(item.get('falsification_test') or ''),
        })
    return out[:n]


def critique_hypothesis(hypothesis: dict, evidence: list, tier: str = None) -> dict:
    """Critic：对假设提出漏洞与反证。

    返回 {critique, weaknesses: [str]}；失败时返回空批判（不阻断）。
    """
    tier = tier or _TIER_DEFAULT
    evidence_txt = '\n'.join('- ' + str(e.get('title') or e.get('text') or '')
                             for e in (evidence or [])[:8]) or 'No evidence provided.'
    prompt = (
        f'You are a skeptical research critic. Evaluate the following hypothesis '
        f'and identify its weakest points and counter-evidence.\n\n'
        f'Hypothesis: {hypothesis.get("statement", "")}\n'
        f'Relevant evidence:\n{evidence_txt}\n\n'
        f'Respond ONLY with a JSON object in this exact shape:\n'
        f'{{"critique": "...", "weaknesses": ["..."]}}'
    )
    raw = _chat('critique', hypothesis.get('statement', ''), prompt, tier)
    data = _extract_json(raw)
    if not isinstance(data, dict):
        return {'critique': '', 'weaknesses': []}
    return {
        'critique': str(data.get('critique') or ''),
        'weaknesses': [str(w) for w in (data.get('weaknesses') or []) if w],
    }


def evolve_hypothesis(hypothesis: dict, critique: dict, tier: str = None) -> dict:
    """Evolver：结合批判改进假设。

    返回 {statement, rationale}；失败时返回原假设（降级保底）。
    """
    tier = tier or _TIER_DEFAULT
    prompt = (
        f'You are an experimental designer. Improve the following hypothesis by '
        f'addressing the given weaknesses. Keep it concrete and testable.\n\n'
        f'Original hypothesis: {hypothesis.get("statement", "")}\n'
        f'Critique: {critique.get("critique", "")}\n'
        f'Weaknesses: {", ".join(critique.get("weaknesses") or [])}\n\n'
        f'Respond ONLY with a JSON object in this exact shape:\n'
        f'{{"statement": "...", "rationale": "..."}}'
    )
    raw = _chat('evolve', hypothesis.get('statement', ''), prompt, tier)
    data = _extract_json(raw)
    if isinstance(data, dict) and str(data.get('statement') or '').strip():
        return {
            'statement': str(data['statement']).strip()[:2000],
            'rationale': str(data.get('rationale') or ''),
        }
    return {'statement': hypothesis.get('statement', ''),
            'rationale': 'Evolver produced no valid improvement; kept original.'}


def compare(h1: dict, h2: dict, question: str, tier: str = None) -> dict:
    """Comparator：对比两条假设，返回 {winner: 1|2, reason}。

    返回 winner 为 0 表示平局/不可判定（不触发 Elo 更新）。
    """
    tier = tier or _TIER_DEFAULT
    prompt = (
        f'You are a research prioritization judge. Given the research question, '
        f'decide which hypothesis is stronger: more novel, more testable and '
        f'more impactful.\n\n'
        f'Research question: {question}\n'
        f'Hypothesis A: {h1.get("statement", "")}\n'
        f'Hypothesis B: {h2.get("statement", "")}\n\n'
        f'Respond ONLY with a JSON object in this exact shape:\n'
        f'{{"winner": 1, "reason": "..."}}'
        f'  (winner must be 1, 2 or 0 for a tie)'
    )
    raw = _chat('compare', question, prompt, tier)
    data = _extract_json(raw)
    if not isinstance(data, dict):
        return {'winner': 0, 'reason': 'Judge returned no valid verdict.'}
    try:
        winner = int(data.get('winner', 0))
    except (TypeError, ValueError):
        winner = 0
    if winner not in (1, 2, 0):
        winner = 0
    return {'winner': winner, 'reason': str(data.get('reason') or '')}


def review_hypothesis(h: dict, question: str, tier: str = None) -> dict:
    """Judge：对单条假设独立评分（0-1）+ 理由（可证伪性/新颖性/可检验/影响力）。

    返回 {score, rationale}；LLM/解析失败时返回 score=0.5（不抛异常）。
    """
    tier = tier or _TIER_DEFAULT
    prompt = (
        f'You are a rigorous research judge. Rate the hypothesis below on a '
        f'0-1 scale for testability, novelty and soundness, and give a concise '
        f'reason.\n\n'
        f'Research question: {question}\n'
        f'Hypothesis: {h.get("statement", "")}\n\n'
        f'Respond ONLY with a JSON object in this exact shape:\n'
        f'{{"score": 0.7, "rationale": "..."}}'
    )
    raw = _chat('review', question, prompt, tier)
    data = _extract_json(raw)
    if not isinstance(data, dict):
        return {'score': 0.5, 'rationale': ''}
    try:
        score = float(data.get('score', 0.5))
    except (TypeError, ValueError):
        score = 0.5
    return {'score': max(0.0, min(1.0, score)),
            'rationale': str(data.get('rationale') or '')}


# ══════════════════════════════════════════════════════════════════
# Elo 锦标赛
# ══════════════════════════════════════════════════════════════════

def elo_tournament(hypotheses: list, question: str, n_rounds: int = 3,
                   tier: str = None, match_sink: callable = None) -> list:
    """对假设列表执行若干轮两两对比，返回带 elo_rating 的新列表。

    - 参赛不足 2 条时直接返回（elo_rating 保持 1000）
    - 每轮随机洗牌后相邻两两 PK；出现 0 平局则该局不计分
    - 失败降级：任何异常仅跳过该局，不中断整体
    - match_sink(winner_dict, loser_dict) 可选：非平局时回调（用于对局留痕），不影响排序
    """
    import random
    tier = tier or _TIER_DEFAULT
    ranked = [dict(h) for h in hypotheses]
    for h in ranked:
        h.setdefault('elo_rating', 1000.0)
    if len(ranked) < 2:
        return ranked

    def _elo(wa, la):
        ew = 1.0 / (1.0 + 10 ** ((la - wa) / 400.0))
        return 32.0 * (1.0 - ew)

    for _ in range(max(1, int(n_rounds))):
        random.shuffle(ranked)
        for i in range(0, len(ranked) - 1, 2):
            a, b = ranked[i], ranked[i + 1]
            try:
                verdict = compare(a, b, question, tier)
                wa = float(a.get('elo_rating') or 1000.0)
                wb = float(b.get('elo_rating') or 1000.0)
                if verdict.get('winner') == 1:
                    a['elo_rating'] = round(wa + _elo(wa, wb), 1)
                    b['elo_rating'] = round(wb - _elo(wb, wa), 1)
                    if match_sink:
                        match_sink(a, b)
                elif verdict.get('winner') == 2:
                    b['elo_rating'] = round(wb + _elo(wb, wa), 1)
                    a['elo_rating'] = round(wa - _elo(wa, wb), 1)
                    if match_sink:
                        match_sink(b, a)
                # winner == 0 → 平局，双方不更新
            except Exception as e:
                logger.warning('tournament round skipped: %s', e)
    ranked.sort(key=lambda h: h.get('elo_rating', 0.0), reverse=True)
    return ranked


# ══════════════════════════════════════════════════════════════════
# JSON 容错解析
# ══════════════════════════════════════════════════════════════════

def _extract_json(raw) -> dict:
    """从 LLM 输出提取第一个合法 JSON 对象。

    容忍 markdown 代码围栏（```json ... ```）、前后解释性文本、BOM。
    解析失败返回 {}（调用方各自降级）。
    """
    if not raw:
        return {}
    text = str(raw).strip().lstrip('\ufeff')
    for ch in _JSON_START:
        start = text.find(ch)
        if start < 0:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == '\\':
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c in ('{', '['):
                depth += 1
            elif c in ('}', ']'):
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except ValueError:
                        break
    return {}
