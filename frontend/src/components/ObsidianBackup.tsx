'use client';

import React, { useEffect, useState } from 'react';
import {
  BookMarked, KeyRound, Copy, AlertCircle, CheckCircle2,
  Loader2, ShieldOff, Download,
} from 'lucide-react';

/**
 * Obsidian 备份卡片 —— 放在个人设置（/my）。
 * 卡片样式与 WechatBinding / ReviewSettings 保持一致。
 */
export default function ObsidianBackup() {
  const [stats, setStats] = useState<{ total: number; eligible: number } | null>(null);

  const [issuing, setIssuing] = useState(false);
  const [issuedToken, setIssuedToken] = useState<string | null>(null);
  const [issueError, setIssueError] = useState<string | null>(null);

  const [copyMsg, setCopyMsg] = useState<string | null>(null);

  const [revoking, setRevoking] = useState(false);
  const [revokeMsg, setRevokeMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const authHeader = (): Record<string, string> => {
    const t = typeof window !== 'undefined' ? localStorage.getItem('trove_token') : null;
    return t ? { Authorization: `Bearer ${t}` } : {};
  };

  useEffect(() => {
    fetch('/api/sync/stats', { headers: authHeader() })
      .then(r => (r.ok ? r.json() : null))
      .then(d => { if (d) setStats({ total: d.total_articles, eligible: d.eligible_articles }); })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleIssueToken = async () => {
    setIssuing(true);
    setIssueError(null);
    setIssuedToken(null);
    try {
      const r = await fetch('/api/sync/issue-token', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeader() },
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${r.status}`);
      }
      const d = await r.json();
      setIssuedToken(d.token);
    } catch (e: any) {
      setIssueError(e?.message || '生成失败');
    }
    setIssuing(false);
  };

  /** 复制 token —— navigator.clipboard 在 http 上不可用，回退到 textarea + execCommand */
  const handleCopyToken = async () => {
    if (!issuedToken) return;
    let ok = false;
    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      try {
        await navigator.clipboard.writeText(issuedToken);
        ok = true;
      } catch {/* fall through */}
    }
    if (!ok) {
      try {
        const ta = document.createElement('textarea');
        ta.value = issuedToken;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.top = '0';
        ta.style.left = '-9999px';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        ta.setSelectionRange(0, ta.value.length);
        ok = document.execCommand('copy');
        document.body.removeChild(ta);
      } catch {
        ok = false;
      }
    }
    setCopyMsg(ok ? '已复制' : '复制失败，请手动选中复制');
    setTimeout(() => setCopyMsg(null), 2000);
  };

  const handleRevokeAll = async () => {
    if (!confirm('确定撤销所有同步 Token？所有正在使用本账号 Token 的 Obsidian 插件 / 本地 agent 都会立即停止工作，需要重新生成并填入新 Token。')) return;
    setRevoking(true);
    setRevokeMsg(null);
    try {
      const r = await fetch('/api/sync/revoke-all-tokens', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeader() },
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${r.status}`);
      }
      const d = await r.json();
      setRevokeMsg({ ok: true, text: d.message || '已撤销' });
      setIssuedToken(null);
      setTimeout(() => setRevokeMsg(null), 5000);
    } catch (e: any) {
      setRevokeMsg({ ok: false, text: e?.message || '撤销失败' });
    }
    setRevoking(false);
  };

  const handleDownloadPlugin = () => {
    // 插件从其独立仓的 GitHub Releases 分发(本仓不打包构建产物)
    window.open('https://github.com/weaiw/trove-sync-obsidian/releases/latest', '_blank', 'noopener');
  };

  return (
    <div className="bg-[var(--bg-primary)] rounded-xl border border-[var(--border-color)] p-5">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <BookMarked size={18} className="text-[var(--accent)]" />
          <h2 className="font-semibold text-[var(--foreground)]">Obsidian 备份</h2>
        </div>
      </div>
      <p className="text-sm text-[var(--text-secondary)] mb-4">
        把你的文章一次性同步到本地 Obsidian vault —— 防数据丢失。本地修改与服务端更新永不互相覆盖。
      </p>

      {stats && (
        <div className="flex flex-wrap items-center gap-4 text-sm mb-4">
          <div className="flex items-center gap-2">
            <span className="text-[var(--text-tertiary)]">总文章</span>
            <span className="font-medium text-[var(--foreground)]">{stats.total}</span>
          </div>
          <div className="hidden sm:block w-px h-4 bg-[var(--border-color)]" />
          <div className="flex items-center gap-2">
            <span className="text-[var(--text-tertiary)]">可同步（AI 已处理）</span>
            <span className="font-medium text-[var(--foreground)]">{stats.eligible}</span>
          </div>
        </div>
      )}

      <ol className="text-sm space-y-2 pl-5 list-decimal text-[var(--foreground)] mb-4">
        <li className="leading-7">
          <button
            onClick={handleDownloadPlugin}
            className="inline-flex items-center gap-2 text-sm px-3 py-1.5 rounded-lg border border-[var(--border-color)] hover:bg-[var(--bg-tertiary)]"
          >
            <Download size={14} />
            前往插件 Releases
          </button>
          <span className="ml-2 text-xs text-[var(--text-tertiary)]">下载最新版的 main.js / manifest.json / styles.css</span>
        </li>
        <li className="leading-7">
          把这三个文件放到你的 Obsidian vault 目录下的&nbsp;
          <code className="px-1.5 py-0.5 rounded bg-[var(--bg-secondary)] text-xs">.obsidian/plugins/trove-sync/</code>
        </li>
        <li className="leading-7">
          打开 Obsidian → 设置 → 第三方插件 → 启用 <strong>Trove AI Sync</strong>
          （首次需关掉「安全模式」）
        </li>
        <li className="leading-7">
          点下方“生成本地同步 Token”，复制 Token → 进插件设置 → 填服务器地址和 Token → 点 Sync Now
        </li>
      </ol>

      <div className="flex flex-wrap items-center gap-3 mb-4">
        <button
          onClick={handleIssueToken}
          disabled={issuing}
          className="inline-flex items-center gap-2 text-sm px-3 py-1.5 rounded-lg border border-[var(--border-color)] hover:bg-[var(--bg-tertiary)] disabled:opacity-50"
        >
          {issuing ? <Loader2 size={14} className="animate-spin" /> : <KeyRound size={14} />}
          生成本地同步 Token
        </button>
        <button
          onClick={handleRevokeAll}
          disabled={revoking}
          className="inline-flex items-center gap-2 text-sm px-3 py-1.5 rounded-lg text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 disabled:opacity-50"
          title="撤销所有先前签发的同步 Token —— 所有正在运行的 agent / 插件会立即被拒"
        >
          {revoking ? <Loader2 size={14} className="animate-spin" /> : <ShieldOff size={14} />}
          撤销所有同步 Token
        </button>
      </div>

      {issueError && (
        <div className="mb-4 px-3 py-2 rounded-lg bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 flex items-start gap-2 text-xs">
          <AlertCircle size={14} className="text-red-600 mt-0.5 shrink-0" />
          <span className="text-red-800 dark:text-red-200">{issueError}</span>
        </div>
      )}
      {revokeMsg && (
        <div className={`mb-4 px-3 py-2 rounded-lg border flex items-start gap-2 text-xs ${
          revokeMsg.ok
            ? 'bg-green-50 dark:bg-green-900/20 border-green-200 dark:border-green-800'
            : 'bg-red-50 dark:bg-red-900/20 border-red-200 dark:border-red-800'
        }`}>
          {revokeMsg.ok
            ? <CheckCircle2 size={14} className="text-[#34c759] mt-0.5 shrink-0" />
            : <AlertCircle size={14} className="text-red-600 mt-0.5 shrink-0" />}
          <span className={revokeMsg.ok ? 'text-green-800 dark:text-green-200' : 'text-red-800 dark:text-red-200'}>
            {revokeMsg.text}
          </span>
        </div>
      )}

      {issuedToken && (
        <div className="border border-[var(--border-color)] rounded-lg p-4 bg-[var(--bg-secondary)] space-y-3">
          <div className="px-3 py-2 rounded-lg bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800 flex items-start gap-2 text-xs">
            <AlertCircle size={14} className="text-yellow-600 mt-0.5 shrink-0" />
            <span className="text-yellow-800 dark:text-yellow-200">
              Token 仅展示这一次，离开后无法再次查看。请立即复制保存（建议直接粘到 Obsidian 插件设置里）。
            </span>
          </div>
          <div className="p-3 rounded-lg bg-[var(--bg-primary)] border border-[var(--border-color)] font-mono text-xs break-all select-all max-h-32 overflow-y-auto">
            {issuedToken}
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={handleCopyToken}
              className="inline-flex items-center gap-2 text-sm px-3 py-1.5 rounded-lg border border-[var(--border-color)] hover:bg-[var(--bg-tertiary)]"
            >
              <Copy size={14} />
              复制 Token
            </button>
            {copyMsg && <span className="text-xs text-[var(--text-tertiary)]">{copyMsg}</span>}
          </div>
        </div>
      )}
    </div>
  );
}
