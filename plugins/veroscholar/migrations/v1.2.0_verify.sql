-- VeroScholar v1.7.0：DOI 权威校验缓存 + 检索策略落库
SET search_path TO veroscholar, public;

CREATE TABLE IF NOT EXISTS doi_checks (
    doi         varchar(512) PRIMARY KEY,
    status      varchar(16) NOT NULL,     -- registered|retracted|concern|unregistered|offline
    notice_doi  varchar(512),             -- 撤稿/关注声明的 DOI
    checked_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE reviews ADD COLUMN IF NOT EXISTS search_strategy jsonb NOT NULL DEFAULT '{}';

CREATE TABLE IF NOT EXISTS search_strategies (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy     jsonb NOT NULL,
    created_by   integer,
    created_at   timestamptz NOT NULL DEFAULT now()
);