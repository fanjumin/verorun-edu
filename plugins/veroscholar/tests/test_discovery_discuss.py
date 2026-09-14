#!/usr/bin/env python3
"""test_discovery_discuss.py — Discovery 假设讨论协议单元测试。

覆盖 discuss.py 的四个角色（Generator/Critic/Evolver/Comparator）、
Elo 锦标赛与 JSON 容错解析。LLM 调用通过 `LLM_FACTORY` 注入替身，
不 mock 全局模块、不连数据库。

运行:
    cd F:\\Sites\\VeroRun
    python -m unittest plugins.veroscholar.tests.test_discovery_discuss -v
"""

import sys
import os
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from plugins.veroscholar.discovery import discuss as ds


class LLMTestCase(unittest.TestCase):
    """注入 LLM_FACTORY 的基类：setUp 保存现场，tearDown 恢复。"""

    def setUp(self):
        self._real_factory = ds.LLM_FACTORY
        ds.LLM_FACTORY = None

    def tearDown(self):
        ds.LLM_FACTORY = self._real_factory


class TestExtractJson(unittest.TestCase):
    def test_empty_returns_empty_dict(self):
        self.assertEqual(ds._extract_json(''), {})
        self.assertEqual(ds._extract_json(None), {})

    def test_plain_json_object(self):
        self.assertEqual(ds._extract_json('{"a": 1}'), {'a': 1})

    def test_markdown_fence_and_noise(self):
        raw = 'Here is the result:\n```json\n{"hypotheses": []}\n```\nDone.'
        self.assertEqual(ds._extract_json(raw), {'hypotheses': []})

    def test_bom_and_leading_text(self):
        raw = '\ufeffSome text then {"x": 2} trailing'
        self.assertEqual(ds._extract_json(raw), {'x': 2})

    def test_invalid_json_returns_empty(self):
        self.assertEqual(ds._extract_json('{broken json'), {})
        self.assertEqual(ds._extract_json('no json at all'), {})

    def test_array_top_level(self):
        self.assertEqual(ds._extract_json('[1, 2, 3]'), [1, 2, 3])


class TestGenerateHypotheses(LLMTestCase):
    def test_parses_and_clamps_scores(self):
        def factory(protocol, question, prompt, tier, temperature):
            self.assertEqual(protocol, 'generate')
            self.assertEqual(question, 'Q')
            self.assertIn('generate exactly 3 distinct', prompt)
            self.assertEqual(tier, 'economy')
            return ('{"hypotheses": ['
                    '{"statement": "H1", "score": 1.7},'
                    '{"statement": "H2", "score": -0.2},'
                    '{"statement": "H3"}]}')
        ds.LLM_FACTORY = factory
        out = ds.generate_hypotheses('Q', n=3)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]['score'], 1.0)  # 上溢钳位
        self.assertEqual(out[1]['score'], 0.0)  # 下溢钳位
        self.assertEqual(out[2]['score'], 0.5)  # 缺省 0.5

    def test_blank_statements_skipped(self):
        ds.LLM_FACTORY = lambda *a, **k: (
            '{"hypotheses": [{"statement": "  "}, {"statement": "OK"}]}')
        out = ds.generate_hypotheses('Q')
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['statement'], 'OK')

    def test_llm_empty_returns_empty(self):
        ds.LLM_FACTORY = lambda *a, **k: ''
        self.assertEqual(ds.generate_hypotheses('Q'), [])

    def test_bad_json_returns_empty(self):
        ds.LLM_FACTORY = lambda *a, **k: 'not json at all'
        self.assertEqual(ds.generate_hypotheses('Q'), [])

    def test_truncates_to_n(self):
        def factory(protocol, question, prompt, tier, temperature):
            items = ','.join('{"statement": "H%d"}' % i for i in range(6))
            return '{"hypotheses": [%s]}' % items
        ds.LLM_FACTORY = factory
        out = ds.generate_hypotheses('Q', n=4)
        self.assertEqual(len(out), 4)

    def test_custom_tier_passed(self):
        seen = {}
        def factory(protocol, question, prompt, tier, temperature):
            seen['tier'] = tier
            return '{"hypotheses": []}'
        ds.LLM_FACTORY = factory
        ds.generate_hypotheses('Q', n=1, tier='quality')
        self.assertEqual(seen['tier'], 'quality')


class TestCritique(LLMTestCase):
    def test_parses_critique_and_filters_blanks(self):
        def factory(protocol, question, prompt, tier, temperature):
            self.assertEqual(protocol, 'critique')
            self.assertIn('Hypothesis: H1', prompt)
            self.assertIn('Relevant evidence', prompt)
            return '{"critique": "weak", "weaknesses": ["w1", "w2", ""]}'
        ds.LLM_FACTORY = factory
        out = ds.critique_hypothesis({'statement': 'H1'}, [{'title': 'E1'}])
        self.assertEqual(out['critique'], 'weak')
        self.assertEqual(out['weaknesses'], ['w1', 'w2'])  # 空串剔除

    def test_evidence_fallback_text(self):
        seen = {}
        def factory(protocol, question, prompt, tier, temperature):
            seen['prompt'] = prompt
            return '{"critique": "", "weaknesses": []}'
        ds.LLM_FACTORY = factory
        out = ds.critique_hypothesis({'statement': 'H1'}, [])
        self.assertIn('No evidence provided.', seen['prompt'])
        self.assertEqual(out, {'critique': '', 'weaknesses': []})

    def test_bad_json_returns_empty_critique(self):
        ds.LLM_FACTORY = lambda *a, **k: 'garbage'
        out = ds.critique_hypothesis({'statement': 'H1'}, [])
        self.assertEqual(out, {'critique': '', 'weaknesses': []})


class TestEvolve(LLMTestCase):
    def test_valid_improvement(self):
        def factory(protocol, question, prompt, tier, temperature):
            self.assertEqual(protocol, 'evolve')
            self.assertIn('Original hypothesis: Original', prompt)
            return '{"statement": "Improved", "rationale": "because"}'
        ds.LLM_FACTORY = factory
        out = ds.evolve_hypothesis({'statement': 'Original'},
                                   {'critique': 'C', 'weaknesses': ['w']})
        self.assertEqual(out['statement'], 'Improved')
        self.assertEqual(out['rationale'], 'because')

    def test_invalid_returns_original(self):
        ds.LLM_FACTORY = lambda *a, **k: 'garbage'
        out = ds.evolve_hypothesis({'statement': 'Original'}, {'critique': 'C'})
        self.assertEqual(out['statement'], 'Original')
        self.assertIn('kept original', out['rationale'])

    def test_blank_statement_returns_original(self):
        ds.LLM_FACTORY = lambda *a, **k: '{"statement": "  ", "rationale": "r"}'
        out = ds.evolve_hypothesis({'statement': 'Original'}, {'critique': 'C'})
        self.assertEqual(out['statement'], 'Original')


class TestCompare(LLMTestCase):
    def test_winner_1(self):
        ds.LLM_FACTORY = lambda *a, **k: '{"winner": 1, "reason": "A better"}'
        out = ds.compare({'statement': 'A'}, {'statement': 'B'}, 'Q')
        self.assertEqual(out['winner'], 1)
        self.assertEqual(out['reason'], 'A better')

    def test_winner_2(self):
        ds.LLM_FACTORY = lambda *a, **k: '{"winner": 2, "reason": "B better"}'
        out = ds.compare({'statement': 'A'}, {'statement': 'B'}, 'Q')
        self.assertEqual(out['winner'], 2)

    def test_invalid_winner_returns_tie(self):
        ds.LLM_FACTORY = lambda *a, **k: '{"winner": 5, "reason": "x"}'
        out = ds.compare({'statement': 'A'}, {'statement': 'B'}, 'Q')
        self.assertEqual(out['winner'], 0)

    def test_bad_json_returns_tie(self):
        ds.LLM_FACTORY = lambda *a, **k: 'garbage'
        out = ds.compare({'statement': 'A'}, {'statement': 'B'}, 'Q')
        self.assertEqual(out['winner'], 0)


class TestEloTournament(unittest.TestCase):
    def test_less_than_two_returns_unchanged(self):
        out = ds.elo_tournament([{'statement': 'A'}], 'Q', n_rounds=2)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['elo_rating'], 1000.0)

    def test_empty_returns_empty(self):
        self.assertEqual(ds.elo_tournament([], 'Q'), [])

    def test_winner_updates_ratings_and_sorts(self):
        with mock.patch.object(ds, 'compare',
                               return_value={'winner': 1, 'reason': 'r'}) as mc:
            out = ds.elo_tournament(
                [{'statement': 'A', 'elo_rating': 1000.0},
                 {'statement': 'B', 'elo_rating': 1000.0}],
                'Q', n_rounds=1)
        mc.assert_called_once()
        self.assertEqual(len(out), 2)
        # 胜者 rating 提升、败者降低；按 Elo 降序返回
        self.assertGreater(out[0]['elo_rating'], out[1]['elo_rating'])
        self.assertNotEqual(out[0]['elo_rating'], out[1]['elo_rating'])

    def test_tie_does_not_update(self):
        with mock.patch.object(ds, 'compare',
                               return_value={'winner': 0, 'reason': 'tie'}):
            out = ds.elo_tournament(
                [{'statement': 'A'}, {'statement': 'B'}], 'Q', n_rounds=1)
        self.assertEqual(out[0]['elo_rating'], 1000.0)
        self.assertEqual(out[1]['elo_rating'], 1000.0)

    def test_exception_skips_pair(self):
        def boom(*a, **k):
            raise RuntimeError('llm down')
        with mock.patch.object(ds, 'compare', side_effect=boom), \
                self.assertLogs(ds.logger, level='WARNING'):
            out = ds.elo_tournament(
                [{'statement': 'A'}, {'statement': 'B'}], 'Q', n_rounds=1)
        self.assertEqual(len(out), 2)


if __name__ == '__main__':
    unittest.main()
