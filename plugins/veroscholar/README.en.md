# VeroScholar

VeroScholar is VeroRun AI's research-lifecycle plugin for the education edition, covering the four core stages of **topic selection · literature · writing · review**.

The plugin strictly follows [`docs/plugin-standard-v1.7.md`](../../docs/plugin-standard-v1.7.md): single database with multiple schemas (`veroscholar`), shared connection pool, JWT admin authentication, iframe standalone pages, i18n bilingual strings, and zero residue on uninstall.

## Feature Modules

| Module | Description |
|--------|-------------|
| Multi-source literature search | Aggregates arXiv / Semantic Scholar / OpenAlex / Chinese works (OpenAlex `language:zh`), DOI deduplication with title+year fallback, `semantic` and `semantic_scholar` aliases; supports year/citation/venue post‑filters |
| Paper knowledge base | Paper ingestion, search, details; fallback dedup by title+year when no DOI |
| Research projects | Project CRUD, member management, paper categorization |
| Reading notes | Paper-level note annotations, optional embedding vectors (pgvector) for semantic search |
| Review generator | Triggers a DAG workflow: keyword expansion → multi-source search → deterministic dedup & ranking → methodology classification → LLM-generated review |
| Citation verification | Online verification via Crossref/DataCite with 5‑state (registered/retracted/concern/unregistered/offline), retraction notice links, local library supplement |
| Paper export | Single-paper BibTeX / RIS export (preprints auto-detected as `@misc`/`TY - RPRT`) |
| Paper Q&A | RAG-based Q&A on paper abstract + reading notes (material‑bound, anti‑hallucination) |
| Abstract translation | Chinese‑English abstract translation (LLM) |
| Related papers | Cosine similarity recommendation via abstract embeddings; three-tier vector backend auto-selected by environment: T2 pgvector (server) → T1 local numpy flat cosine (desktop, self-healing `paper_vectors` side table) → T0 pg_trgm keyword fallback — the recommendation chain never 5xx's in any environment |
| Cognitive loop feedback | After searches / reviews / notes / paper Q&A are persisted, results are explicitly fed back to the active cognitive engine (channel 2: direct call to memory_engine `MemoryExtractor.submit`, with substrate as the event-bus fallback path); auto-disabled when no engine is enabled, failures only logged and never block the main flow |
| Knowledge base two-way sync | Write path: papers / notes / reviews upserted into project_workspace (documents + chunks, idempotent); read path: paper Q&A merges project knowledge via the `veroscholar.qa_context` hook. Auto-disabled when project_workspace is not enabled (research desktop edition) |
| Tag system | Paper-level tags (idempotent add/remove) + library filtering by tag |
| Reading status | unread/reading/read three-state, library card badge + drawer toggle |
| Full‑text search | pg_trgm GIN-indexed title/abstract search (Chinese‑friendly, no tokenizer dependency) |
| PDF fulltext (B1) | PDF upload (≤10MB, sha256-idempotent) → pypdf parse → paragraph chunking → chunk browsing; paper Q&A auto-injects top-3 relevant full-text excerpts (hook-based, silent degradation on failure) |
| 3 sub-agents | Literature Review (high tier), Experiment Designer (high tier), Paper Writer (standard tier) |

## Directory Structure

```
plugins/veroscholar/
├── __init__.py                  # BasePlugin lifecycle assembly
├── models.py                    # Data layer (shared connection pool + search_path isolation)
├── routes.py                    # Blueprint: 3 pages + RESTful API + JWT auth
├── workflow.py                  # DAG node handlers + review workflow trigger
├── services/ai.py               # AI service layer (Q&A / translate / related, platform default model resolution)
├── services/vector_backend.py   # Vector backend abstraction (pgvector / local numpy / pg_trgm three-tier fallback)
├── services/kb_sync.py          # Cognitive loop channel 2 + project_workspace two-way knowledge sync
├── plugin.json                  # Plugin metadata (agents/menu/dashboard/settings)
├── migrations/v1.0.0_init.sql   # Schema creation (all IF NOT EXISTS idempotent)
├── migrations/v1.1.1_lib.sql    # pg_trgm index + tags + reading status
├── migrations/v1.2.0_verify.sql # DOI check cache + search strategy tables
├── migrations/v1.3.0_fulltext.sql # PDF fulltext tables (B1, zero extension dependency)
├── fulltext/                    # PDF fulltext module (parser / chunker / qa_ext / routes)
├── adapters/                    # Data source adapters (base + arxiv + semantic_scholar + openalex + openalex_zh)
├── agents/                      # 3 sub-agent prompts
├── workflows/literature_review.json  # Review DAG blueprint
├── templates/                   # dashboard / search / review pages
├── static/js/veroscholar.js     # Frontend logic (VS namespace)
├── static/css/veroscholar.css   # design-system variable styles
├── i18n/en.yml + zh-CN.yml      # English-key bilingual mapping
└── tests/                       # Unit tests (118 cases)
```

## Installation & Enablement

Upload/enable this plugin through the system plugin management page:

1. `python_dependencies.required` declares `feedparser` (loaded lazily at runtime by the arXiv adapter) and `pypdf` (PDF fulltext parsing);
2. On enable, `migrations/*.sql` run automatically (idempotent, records `schema_version`);
3. On uninstall, `DROP SCHEMA veroscholar CASCADE` leaves zero residue.

## Configuration

Configurable on the plugin settings page:

| Config | Default | Description |
|--------|---------|-------------|
| `semantic_scholar_api_key` | — | Optional; raises semantic search rate limits |
| `openalex_api_key` | — | Optional; raises OpenAlex request limits |
| `default_search_limit` | 30 | Default search result count (5–100) |
| `arxiv_enabled` | true | arXiv source toggle |
| `semantic_enabled` | true | Semantic Scholar source toggle |
| `openalex_enabled` | true | OpenAlex source toggle |
| `zh_enabled` | true | Chinese works source toggle (OpenAlex Chinese works) |

## Testing

```bash
cd <VeroRun repo root>   # e.g. D:\projects\verorun-code
python -m unittest plugins.veroscholar.tests.test_adapters \
                       plugins.veroscholar.tests.test_models \
                       plugins.veroscholar.tests.test_routes \
                       plugins.veroscholar.tests.test_services \
                       plugins.veroscholar.tests.test_workflow \
                       plugins.veroscholar.tests.test_zh_adapter \
                       plugins.veroscholar.tests.test_fulltext -v
```

118 test cases cover: adapter field mapping / abstract reconstruction / error propagation, Chinese source language:zh filter & no-DOI retention, data-layer SQL construction, route auth (401/302/static exemption/CORS preflight pass-through), pages and API endpoints, citation verification 5‑state, review dedup node, search filters, preprint export, paper Q&A & translation, tag system & reading status, fulltext chunking & QA injection degradation, review write-back to reviews table, fulltext delete route (success/paper missing/invalid file_id), multi-source search per_source quota.

## Dependencies

- Runtime: `feedparser` + `pypdf` (Python dependencies), PostgreSQL + `pgvector` (optional; when absent, related-papers automatically falls back to local numpy vectors or pg_trgm keywords — see `services/vector_backend.py`)
- Optional companions: `memory_engine` (cognitive loop feedback), `project_workspace` (two-way knowledge base sync, declared via `recommends`; the corresponding capabilities auto-disable when not enabled)
- Environment: plugin standard v1.5+, `min_app_version: 0.58.0`
