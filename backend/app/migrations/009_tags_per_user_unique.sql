-- 标签按用户隔离:把全局唯一 UNIQUE(name) 改为 UNIQUE(user_id, name)。
-- ⚠️ 仅改约束,不迁移旧数据(旧标签归属维持现状,前向逻辑保证今后各用户各建各的)。
-- 现有数据 name 本就全局唯一 → (user_id,name) 必然无重复,加约束不会失败。
-- 部署需 SSH 手动 apply(init_db 单事务老坑):
--   sudo docker exec -i linkmind-db psql -U readflow -d readflow -f .../009_*.sql

ALTER TABLE tags DROP CONSTRAINT IF EXISTS tags_name_key;
ALTER TABLE tags ADD CONSTRAINT uq_tags_user_name UNIQUE (user_id, name);
