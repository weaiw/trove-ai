-- 概念合成页(Phase 7·E):同一概念跨文章合成一页带溯源。
-- 部署需 SSH 手动 apply(init_db 单事务老坑)。
CREATE TABLE IF NOT EXISTS concept_pages (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL,
    name VARCHAR(200) NOT NULL,
    seed_type VARCHAR(20) DEFAULT 'topic',
    seed_tag VARCHAR(100),
    content TEXT,
    source_article_ids JSONB DEFAULT '[]'::jsonb,
    stale BOOLEAN DEFAULT FALSE,
    new_source_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT uq_concept_user_name UNIQUE (user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_concept_pages_user_id ON concept_pages (user_id);
