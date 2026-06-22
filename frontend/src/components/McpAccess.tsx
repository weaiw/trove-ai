'use client';

import React, { useState, useEffect } from 'react';
import { Plug, Copy, Check, ChevronDown, Loader2 } from 'lucide-react';
import { api } from '@/lib/api';

/**
 * 外部 AI 接入(MCP)说明卡片。放在个人设置(/my)。
 * MCP 复用同步 Token(上方「Obsidian 备份」生成的那个),不另设密钥。
 * 写入默认关,开关控制是否暴露 add/update/set_tags 等写工具。
 */
export default function McpAccess() {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState<string>('');
  const [writeOn, setWriteOn] = useState<boolean | null>(null);
  const [toggling, setToggling] = useState(false);

  useEffect(() => {
    api.getMe().then((u) => setWriteOn(!!u.mcp_write_enabled)).catch(() => setWriteOn(false));
  }, []);

  const toggleWrite = async () => {
    if (writeOn === null) return;
    const next = !writeOn;
    setToggling(true);
    setWriteOn(next); // optimistic
    try {
      const r = await api.setMcpWrite(next);
      setWriteOn(r.mcp_write_enabled);
    } catch {
      setWriteOn(!next); // revert
    } finally {
      setToggling(false);
    }
  };

  const origin = typeof window !== 'undefined' ? window.location.origin : 'http://你的服务器';
  const endpoint = `${origin}/api/mcp`;
  const cliCmd = `claude mcp add --transport http trove ${endpoint} \\\n  --header "Authorization: Bearer <你的同步Token>"`;

  const copy = (text: string, key: string) => {
    const done = () => { setCopied(key); setTimeout(() => setCopied(''), 2000); };
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(() => fallback(text, done));
    } else {
      fallback(text, done);
    }
  };
  const fallback = (text: string, done: () => void) => {
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); done(); } catch {}
    document.body.removeChild(ta);
  };

  return (
    <div className="bg-[var(--bg-primary)] rounded-xl border border-[var(--border-color)] p-5">
      <div className="flex items-center gap-2 mb-3">
        <Plug size={18} className="text-[var(--accent)]" />
        <h2 className="font-semibold text-[var(--foreground)]">外部 AI 接入(MCP)</h2>
      </div>
      <p className="text-sm text-[var(--text-secondary)] mb-4">
        通过 MCP 协议,让 Claude 等外部 AI 助手直接检索、读取你的知识库与图谱洞察。
      </p>

      {/* Token 说明 */}
      <div className="text-sm text-[var(--text-secondary)] mb-3 leading-relaxed">
        <span className="font-medium text-[var(--foreground)]">用哪个 Token?</span> 不用另建——直接用上方
        「Obsidian 备份」里「生成本地同步 Token」得到的那个 <b>365 天</b>长期 Token。MCP 和 Obsidian
        同步共用它;在那张卡片点「撤销所有同步 Token」即可一并失效。
      </div>

      {/* Endpoint */}
      <div className="mb-3">
        <label className="block text-xs text-[var(--text-tertiary)] mb-1">MCP 端点</label>
        <div className="flex items-center gap-2">
          <code className="flex-1 px-3 py-2 rounded-lg bg-[var(--bg-secondary)] text-xs text-[var(--text-primary)] break-all">{endpoint}</code>
          <button
            onClick={() => copy(endpoint, 'ep')}
            className="px-2.5 py-2 rounded-lg border border-[var(--border-color)] hover:bg-[var(--bg-tertiary)] text-[var(--text-secondary)]"
            title="复制端点"
          >
            {copied === 'ep' ? <Check size={14} className="text-green-500" /> : <Copy size={14} />}
          </button>
        </div>
        {origin.startsWith('http://') && (
          <p className="text-[11px] text-[var(--text-tertiary)] mt-1">
            注:当前是 HTTP 明文,公网暴露建议先套 HTTPS。
          </p>
        )}
      </div>

      {/* 写入开关 */}
      <div className="mb-3 p-3 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-color)]">
        <label className="flex items-center justify-between cursor-pointer" onClick={() => !toggling && toggleWrite()}>
          <div className="pr-3">
            <div className="text-sm font-medium text-[var(--foreground)]">允许写入</div>
            <div className="text-xs text-[var(--text-tertiary)] mt-0.5">
              开启后 MCP 才暴露 add_article / add_note / update_article / set_article_tags 等写工具。默认关(只读),
              因为这是对外 token 入口,谨慎开启。
            </div>
          </div>
          <span
            className={`relative inline-block w-10 h-6 rounded-full shrink-0 transition-colors ${
              writeOn ? 'bg-[var(--accent)]' : 'bg-[var(--border-color)]'
            } ${toggling ? 'opacity-60' : ''}`}
          >
            <span className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${writeOn ? 'translate-x-4' : ''}`} />
          </span>
        </label>
        {writeOn === null && <div className="text-[11px] text-[var(--text-tertiary)] mt-1 flex items-center gap-1"><Loader2 size={10} className="animate-spin" /> 读取设置…</div>}
      </div>

      {/* 连接教程(可折叠) */}
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1 text-sm text-[var(--accent)] hover:underline"
      >
        连接配置教程 <ChevronDown size={14} className={open ? 'rotate-180 transition-transform' : 'transition-transform'} />
      </button>

      {open && (
        <div className="mt-3 space-y-4 text-sm">
          <div>
            <div className="text-xs text-[var(--text-tertiary)] mb-1">方式一:Claude Code 命令行</div>
            <div className="relative">
              <pre className="px-3 py-2 rounded-lg bg-[var(--bg-secondary)] text-xs text-[var(--text-primary)] overflow-x-auto whitespace-pre-wrap">{cliCmd}</pre>
              <button
                onClick={() => copy(cliCmd.replace('\\\n  ', ' '), 'cli')}
                className="absolute top-2 right-2 px-2 py-1 rounded border border-[var(--border-color)] bg-[var(--bg-primary)] text-[var(--text-secondary)]"
              >
                {copied === 'cli' ? <Check size={12} className="text-green-500" /> : <Copy size={12} />}
              </button>
            </div>
          </div>

          <div>
            <div className="text-xs text-[var(--text-tertiary)] mb-1">方式二:其它 MCP 客户端(claude_desktop_config.json 等)</div>
            <pre className="px-3 py-2 rounded-lg bg-[var(--bg-secondary)] text-xs text-[var(--text-primary)] overflow-x-auto">{`{
  "mcpServers": {
    "trove": {
      "type": "http",
      "url": "${endpoint}",
      "headers": {
        "Authorization": "Bearer <你的同步Token>"
      }
    }
  }
}`}</pre>
          </div>

          <div>
            <div className="text-xs text-[var(--text-tertiary)] mb-1">可用工具(读)</div>
            <ul className="text-xs text-[var(--text-secondary)] space-y-1 pl-4 list-disc">
              <li><code>search_knowledge</code> — 语义检索知识库</li>
              <li><code>get_article</code> — 按 id 读取整篇文章</li>
              <li><code>knowledge_insights</code> — 主题簇 / 枢纽 / 意外连接 / 知识缺口</li>
              <li><code>list_recent_articles</code> — 最近收藏列表</li>
            </ul>
            <div className="text-xs text-[var(--text-tertiary)] mt-2 mb-1">写工具{writeOn ? '(已开启)' : '(需打开上方「允许写入」)'}</div>
            <ul className={`text-xs space-y-1 pl-4 list-disc ${writeOn ? 'text-[var(--text-secondary)]' : 'text-[var(--text-tertiary)] opacity-60'}`}>
              <li><code>add_article</code> — 加链接进库</li>
              <li><code>add_note</code> — 新建笔记</li>
              <li><code>update_article</code> — 改标题/正文</li>
              <li><code>set_article_tags</code> — 设标签</li>
            </ul>
          </div>
        </div>
      )}
    </div>
  );
}
