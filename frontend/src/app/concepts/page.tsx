'use client';

import React, { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { Library, Loader2, Sparkles, RefreshCw, AlertCircle, Plus } from 'lucide-react';
import { api } from '@/lib/api';
import type { ConceptSummary, ConceptSuggestion, ConceptAnalyze } from '@/lib/types';

export default function ConceptsPage() {
  const router = useRouter();
  const [concepts, setConcepts] = useState<ConceptSummary[]>([]);
  const [suggestions, setSuggestions] = useState<ConceptSuggestion[]>([]);
  const [loading, setLoading] = useState(true);

  // New-concept flow state
  const [name, setName] = useState('');
  const [tag, setTag] = useState<string | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [analysis, setAnalysis] = useState<ConceptAnalyze | null>(null);
  const [creating, setCreating] = useState(false);
  const [err, setErr] = useState('');

  const refresh = useCallback(async () => {
    try {
      const [c, s] = await Promise.all([api.listConcepts(), api.conceptSuggestions()]);
      setConcepts(c);
      setSuggestions(s);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const runAnalyze = async (n: string, t: string | null) => {
    if (!n.trim()) return;
    setName(n);
    setTag(t);
    setAnalysis(null);
    setErr('');
    setAnalyzing(true);
    try {
      setAnalysis(await api.analyzeConcept(n.trim(), t ?? undefined));
    } catch (e: any) {
      setErr(e.message || String(e));
    } finally {
      setAnalyzing(false);
    }
  };

  const doCreate = async (body: { name?: string; seed_type: string; seed_tag?: string | null; article_ids?: string[] | null }) => {
    setCreating(true);
    setErr('');
    try {
      const page = await api.createConcept({ name: name.trim(), ...body });
      router.push(`/concepts/${page.id}`);
    } catch (e: any) {
      setErr(e.message || String(e));
      setCreating(false);
    }
  };

  const resetFlow = () => {
    setName('');
    setTag(null);
    setAnalysis(null);
    setErr('');
  };

  return (
    <div className="p-6 md:p-8 max-w-5xl mx-auto space-y-8">
      <div>
        <h1 className="text-2xl font-bold text-[var(--text-primary)] flex items-center gap-2">
          <Library size={24} className="text-[var(--accent)]" /> 概念词条
        </h1>
        <p className="text-sm text-[var(--text-secondary)] mt-1">
          把同一概念散落在多篇文章里的知识，合成一篇带来源引用的"活词条"。宽泛的标签会自动拆成更聚焦的子概念。
        </p>
      </div>

      {/* New concept */}
      <div className="bg-[var(--bg-primary)] rounded-xl border border-[var(--border-color)] p-5">
        <div className="flex items-center gap-2 mb-3">
          <Plus size={18} className="text-[var(--accent)]" />
          <h2 className="font-semibold text-[var(--text-primary)]">新建概念词条</h2>
        </div>

        <div className="flex gap-2">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && runAnalyze(name, tag)}
            placeholder="输入一个概念/主题，如「RAG 检索增强」「提示词工程」"
            className="flex-1 px-3 py-2 text-sm rounded-lg border border-[var(--border-color)] bg-[var(--bg-secondary)] text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent)]"
          />
          <button
            onClick={() => runAnalyze(name, tag)}
            disabled={analyzing || !name.trim()}
            className="px-4 py-2 text-sm rounded-lg bg-[var(--accent)] text-white hover:bg-[var(--accent-hover)] disabled:opacity-50 inline-flex items-center gap-1.5"
          >
            {analyzing ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />} 分析来源
          </button>
        </div>

        {/* Suggestions */}
        {!analysis && suggestions.length > 0 && (
          <div className="mt-3">
            <div className="text-xs text-[var(--text-tertiary)] mb-1.5">或从你的高频标签里挑一个：</div>
            <div className="flex flex-wrap gap-1.5">
              {suggestions.filter((s) => !s.has_page).slice(0, 16).map((s) => (
                <button
                  key={s.tag}
                  onClick={() => runAnalyze(s.tag, s.tag)}
                  className="px-2.5 py-1 text-xs rounded-full bg-[var(--bg-secondary)] text-[var(--text-secondary)] border border-[var(--border-color)] hover:text-[var(--accent)] hover:border-[var(--accent)]"
                >
                  {s.tag} <span className="opacity-60">{s.article_count}</span>
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Analysis result */}
        {analysis && (
          <div className="mt-4 border-t border-[var(--border-color)] pt-4">
            <div className="flex items-center justify-between mb-2">
              <span className="text-sm text-[var(--text-primary)] font-medium">「{name}」</span>
              <button onClick={resetFlow} className="text-xs text-[var(--text-tertiary)] hover:text-[var(--text-primary)]">重选</button>
            </div>

            {creating ? (
              <div className="flex items-center gap-2 text-sm text-[var(--text-secondary)] py-4">
                <Loader2 size={16} className="animate-spin" /> 正在合成词条（LLM 融合多篇来源）…
              </div>
            ) : analysis.needs_split ? (
              <div>
                <div className="flex items-start gap-2 text-xs text-[var(--text-secondary)] mb-3">
                  <AlertCircle size={14} className="text-[var(--accent)] mt-0.5 shrink-0" />
                  这个范围有点宽，包含几个不同子主题。选一个聚焦合成，或仍按整体合成一页：
                </div>
                <div className="space-y-2">
                  {analysis.clusters?.map((c, i) => (
                    <button
                      key={i}
                      onClick={() => doCreate({ name: c.label, seed_type: 'topic', seed_tag: null, article_ids: c.article_ids })}
                      className="w-full text-left p-3 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-color)] hover:border-[var(--accent)] transition-colors"
                    >
                      <div className="flex items-center justify-between">
                        <span className="font-medium text-sm text-[var(--text-primary)]">{c.label}</span>
                        <span className="text-xs text-[var(--text-tertiary)]">{c.size} 篇</span>
                      </div>
                      <div className="text-xs text-[var(--text-tertiary)] mt-1 truncate">{c.sample_titles.join(' · ')}</div>
                    </button>
                  ))}
                  <button
                    onClick={() => doCreate({ seed_type: tag ? 'tag' : 'topic', seed_tag: tag })}
                    className="text-xs text-[var(--accent)] hover:underline mt-1"
                  >
                    仍把「{name}」整体合成为一页 →
                  </button>
                </div>
              </div>
            ) : (analysis.source_count ?? 0) < 2 ? (
              <div className="text-sm text-[var(--text-secondary)] py-2">
                相关文章不足（{analysis.source_count ?? 0} 篇），至少要 2 篇才能合成。换个更宽的主题或多收藏些内容。
              </div>
            ) : (
              <div>
                <div className="text-sm text-[var(--text-secondary)] mb-3">
                  找到 <span className="font-medium text-[var(--text-primary)]">{analysis.source_count}</span> 篇连贯来源，可直接合成。
                </div>
                <button
                  onClick={() => doCreate({ seed_type: tag ? 'tag' : 'topic', seed_tag: tag })}
                  className="px-4 py-2 text-sm rounded-lg bg-[var(--accent)] text-white hover:bg-[var(--accent-hover)] inline-flex items-center gap-1.5"
                >
                  <Sparkles size={14} /> 合成概念词条
                </button>
              </div>
            )}
            {err && <div className="text-xs text-red-500 mt-2">{err}</div>}
          </div>
        )}
        {err && !analysis && <div className="text-xs text-red-500 mt-2">{err}</div>}
      </div>

      {/* Existing concepts */}
      <div>
        <h2 className="font-semibold text-[var(--text-primary)] mb-3">已合成的词条</h2>
        {loading ? (
          <div className="flex items-center gap-2 text-sm text-[var(--text-tertiary)]">
            <Loader2 size={14} className="animate-spin" /> 加载中…
          </div>
        ) : concepts.length === 0 ? (
          <div className="text-sm text-[var(--text-tertiary)] py-8 text-center border border-dashed border-[var(--border-color)] rounded-xl">
            还没有概念词条。上面挑个主题合成第一篇吧。
          </div>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            {concepts.map((c) => (
              <Link
                key={c.id}
                href={`/concepts/${c.id}`}
                className="block p-4 rounded-xl bg-[var(--bg-primary)] border border-[var(--border-color)] hover:border-[var(--accent)] transition-colors"
              >
                <div className="flex items-center justify-between">
                  <span className="font-medium text-[var(--text-primary)]">{c.name}</span>
                  {c.stale && (
                    <span className="text-[10px] bg-[var(--accent)] text-white rounded px-1.5 py-0.5 inline-flex items-center gap-1">
                      <RefreshCw size={9} /> {c.new_source_count} 篇新内容
                    </span>
                  )}
                </div>
                <div className="text-xs text-[var(--text-tertiary)] mt-1">
                  {c.source_count} 篇来源 · {c.seed_type === 'tag' ? `标签「${c.seed_tag}」` : '自定义主题'}
                </div>
              </Link>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
