-- VeroScholar Discovery Engine 数据表（v1.11.0）
-- 全部位于 veroscholar schema；全 IF NOT EXISTS 幂等；不依赖 pgvector；
-- 迁移由 models.migrate() 按文件名升序执行，本文件字典序位于全文迁移之后。

-- 1. 发现运行（一轮：从问题到假设集合）
CREATE TABLE IF NOT EXISTS veroscholar.discovery_runs (
    id              UUID PRIMARY KEY,
    question        TEXT NOT NULL,
    project_id      UUID REFERENCES veroscholar.projects(id) ON DELETE SET NULL,
    user_id         BIGINT DEFAULT 0,
    trigger         TEXT NOT NULL DEFAULT 'manual',
    status          TEXT NOT NULL DEFAULT 'running',
    config          JSONB DEFAULT '{}',
    metrics         JSONB DEFAULT '{}',
    error           TEXT DEFAULT '',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    finished_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_discovery_runs_user_created
    ON veroscholar.discovery_runs(user_id, created_at DESC);

-- 2. 假设主表
CREATE TABLE IF NOT EXISTS veroscholar.hypotheses (
    id              UUID PRIMARY KEY,
    run_id          UUID NOT NULL REFERENCES veroscholar.discovery_runs(id) ON DELETE CASCADE,
    statement       TEXT NOT NULL,
    context         TEXT DEFAULT '',
    variables       JSONB DEFAULT '[]',
    relationship    TEXT DEFAULT '',
    null_hypothesis TEXT DEFAULT '',
    falsification_test TEXT DEFAULT '',
    score           DOUBLE PRECISION DEFAULT 0.0,
    evidence_ids    JSONB DEFAULT '[]',
    cluster_id      TEXT DEFAULT '',
    current_status  TEXT DEFAULT 'pending',
    elo_rating      DOUBLE PRECISION DEFAULT 1000.0,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_hypotheses_run_elo
    ON veroscholar.hypotheses(run_id, elo_rating DESC);

-- 3. Elo 对局计分
CREATE TABLE IF NOT EXISTS veroscholar.hypothesis_votes (
    id              UUID PRIMARY KEY,
    run_id          UUID NOT NULL REFERENCES veroscholar.discovery_runs(id) ON DELETE CASCADE,
    winner_id       UUID NOT NULL REFERENCES veroscholar.hypotheses(id) ON DELETE CASCADE,
    loser_id        UUID NOT NULL REFERENCES veroscholar.hypotheses(id) ON DELETE CASCADE,
    voter           TEXT DEFAULT 'engine',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (run_id, winner_id, loser_id, voter)
);

-- 4. 假设讨论记录
CREATE TABLE IF NOT EXISTS veroscholar.hypothesis_discussions (
    id              UUID PRIMARY KEY,
    hypothesis_id   UUID NOT NULL REFERENCES veroscholar.hypotheses(id) ON DELETE CASCADE,
    protocol        TEXT DEFAULT 'discussion',
    question        TEXT DEFAULT '',
    response        TEXT DEFAULT '',
    meta            JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_hypothesis_discussions_hid
    ON veroscholar.hypothesis_discussions(hypothesis_id, created_at ASC);

-- 5. 节点事件流
CREATE TABLE IF NOT EXISTS veroscholar.discovery_events (
    id              UUID PRIMARY KEY,
    run_id          UUID NOT NULL REFERENCES veroscholar.discovery_runs(id) ON DELETE CASCADE,
    stage           TEXT DEFAULT '',
    status          TEXT DEFAULT 'info',
    detail          JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_discovery_events_run_created
    ON veroscholar.discovery_events(run_id, created_at ASC);

-- 6. 长期记忆（M1 预算/回流预留；当前仅存储不消费）
CREATE TABLE IF NOT EXISTS veroscholar.discovery_memory (
    run_id          UUID NOT NULL REFERENCES veroscholar.discovery_runs(id) ON DELETE CASCADE,
    memory_type     TEXT NOT NULL,
    content         TEXT DEFAULT '',
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (run_id, memory_type)
);
