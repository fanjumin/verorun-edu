-- plugins/veroscholar/migrations/v1.1.1_lib.sql
-- VeroScholar 库管理增强：全文检索索引 + 标签系统 + 阅读状态（幂等）
--
-- 约定（对齐 v1.0.0_init.sql）：
--   1. 全部 IF NOT EXISTS，可安全重复执行
--   2. pg_trgm 显式 SCHEMA public（trusted extension，与 pgvector 同先例）
--   3. 检索仍使用 ILIKE（GIN trgm 索引自动加速，中文不依赖分词器）

CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA public;

SET search_path TO veroscholar, public;

-- 阅读状态：'unread' | 'reading' | 'read'
ALTER TABLE papers
    ADD COLUMN IF NOT EXISTS reading_status varchar(16) NOT NULL DEFAULT 'unread';

-- 全文检索：trigram GIN 索引加速 title/abstract 的 ILIKE 模糊匹配
CREATE INDEX IF NOT EXISTS idx_papers_title_trgm
    ON papers USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_papers_abstract_trgm
    ON papers USING gin (abstract gin_trgm_ops);

-- ------------------- 标签表（唯一名称） -------------------
CREATE TABLE IF NOT EXISTS tags (
    id         bigserial PRIMARY KEY,
    name       varchar(64) NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- ------------------- 论文-标签关联（多对多） -------------------
CREATE TABLE IF NOT EXISTS paper_tags (
    paper_id   uuid NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    tag_id     bigint NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (paper_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_paper_tags_tag ON paper_tags(tag_id);
