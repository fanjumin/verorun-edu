-- plugins/veroscholar/migrations/v1.0.0_init.sql
-- VeroScholar 科研工作台初始 Schema（幂等）
--
-- 约定（对齐插件标准 §9.1 单库多 Schema + project_workspace 先例）：
--   1. 独立 schema `veroscholar`，全部插件数据自包含
--   2. 依赖 pgvector（由部署预置，同 project_workspace 约定）；
--      向量列 embedding vector(1536) 不可用时，应用层降级为关键词检索
--   3. 所有语句 IF NOT EXISTS，可安全重复执行
--   4. 主库 public 表仅作只读引用，不建外键（避免安装时与主库解耦）

CREATE SCHEMA IF NOT EXISTS veroscholar;

-- pgvector 为平台能力（trusted extension）：迁移自建，不依赖部署脚本预建。
-- 显式 SCHEMA public：迁移执行器已预设 search_path 为插件 schema，若不显式指定，
-- 扩展会落入插件 schema（其他插件 search_path 不含它 → vector 类型不可见）。
CREATE EXTENSION IF NOT EXISTS vector SCHEMA public;

SET search_path TO veroscholar, public;

-- ------------------- 核心论文表 -------------------
CREATE TABLE IF NOT EXISTS papers (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    doi            varchar(512) UNIQUE,
    title          text NOT NULL,
    authors        jsonb NOT NULL DEFAULT '[]',      -- [{"name": "...", "orcid": "..."}]
    abstract       text,
    venue          varchar(512),                      -- 期刊/会议名称
    year           integer,
    citation_count integer NOT NULL DEFAULT 0,
    pdf_url        text,
    metadata       jsonb NOT NULL DEFAULT '{}',       -- 额外字段：keywords, funding 等
    embedding      vector(1536),                      -- pgvector 向量（语义检索预留）
    source_db      varchar(64) NOT NULL DEFAULT 'unknown',  -- 'arxiv' | 'semantic_scholar' | 'openalex'
    external_id    varchar(256),                      -- 源数据库中的原始 ID
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);
CREATE INDEX IF NOT EXISTS idx_papers_year ON papers(year);
CREATE INDEX IF NOT EXISTS idx_papers_source ON papers(source_db);
CREATE INDEX IF NOT EXISTS idx_papers_embedding
    ON papers USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ------------------- 研究项目表 -------------------
CREATE TABLE IF NOT EXISTS projects (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name         varchar(255) NOT NULL,
    description  text NOT NULL DEFAULT '',
    status       varchar(16) NOT NULL DEFAULT 'active',  -- 'active' | 'archived'
    team_members jsonb NOT NULL DEFAULT '[]',            -- [user_id, ...]
    created_by   integer,                                -- public.users.id（只读引用，无外键）
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- ------------------- 项目-论文关联表（多对多） -------------------
CREATE TABLE IF NOT EXISTS project_papers (
    project_id  uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    paper_id    uuid NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    added_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, paper_id)
);

CREATE INDEX IF NOT EXISTS idx_project_papers_paper ON project_papers(paper_id);

-- ------------------- 论文笔记/批注表（支持 RAG 检索） -------------------
CREATE TABLE IF NOT EXISTS annotations (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id   uuid NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    user_id    integer,
    note_type  varchar(32) NOT NULL DEFAULT 'summary',   -- 'summary' | 'critique' | 'methodology' | 'question'
    content    text NOT NULL,
    embedding  vector(1536),                             -- 语义检索笔记内容
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_annotations_paper ON annotations(paper_id);
CREATE INDEX IF NOT EXISTS idx_annotations_user ON annotations(user_id);
CREATE INDEX IF NOT EXISTS idx_annotations_embedding
    ON annotations USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ------------------- 综述文档表 -------------------
CREATE TABLE IF NOT EXISTS reviews (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title      varchar(512) NOT NULL,
    topic      text,                                    -- 综述主题
    structure  jsonb NOT NULL DEFAULT '{}',             -- 大纲结构 {"sections": [...]}
    content    text,                                    -- 完整 Markdown 内容
    paper_ids  jsonb NOT NULL DEFAULT '[]',             -- 引用的论文 ID 列表
    status     varchar(16) NOT NULL DEFAULT 'draft',    -- 'draft' | 'reviewing' | 'final'
    created_by integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- ------------------- 审计日志（用于统计仪表盘） -------------------
CREATE TABLE IF NOT EXISTS search_logs (
    id           bigserial PRIMARY KEY,
    query        text,
    source_db    varchar(64),
    result_count integer NOT NULL DEFAULT 0,
    user_id      integer,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_search_logs_created ON search_logs(created_at);

-- ------------------- 迁移版本追踪 -------------------
CREATE TABLE IF NOT EXISTS schema_version (
    version     varchar(64) PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
