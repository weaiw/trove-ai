-- 概念页:加语义质心(centroid,给所有类型页做语义 stale 提醒)+ 自动合并开关(auto_update)。
-- 部署需 SSH 手动 apply(init_db 单事务老坑)。
ALTER TABLE concept_pages ADD COLUMN IF NOT EXISTS centroid vector(1024);
ALTER TABLE concept_pages ADD COLUMN IF NOT EXISTS auto_update BOOLEAN DEFAULT FALSE;
