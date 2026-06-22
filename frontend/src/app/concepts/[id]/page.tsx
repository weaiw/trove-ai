'use client';

import React, { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { useParams, useRouter } from 'next/navigation';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ArrowLeft, Loader2, RefreshCw, Trash2, FileText } from 'lucide-react';
import { api } from '@/lib/api';
import type { ConceptDetail } from '@/lib/types';

export default function ConceptDetailPage() {
  const params = useParams();
  const router = useRouter();
  const id = params?.id as string;

  const [page, setPage] = useState<ConceptDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [regenerating, setRegenerating] = useState(false);
  const [err, setErr] = useState('');

  const load = useCallback(async () => {
    try {
      setPage(await api.getConcept(id));
    } catch (e: any) {
      setErr(e.message || String(e));
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  const handleRegenerate = async () => {
    setRegenerating(true);
    setErr('');
    try {
      setPage(await api.regenerateConcept(id));
    } catch (e: any) {
      setErr(e.message || String(e));
    } finally {
      setRegenerating(false);
    }
  };

  const toggleAuto = async () => {
    if (!page) return;
    const next = !page.auto_update;
    setPage({ ...page, auto_update: next });  // optimistic
    try {
      setPage(await api.setConceptAutoUpdate(id, next));
    } catch (e: any) {
      setErr(e.message || String(e));
      setPage(await api.getConcept(id));      // revert
    }
  };

  const handleDelete = async () => {
    if (!confirm('删除这篇概念词条？(不影响原始文章)')) return;
    try {
      await api.deleteConcept(id);
      router.push('/concepts');
    } catch (e: any) {
      setErr(e.message || String(e));
    }
  };

  if (loading) {
    return (
      <div className="p-8 flex items-center gap-2 text-[var(--text-tertiary)]">
        <Loader2 size={16} className="animate-spin" /> 加载中…
      </div>
    );
  }
  if (!page) {
    return <div className="p-8 text-sm text-red-500">{err || '未找到该概念词条。'}</div>;
  }

  return (
    <div className="p-6 md:p-8 max-w-3xl mx-auto">
      <Link href="/concepts" className="inline-flex items-center gap-1 text-sm text-[var(--text-secondary)] hover:text-[var(--accent)] mb-4">
        <ArrowLeft size={15} /> 概念词条
      </Link>

      <div className="flex items-start justify-between gap-4 mb-2">
        <h1 className="text-2xl font-bold text-[var(--text-primary)]">{page.name}</h1>
        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={handleRegenerate}
            disabled={regenerating}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg border border-[var(--border-color)] hover:bg-[var(--bg-tertiary)] disabled:opacity-50"
          >
            <RefreshCw size={13} className={regenerating ? 'animate-spin' : ''} /> {regenerating ? '合成中…' : '重新合成'}
          </button>
          <button
            onClick={handleDelete}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg border border-[var(--border-color)] text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20"
          >
            <Trash2 size={13} /> 删除
          </button>
        </div>
      </div>

      <div className="text-xs text-[var(--text-tertiary)] mb-3">
        {page.source_count} 篇来源 · {page.seed_type === 'tag' ? `标签「${page.seed_tag}」` : '自定义主题'}
        {page.updated_at && ` · 更新于 ${new Date(page.updated_at).toLocaleString('zh-CN', { hour12: false })}`}
      </div>

      {/* Auto-update toggle */}
      <label className="flex items-center gap-2 mb-4 cursor-pointer w-fit" onClick={toggleAuto}>
        <span
          className={`relative inline-block w-9 h-5 rounded-full transition-colors ${
            page.auto_update ? 'bg-[var(--accent)]' : 'bg-[var(--border-color)]'
          }`}
        >
          <span
            className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white shadow transition-transform ${
              page.auto_update ? 'translate-x-4' : ''
            }`}
          />
        </span>
        <span className="text-xs text-[var(--text-secondary)]">
          有新相关内容时自动重新合成{page.auto_update ? '（已开启，会消耗 AI 额度）' : '（关闭时仅提醒，不自动跑）'}
        </span>
      </label>

      {page.stale && (
        <div className="mb-4 px-3 py-2 rounded-lg bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800 flex items-center justify-between gap-2 text-xs">
          <span className="text-yellow-800 dark:text-yellow-200">
            有 {page.new_source_count} 篇新内容命中这个概念，词条可能已过时。
          </span>
          <button onClick={handleRegenerate} disabled={regenerating} className="text-[var(--accent)] hover:underline shrink-0">
            重新合成
          </button>
        </div>
      )}

      {err && <div className="text-xs text-red-500 mb-3">{err}</div>}

      {/* Synthesized content */}
      <div className="reader-content">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{page.content || ''}</ReactMarkdown>
      </div>

      {/* Sources (citation numbers map to these in order) */}
      {page.sources.length > 0 && (
        <div className="mt-8 pt-5 border-t border-[var(--border-color)]">
          <h3 className="text-sm font-semibold text-[var(--text-primary)] mb-2 flex items-center gap-1.5">
            <FileText size={15} className="text-[var(--accent)]" /> 来源（引用编号对应）
          </h3>
          <ol className="space-y-1.5 text-sm list-decimal pl-5">
            {page.sources.map((s) => (
              <li key={s.id}>
                <Link href={`/read/${s.id}`} className="text-[var(--accent)] hover:underline">
                  {s.title}
                </Link>
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  );
}
