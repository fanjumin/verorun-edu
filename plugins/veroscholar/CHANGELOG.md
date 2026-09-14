# Changelog

## v1.11.3 — 2026-09-10

### Fixes

- DE-5: `auto` 触发在 workflow 引擎不可用时不再阻塞降级为 sync（整条 LLM 流水线实测 >322s 被网关超时截断，前端误报失败而后台仍在执行）。新增 `_background_generate`：先建 run、由 daemon 线程执行流水线、立即返回 `{mode:'background', run_id, status:'running'}`；节点流水线抽为 `_run_pipeline`（不触碰 Flask request/g，线程安全），sync 路径复用之。前端 discovery.html 对 background/dag 模式按 run_id 轮询运行详情直至终态再刷新（5s 间隔）。`mode:'sync'` 显式同步保留为调试/测试专用

### Tests

- test_discovery_routes：降级测试语义更新（sync → background）；新增后台 worker 调度断言与 `_run_pipeline` 内部异常落 failed 不逃逸回归（23 项全绿，discovery 三套共 75 项全绿）

## v1.11.2 — 2026-09-09

### Fixes

- DE-6: persist 零产物不再假报 completed —— DAG/同步路径无任何假设落库时统一置 `run=failed` 并透出上游各节点失败原因（`_collect_upstream_errors`），杜绝"工作台显示成功却零产物"的静默假成功；persist 现统一写 run metrics（结构指标 + `llm_calls`，DAG 与 sync 一致）；async(DAG) 触发前重置 `ds.reset_llm_calls()` 作 run 级核算起点
- DE-7: cluster 节点返回 `cluster_assignments`，persist 消费并把 `cluster_id` 写回 hypotheses；sync 路径此前丢弃 rank 的 matches 与 cluster 的聚类归属（→ hypothesis_votes 0 行 + cluster_id 全空），现注入 ctx
- DE-4b: `discuss._chat` 生产路径显式 `max_tokens=16384`（默认 4096 易截断长 JSON 结构输出），空响应/瞬时异常各重试一次（仅生产路径；`LLM_FACTORY` 注入测试保持单次调用语义，mock 精度不受影响）
- DE-3: 清理 `v1.1.1_lib.sql` 注释中的裸 `%q`（psycopg2 参数化时误当占位符报 unsupported format character）；共享 `plugins/_base/db.py` 改为仅在显式传参时做参数化绑定（params=None 直接执行），根治同类迁移注释/字面量问题

#### 说明（修正 v1.11.1 已知边界）

- v1.11.1 所述"async(DAG) 暂不写 metrics.llm_calls"自本版起过时：persist 双路径统一写入。计数仍为进程内近似（并发多 run 同进程串扰 / 跨进程不精确），token 级精确核算仍留 M1 RunBudget

### Tests

- test_discovery_nodes：DE-6 空产物置 failed（原 P1-2 测试同步语义更新，仍保证无 NameError）；新增 DAG 形态上游错误聚合回归、DE-7 cluster_id 回写回归、llm_calls metric 落库回归

## v1.11.1 — 2026-09-09

### Fixes

- N1: judge 节点透传锦标赛 `elo_rating`（persist 落库即真实排名），修复 `hypotheses.elo_rating` 与 `list_hypotheses/list_ranked` 排序失真（第二轮复测 N1）
- N2: 引擎对局落库改为纯审计留痕（`record_vote(update_elo=False)`，vote 行幂等去重 + `voter='engine'`，不再重放计分）—— Elo 以 judge 透传的锦标赛快照为唯一计分权威，杜绝双重叠加；人工投票（`/vote`）保持原有实时计分语义（第二轮复测 N2）
- N4: persist 讨论/对局按 statement 归属统一 `_stmt_key`（strip + 2000 与落库截断同口径），超 2000 字符假设不再静默丢失讨论/对局（第二轮复测 N4）
- N5: 修正上一版 CHANGELOG 措辞（judge 评分实际走 `ds.review_hypothesis`，非 `ds.compare`）（第二轮复测 N5）
- 版本号同步 1.11.1

#### 已知边界（M1 预留）

- `discuss._llm_calls` 为模块级无锁全局计数（N3）：多线程并发 run 计数可能串扰，且 async(DAG) 跨进程不可精确、暂不写 metrics.llm_calls —— M1 改 `RunBudget` 实例化传递（含加锁）

### Tests

- test_discovery_nodes：judge Elo 透传断言；persist 锦标赛 Elo 落库 + 对局审计化回归；超长 statement 讨论不丢回归
- test_discovery_models：`record_vote(update_elo=False)` 不触发 Elo 重算回归

## v1.11.0 — 2026-09-09

### Features

- W1: 新增假设发现引擎（Discovery Engine，M0）：从研究问题出发，经「生成候选假设 → 多源证据召回 → Elo 锦标赛排序 → 关键词聚类 → 三轮推演（生成/批判/进化）→ 最终裁决 → 落库」流水线产出**候选假设集合 + Elo 排名 + 推演记录**（见"已知边界"——当前产物为候选集，不声称具备结构化可证伪检验）
- W2: 新增 6 张发现数据表（`discovery_runs` / `hypotheses` / `hypothesis_votes` / `hypothesis_discussions` / `discovery_events` / `discovery_memory`，迁移 `v1.4.0_discovery.sql`，全部幂等、无 pgvector 依赖）；支持 7 个自定义 DAG 节点（`veroscholar_discovery_*`）与「假设发现锦标赛」工作流蓝图
- W3: 新增发现工作台页面 `GET /admin/veroscholar/discovery`（hub 新增「发现引擎」页签）+ 8 组 RESTful API（`/api/v1/discovery/*`）；同步执行与 DAG 异步执行双路径（**异步依赖 orchestrator 引擎可用**），引擎不可用时自动降级同步
- W4: 新增 4 个配置项（`discovery_enabled` 默认关闭 / `discovery_per_source` / `discovery_elo_rounds` / `hypothesis_model_tier`）与 7 项 capability；M1 预算/回流配置键预埋
- LLM 统一走 `agent_matrix` tier 模型解析 + UnifiedLLM（`model → model_name` 映射，消息记账 `module='veroscholar'`），与 stock_analysis 讨论协议同款范式

### Fixes

- i18n: 新增 25 个双语键（en/zh-CN），含发现引擎页面与 JS 文案
- P0: persist 讨论记录按假设归属写入（仅落库假设 + 按 statement 匹配轮次），杜绝 `hypothesis_id=NULL` 触发 NOT NULL/FK 完整性错误与 N×M 交叉积
- P0: `_get_or_create_hypothesis_workflow` INSERT 分支改链式 `conn.execute(...).fetchone()`，适配 `PgConnection`（无独立 fetchone）
- discovery 节点统一包内导入 `from . import discuss as ds`（修复 `from ..discuss` 模块导入失效导致的永久降级）；persist 预置 `created=[]`（空假设不再 NameError）
- 发现工作台 stats API 与 evidence 节点补 `discovery_enabled` 门禁（关闭态零副作用）
- P2: 异步 DAG 失败路径标记已建 run 为 `failed`（清理无引擎/异常时的孤儿 `running` 记录）
- P2: evidence 全文召回对 `%`/`_`/`\` 做 LIKE 通配符转义（`ESCAPE '\'`），杜绝模式注入与误匹配
- P2: plugin.json 版本号同步为 1.11.0（与代码/CHANGELOG 对齐）
- P1-5(M0): `hypotheses` 落 `context/variables/relationship/null_hypothesis/falsification_test` 结构化字段并全链路透传；LLM 生成 prompt 一并产出，管理界面「发现引擎」页展示（未建 evidence_links 精度表）
- P2-7: judge 弃用"自身 vs 自身+baseline"自我比较（恒 +0.05 虚高），改 `ds.review_hypothesis` 对单条假设独立 0–1 评分 + 理由
- P1-7: Elo 锦标赛非平局对局落 `hypothesis_votes`（`voter='engine'`，rank 节点经 `match_sink` 采集、persist 按 statement 映射真实 id 入库），引擎自动对局留痕
- P1-6: `discovery_runs` 新增 `metrics` 列（JSON）；persist 落结构指标（hypotheses/discussions/tournament_matches），sync 路径并入生产 LLM 调用计数（`ds.llm_calls()`）
- P2-8: `_chat` 生产异常时保留最近错误摘要（`ds.last_error()`，返回仍为 ''），`handle_hypo_gen` 失败时经 `warning` 透出根因（区分"建模失败"与"禁用"）
- P2-3: 迁移 JSON 列改 `JSONB`（`discovery_runs.config/metrics`、`hypotheses.variables/evidence_ids`、`hypothesis_discussions.meta`、`discovery_events.detail`），写侧经 `_jsonb()` 适配 psycopg2 原生类型/回退字符串两态

#### 已知边界（M0 与方案差异，M1 预留，如实标注）

- **结构化字段已落地，但全链路 provenance 未落地**：`hypotheses` 已含 context/variables/relationship/null_hypothesis/falsification_test；`evidence_ids` 当前仍恒空、无 evidence_links 表，`falsification_test` 仅文本未做自动校验 —— 幻觉可证性压降有限
- **LLM 成本核算为结构指标 + 调用次数，非 token 级**：`metrics` 记录 hypotheses/discussions/tournament_matches/llm_calls；sync 路径 llm_calls 精确（单请求内计数），async(DAG) 路径因跨进程全局计数不可精确、暂不写 llm_calls；token 级账仍需预算重构
- **Elo 对局已入库，但复盘/回放 UI 未做**：`hypothesis_votes` 记录引擎对局（voter='engine'），尚无工作台复盘视图

### Tests

- 新增 4 个测试模块（test_discovery_models / test_discovery_nodes / test_discovery_discuss / test_discovery_routes），数据层/节点/协议/路由全覆盖

## v1.10.0 — 2026-09-02

### Features

- W1: DAG 综述生成节点现将综述正文回写 `reviews` 表（DOI 反查关联论文，`created_by` 取上下文 user_id）；回写失败仅告警不阻断工作流。生成的综述不再只存在于工作流实例，用户可在综述列表看到
- W2: 全文模块补齐删除路径 `DELETE /api/v1/papers/<paper_id>/fulltext/<file_id>`（chunks 经 ON DELETE CASCADE 级联清理，WHERE 携带 paper_id 防越权）；`file_id` 纳入蓝图 UUID 前置校验（非法 → 400 而非 500）；论文详情抽屉新增「全文」区块：PDF 上传（走 `VS.authHeaders()` 原样 fetch，保留 multipart boundary）、文件列表与逐文件删除、分块预览
- W3: 多源检索新增 `per_source` 每源配额，综述蓝图升至「文献综述生成 v5」并配置 `per_source=8`，后位中文源（openalex_zh）不再被英文源挤出去重窗口

### Fixes

- i18n: 新增 10 个双语键（en/zh-CN），含全文抽屉 UI 文案与空态提示

## v1.9.1 — 2026-09-02

### Fixes

- Fix (P0): AI endpoints returned 503 on every call — services/ai.py invoked UnifiedLLM().chat() without provider/model, and _resolve_model raises 'Cannot resolve model' when both are absent. Now resolves the platform default from system_config (ai_text_provider/ai_text_model, same convention as shop_ai/cleaner_ai) with deepseek-chat fallback; 4 regression tests added (105 total)

## v1.9.0 — 2026-09-01

### Features

- B (fulltext, B1): PDF full-text pipeline — upload (`POST /api/v1/papers/<id>/fulltext`) → pypdf parse → paragraph chunking (≤1200 chars, page-tracked) → chunk browse (`GET`, limit/offset); idempotent by (paper_id, sha256)
- Paper Q&A now injects top-3 relevant full-text excerpts via `veroscholar.qa_context` filter hook (registered in on_enable after migration; failures degrade silently); papers without fulltext behave byte-identically to v1.8
- New migration `v1.3.0_fulltext.sql` (fulltext_files + fulltext_chunks; no pgvector dependency — embedding column deferred to B2)
- pypdf added to `python_dependencies.required`; environments without pypdf get a 503 on upload with all other features unaffected
- i18n: 8 bilingual error-path strings (en/zh-CN)

## v1.8.0 — 2026-09-01

### Features

- A (zhsrc): New Chinese literature source `openalex_zh` — OpenAlex works filtered by `language:zh` (~5M indexed Chinese works incl. CMJ series); no-DOI Chinese works are kept (dedup falls back to title+year)
- Settings toggle `zh_enabled` (default on); search page checkbox "OpenAlex (Chinese)", library source filter and amber source badge
- Literature review blueprint bumped to "文献综述生成 v3" with `openalex_zh` in the search node; all legacy definitions auto-deactivated (generalized matching)

## v1.7.0 — 2026-09-01

### Features

- B1: Citation verification now uses Crossref/DataCite online API (5‑state: registered/retracted/concern/unregistered/offline) with 7‑day doi_checks cache; arXiv DOI routed to DataCite automatically
- B2: Literature review DAG n3 dedup‑sort node is now deterministic pure‑Python (veroscholar_dedup) instead of LLM ai_process
- B3: Search API accepts year_from/year_to/min_citations/venue filters (post‑apply); each search is logged to search_strategies table and returns strategy_id

### Fixes

- B4: arXiv papers export BibTeX @misc + archivePrefix/eprint and RIS TY – RPRT + DB – arXiv; journal papers unchanged (regression‑protected)

## v1.6.0 — 2026-09-01

### Changes

- Fix (P0): `services/ai.py` relative import `from . import models` → `from .. import models` — chat / translate / related were 500 `cannot import name 'models'` at call time
- Fix (P2): validate paper/project/review uuid path params (invalid → 400) and sanitize 500 error bodies (no raw SQL / exception text leak)
- Add regression tests `tests/test_services.py` that exercise the real `ai.chat_answer` / `translate` / `related_papers` bodies
- Version bump from v1.5.0

## v1.5.0 — 2026-08-31

### Features

- Add tag system: per-paper tags with idempotent add / remove (`GET/POST /api/v1/papers/<id>/tags`, `DELETE /api/v1/papers/<id>/tags/<tag_id>`, `GET /api/v1/tags`)
- Add reading status (unread / reading / read): `PATCH /api/v1/papers/<id>/status`, status badge in library cards
- Add library filters by tag and reading status; tag filter dropdown in library toolbar
- Full-text search: pg_trgm GIN indexes on title/abstract accelerate `ILIKE '%q%'` (Chinese-friendly, no tokenizer)
- Tags section and status selector in the paper detail drawer (migration `v1.1.1_lib.sql`)

## v1.4.0 — 2026-08-31

### Features

- Add chat-with-paper RAG: answer questions grounded in the paper abstract and reading notes (`POST /api/v1/papers/<id>/chat`)
- Add abstract translation (zh/en) via LLM (`POST /api/v1/papers/<id>/translate`)
- Add related-paper recommendations by abstract-vector cosine similarity (`GET /api/v1/papers/<id>/related`)
- Add AI Assistant panel (ask / translate / related papers) in the paper detail drawer

## v1.3.0 — 2026-08-31

### Features

- Add citation verification: reverse-lookup DOIs in generated reviews against the paper library to flag hallucinated references (`GET /api/v1/reviews/<id>/verify`)
- Add BibTeX / RIS citation export for library papers (`GET /api/v1/papers/<id>/export?format=bib|ris`)
- Show citation verification status in the review detail UI; add BibTeX / RIS export buttons in the paper detail drawer

## v1.2.3 — 2026-08-30

### Changes

- Version bump from v1.1.3

## v1.1.3 — 2026-08-28

### Changes

- Version bump from v1.0.3
- fix(plugins): attach edition roles to dedicated sub-agents

## v1.0.3 — 2026-08-22

### Changes

- Version bump from v1.0.2

