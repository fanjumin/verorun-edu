# VeroRun EDU

**Knowledge-Centric AI Platform for Research & Education**

VeroRun EDU is a self-hosted, knowledge-centric AI operating system built for research institutes, universities, and educational organizations that operate inside an intranet. It transforms institutional knowledge into a living, queryable, AI-empowered asset — with built-in project isolation, document RAG, DAG workflows, content automation, and enterprise-grade health/backup safeguards.

> **Version:** 0.56.4
> **Repository:** https://github.com/fanjumin/verorun-edu
> **Edition:** VeroRun EDU Edition

[![Version](https://img.shields.io/badge/version-0.56.4-blue)]()
[![Python](https://img.shields.io/badge/python-3.11+-green)](https://www.python.org/)
[![Database](https://img.shields.io/badge/database-PostgreSQL-336791)](https://www.postgresql.org/)

---

## Table of Contents

1. [What Is VeroRun EDU](#what-is-verorun-edu)
2. [Knowledge-Centric by Design](#knowledge-centric-by-design)
3. [How the Knowledge Base Is Implemented](#how-the-knowledge-base-is-implemented)
4. [Isolation Model](#isolation-model)
5. [DAG Workflow Engine](#dag-workflow-engine)
6. [Content Factory Plugin](#content-factory-plugin)
7. [Built-in Plugins](#built-in-plugins)
8. [Deployment on the Intranet](#deployment-on-the-intranet)
9. [Data Safety & Backup](#data-safety--backup)
10. [Edition Identity](#edition-identity)

---

## What Is VeroRun EDU

VeroRun EDU is a distribution of the VeroRun AI operating system tailored for **research and education institutions on private networks**. It ships with a curated set of plugins optimized for institutional knowledge management — not for e-commerce. The commercial module ("Business Center") and consumer-site tooling are excluded.

**What it does out of the box:**

- Curate and store institutional knowledge (papers, reports, manuals, lecture notes, regulations, datasets)
- Retrieve knowledge via semantic search and RAG-powered Q&A with cited sources
- Isolate knowledge per project / department while keeping an admin-curated global knowledge base
- Automate content collection, processing, review, and publishing through DAG workflows
- Keep every service healthy with automated health checks, alerting, and encrypted backups

---

## Knowledge-Centric by Design

Everything in VeroRun EDU orbits one asset: **knowledge**. The architecture is layered so that knowledge flows upward from raw storage to AI reasoning:

```
┌─────────────────────────────────────────────────────────────┐
│   Agents & Chatbots  (RAG Q&A · Advisor · Memory · Profile) │
├─────────────────────────────────────────────────────────────┤
│   Knowledge Services  (semantic search · workspace RAG)     │
├─────────────────────────────────────────────────────────────┤
│   Knowledge Store  (knowledge_blocks · project_workspace)   │
├─────────────────────────────────────────────────────────────┤
│   Content Factory  (collect → AI process → review → ingest) │
├─────────────────────────────────────────────────────────────┤
│   Data Safety      (Vault backup/audit · Health Check)      │
└─────────────────────────────────────────────────────────────┘
```

Knowledge is entered through three paths:

1. **Manual curation** — admins write knowledge entries directly in the Knowledge Base admin panel (title, content, keywords, category, priority, scope).
2. **Document import** — Project Workspace ingests PDF/DOCX/TXT/MD/PPTX/XLSX/CSV files and indexes them for semantic search.
3. **Content Factory pipeline** — RSS/web sources are collected, AI-processed, human-reviewed, and pushed into the knowledge base automatically on a schedule.

---

## How the Knowledge Base Is Implemented

### Core table: `knowledge_blocks`

The system-wide knowledge base lives in the main PostgreSQL database under the `knowledge_blocks` table. Every entry carries:

| Field | Purpose |
|---|---|
| `title` / `content` | The knowledge itself |
| `keywords` | Explicit keyword index |
| `category` | Grouping (papers, regulations, faq, training, ...) |
| `priority` | Ordering weight in search results |
| `scope` | `system` (admin-global) or project-scoped entries |
| `source` | Provenance (manual / import / content factory) |
| `owner_id` | Creating admin |
| `deleted_at` | Soft delete (auditable, recoverable) |
| `hit_count` | Usage statistics |

### Search & Q&A

- **Keyword search** — admin panel supports filtered listing and full-text search over title/content/keywords.
- **Semantic / RAG search** — Project Workspace embeds documents (embedding model, configurable), chunks them (default 1000 chars, 200 overlap), and retrieves top-K chunks for RAG-powered answers. The `Workspace Assistant` agent answers questions **with sources** (`qa.with_sources`).
- **Chatbot knowledge access** — the AI Advisor chatbot cross-reads the main database knowledge base, so every user-facing answer can cite institutional knowledge.

### Admin endpoints

The Knowledge Base admin exposes a REST API under `/admin/knowledge/`:

- `GET /` — overview (total entries, categories)
- `GET /stats` — statistics (by category, by scope, total hits)
- `GET/POST /entries` — list/create knowledge entries
- `GET/PUT/DELETE /entries/<id>` — retrieve/update/soft-delete
- `POST /search` — filtered search
- `POST /query` — query answering

---

## Isolation Model

VeroRun EDU provides **four layers of isolation**:

### 1. Network isolation (intranet-first)

EDU deploys with **no public domain** (`DEPLOY_TYPE=edu`). There is no external DNS exposure and no Let's Encrypt dependency. It can run on an air-gapped LAN or behind a corporate firewall; if outbound access is required, the deployment can be pointed at an internal DNS name instead.

### 2. Project-level isolation (Project Workspace)

Each project is a sealed workspace:

- Up to 50 projects per user; up to 5,000 documents per project
- Documents are scoped to their project
- **Cross-project search is disabled by default** (`enable_cross_project_search: false`) so a research group can never see another group's documents
- Workspace Assistant only answers from the current project's corpus

### 3. System vs project scope (knowledge_blocks.scope)

The global knowledge base distinguishes admin-curated `system` entries from project entries, so institutional standards and project-specific material stay separated.

### 4. Release isolation (distribution model)

EDU is a dedicated repository (`verorun-edu`) produced by an automated pipeline from the private `verorun-code` repository. The pipeline ships **only** the EDU plugin allowlist plus the shared `site_domains` base. Plugin source for non-EDU plugins never lands in the EDU repository, and the commercial store/subscription surface is not installed.

---

## DAG Workflow Engine

VeroRun EDU ships a lightweight DAG workflow engine (`orchestrator/workflow_engine.py`) that executes automation with full state machines and failure handling.

### Instance state machine

```
pending → running → completed
              ├→ failed
              ├→ paused → running
              ├→ timeout
              └→ cancelled
```

### Node state machine

```
pending → running → completed
              ├→ failed
              ├→ skipped
              └→ waiting_approval → completed / rejected
```

### Node types

| Node | Purpose |
|---|---|
| `ai_agent` | Invoke an AI agent for reasoning/generation |
| `data_collect` | Collect data from sources |
| `ai_process` | Transform/process content with AI |
| `condition` | Branching based on context |
| `approval` | Human-in-the-loop approval gate |
| `publish` | Publish to CMS / social / static pages |
| `notify` | Send notifications |
| `wait` | Delay / schedule |
| `sub_workflow` | Nest another workflow |
| `market_check` | Check external conditions |
| `http_request` | Call external HTTP APIs |
| `script` | Execute custom scripts |

Handlers are registered externally (e.g., analytics registers report/insight/alert nodes), and safe evaluation prevents arbitrary code execution in condition nodes. Use cases: nightly content ingestion, weekly knowledge digest, multi-step research summarization with approval gates, and monitoring alert pipelines.

---

## Content Factory Plugin

The **Content Factory** (`content_factory`) is the content-production engine. It provides a full lifecycle: **collect → AI process → review → publish → ingest to knowledge base**.

### What it does

- **Multi-source collection** — RSS feed collectors with configurable interval, keyword filters, and per-run limits
- **AI processing** — batch processing via DashScope (Qwen): summaries, rewriting, layout cleanup, and AI cover-image generation
- **Review workflow** — a formal review state machine

```
draft → submit_review → review → approve → approved → publish → published
   ^                      |          |                                  |
   +----- back_to_draft --+-- rejected +----- back_to_draft -----------+
```

- **Multi-channel publishing** — internal CMS articles, social distribution, and static HTML page generation (article/category/doc index)
- **Knowledge base push** — finished content is pushed into the knowledge system so it becomes searchable/Q&A-able
- **Skill push** — converts processed content into Agent Skills for AI agents
- **Scheduled runs** — driven by `cron.tick` for automated collection on a schedule

### Data model

Independent PostgreSQL schema `content_factory` with 5 core tables: `content_sources`, `raw_contents`, `processed_contents`, `content_tasks`, `skill_pushes`.

---

## Built-in Plugins

VeroRun EDU ships with a curated plugin set:

| Plugin | Role |
|---|---|
| **Project Workspace** | Project isolation, document RAG, semantic search, research assistant |
| **Knowledge Base** | Global `knowledge_blocks` store, admin curation, stats |
| **Content Factory** | Multi-source collection, AI processing, review, publish, KB ingest |
| **Workflow Engine** | DAG automation with approval gates |
| **Health Check** | CPU/memory/disk thresholds, alert email/webhook, trend analysis, LLM-based fix suggestions |
| **Vault** | Full/incremental backups, AES-256-GCM encryption, audit logging, scheduled backups |
| **Agent Memory** | Hierarchical agent memory with vector retrieval and Reflexion self-evolution |
| **AI Advisor (chatbot)** | Site-wide AI Q&A that answers from the knowledge base and routes to humans when needed |
| **Email Service** | SMTP/IMAP client for internal mail workflows |
| **OAuth Login Config** | Third-party SSO login integration |
| **Visitor Profile Engine** | AI-driven visitor behavior profiling (optional, on connected networks) |

Plugins not included in EDU: e-commerce (shop), site builder, mini-app builder, SMS, Alibaba cloud API, social push, enterprise license verification, analytics, and captcha.

---

## Deployment on the Intranet

### Requirements

| Requirement | Minimum |
|---|---|
| OS | Ubuntu 22.04 / 24.04 (x86_64) |
| CPU / RAM | 2 vCPU / 4 GB (8 GB recommended) |
| Disk | 40 GB free |
| Python | 3.10+ |
| Network | Intranet / LAN (no public domain required) |

### One-command install

```bash
# From a machine with internet access to github.com (or mirror):
curl -fsSL https://raw.githubusercontent.com/fanjumin/verorun-edu/master/deploy/install.sh -o /tmp/install.sh
sudo bash /tmp/install.sh --approve-migrate
```

- When prompted for install type, choose **educational** (`INSTALL_TYPE=educational`)
- The installer resolves this to `DEPLOY_TYPE=edu`, clones `verorun-edu`, writes `VR_EDITION=edu` into `.env`, and starts all systemd services (`verorun-main` / `verorun-auth` / `verorun-admin` / `verorun-health` / `verorun-guardian`)
- No domain or HTTPS certificate is required — services are bound for intranet access
- **Idempotent**: the installer is safe to re-run; DB migration is approved explicitly via `--approve-migrate`

### Post-install verification

```bash
grep VR_EDITION /path/to/verorun/.env   # expect: VR_EDITION=edu
systemctl status verorun-main --no-pager # active (running)
```

---

## Data Safety & Backup

- **Vault** — full/incremental backups with AES-256-GCM encryption, gzip compression, retention policy (default 30 days), scheduled backups, and multi-target storage (local / S3 / OSS / Azure / GCS)
- **Audit logging** — all backup and vault operations are audit-logged
- **Health Check** — automated probes with configurable CPU/memory/disk thresholds, alert email + webhook notifications, and trend analysis; optional LLM-powered root-cause analysis and repair suggestions
- **Soft deletes** — knowledge entries use soft delete for recoverability

---

## Edition Identity

VeroRun EDU installs are clearly marked:

- `VR_EDITION=edu` is written to `.env` at install time
- The admin sidebar shows a golden **EDU Edition** badge
- The public footer copyright shows **VeroRun EDU Edition**
- The **Business Center** (subscription/pricing/revenue) menu group is hidden in the EDU edition

---

## License

This distribution is provided under the VeroRun EULA. Redistribution of the EDU plugin set outside of the intended institutional deployment is not permitted.
