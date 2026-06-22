-- MCP 写入开关:默认关(MCP 默认只读)。部署需 SSH 手动 apply。
ALTER TABLE users ADD COLUMN IF NOT EXISTS mcp_write_enabled BOOLEAN NOT NULL DEFAULT FALSE;
