-- plugins/veroscholar/migrations/v1.3.0_fulltext.sql
-- VeroScholar 模块 B：PDF 全文（B1 期，幂等）
--
-- 约定（对齐 v1.1.1_lib.sql）：
--   1. 全部 IF NOT EXISTS，可安全重复执行
--   2. B1 不依赖 pgvector：embedding 列留待 B2 期以
--      ALTER TABLE ... ADD COLUMN IF NOT EXISTS 增补（本迁移零扩展依赖）

SET search_path TO veroscholar, public;

-- ------------------- 全文文件（同文重复上传幂等） -------------------
CREATE TABLE IF NOT EXISTS fulltext_files (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id   uuid NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    filename   varchar(512) NOT NULL,
    size_bytes integer NOT NULL DEFAULT 0,
    sha256     varchar(64) NOT NULL,
    n_chunks   integer NOT NULL DEFAULT 0,
    status     varchar(16) NOT NULL DEFAULT 'ready',   -- ready | failed
    created_by integer,                                -- public.users.id（只读引用，无外键）
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (paper_id, sha256)
);

-- ------------------- 全文分块（B2 期再增补 embedding 向量列） -------------------
CREATE TABLE IF NOT EXISTS fulltext_chunks (
    id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id  uuid NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    file_id   uuid NOT NULL REFERENCES fulltext_files(id) ON DELETE CASCADE,
    seq       integer NOT NULL,
    page      integer NOT NULL DEFAULT 1,
    content   text NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ft_chunks_paper ON fulltext_chunks(paper_id);
CREATE INDEX IF NOT EXISTS idx_ft_files_paper ON fulltext_files(paper_id);
