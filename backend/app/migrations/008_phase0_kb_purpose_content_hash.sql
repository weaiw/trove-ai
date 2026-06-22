-- Phase 0 地基: 知识库定位(kb_purpose) + 内容哈希去重(content_hash)
-- ⚠️ init_db() 单一事务跑全部 migration,早期任意 statement 失败会 cascade 挡住后续。
--   部署时务必 SSH 进服务器手动 apply 一次(见 项目记忆.md「已知坑」):
--   sudo docker exec linkmind-db psql -U readflow -d readflow -f /path/to/this.sql
--   或逐条 ALTER ... IF NOT EXISTS。

ALTER TABLE users ADD COLUMN IF NOT EXISTS kb_purpose TEXT;

ALTER TABLE articles ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64);
CREATE INDEX IF NOT EXISTS idx_articles_content_hash ON articles (content_hash);
