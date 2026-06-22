'use client';

import React, { useEffect, useState } from 'react';
import { X, Loader2, Link2, Boxes, Star, Puzzle, Sparkles } from 'lucide-react';
import { api } from '@/lib/api';
import type { GraphInsights } from '@/lib/types';

/**
 * Graph Insights 抽屉(Phase 1·A)。借鉴 llm_wiki:在知识图谱上跑社区检测 + Adamic-Adar,
 * 主动暴露「意外连接」与「知识缺口」。点标题跳到对应文章。
 */
export default function InsightsPanel({
  username,
  onClose,
  onNavigate,
}: {
  username?: string;
  onClose: () => void;
  onNavigate: (id: string) => void;
}) {
  const [data, setData] = useState<GraphInsights | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const r = await api.getInsights(username || undefined);
        if (alive) setData(r);
      } catch (e: any) {
        if (alive) setError(e.message || String(e));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, [username]);

  const titleBtn = (id: string, title: string) => (
    <button
      onClick={() => onNavigate(id)}
      className="text-left text-[#007aff] hover:underline"
    >
      《{title}》
    </button>
  );

  return (
    <div className="fixed inset-0 z-50 flex justify-end" onClick={onClose}>
      <div className="absolute inset-0 bg-black/20" />
      <div
        className="relative w-full max-w-md h-full bg-white shadow-2xl overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="sticky top-0 bg-white border-b border-[#e5e5ea] px-5 py-4 flex items-center justify-between z-10">
          <div className="flex items-center gap-2">
            <Sparkles size={18} className="text-[#007aff]" />
            <h2 className="font-semibold text-[#1d1d1f]">知识洞察</h2>
          </div>
          <button onClick={onClose} className="text-[#aeaeb2] hover:text-[#1d1d1f]">
            <X size={18} />
          </button>
        </div>

        <div className="p-5 space-y-6">
          {loading ? (
            <div className="flex flex-col items-center py-16 text-[#aeaeb2]">
              <Loader2 size={28} className="animate-spin mb-2" />
              <p className="text-sm">正在分析你的知识图谱…</p>
            </div>
          ) : error ? (
            <div className="text-sm text-red-500 py-8 text-center">分析失败：{error}</div>
          ) : !data || data.empty ? (
            <div className="text-sm text-[#6e6e73] py-12 text-center leading-relaxed">
              图谱还太小，跑不出洞察。<br />
              多收藏些内容、点「重新生成」建立关联后再来看。
            </div>
          ) : (
            <>
              {/* Stats */}
              <div className="grid grid-cols-4 gap-2 text-center">
                {[
                  { k: '文章', v: data.stats.articles },
                  { k: '关联', v: data.stats.edges },
                  { k: '主题簇', v: data.stats.communities },
                  { k: '孤岛', v: data.stats.orphans },
                ].map((s) => (
                  <div key={s.k} className="bg-[#f5f5f7] rounded-lg py-2">
                    <div className="text-lg font-bold text-[#1d1d1f]">{s.v}</div>
                    <div className="text-[11px] text-[#aeaeb2]">{s.k}</div>
                  </div>
                ))}
              </div>

              {/* Surprising links */}
              <section>
                <div className="flex items-center gap-1.5 mb-2 text-[#1d1d1f] font-medium text-sm">
                  <Link2 size={15} className="text-[#007aff]" /> 意外连接
                </div>
                {data.surprising_links.length === 0 ? (
                  <p className="text-xs text-[#aeaeb2]">暂无——文章之间还没出现"该连未连"的强关联。</p>
                ) : (
                  <ul className="space-y-2.5">
                    {data.surprising_links.map((s, i) => (
                      <li key={i} className="text-sm leading-relaxed bg-[#f5f5f7] rounded-lg p-3">
                        {s.cross_community && (
                          <span className="inline-block text-[10px] bg-[#007aff] text-white rounded px-1.5 py-0.5 mr-1.5 align-middle">
                            跨主题
                          </span>
                        )}
                        {titleBtn(s.source.id, s.source.title)}
                        <span className="text-[#aeaeb2] mx-1">↔</span>
                        {titleBtn(s.target.id, s.target.title)}
                        <div className="text-[11px] text-[#aeaeb2] mt-1">关联强度 {s.score}</div>
                      </li>
                    ))}
                  </ul>
                )}
              </section>

              {/* Communities */}
              <section>
                <div className="flex items-center gap-1.5 mb-2 text-[#1d1d1f] font-medium text-sm">
                  <Boxes size={15} className="text-[#007aff]" /> 主题簇
                </div>
                {data.communities.length === 0 ? (
                  <p className="text-xs text-[#aeaeb2]">暂无明显聚类。</p>
                ) : (
                  <ul className="space-y-2">
                    {data.communities.map((c) => (
                      <li key={c.id} className="text-sm bg-[#f5f5f7] rounded-lg p-3">
                        <div className="flex items-center justify-between">
                          <span className="font-medium text-[#1d1d1f]">{c.label}</span>
                          <span className="text-[11px] text-[#aeaeb2]">{c.size} 篇</span>
                        </div>
                        <div className="text-xs text-[#6e6e73] mt-1 truncate">
                          {c.sample_titles.join(' · ')}
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </section>

              {/* Hubs */}
              {data.hubs.length > 0 && (
                <section>
                  <div className="flex items-center gap-1.5 mb-2 text-[#1d1d1f] font-medium text-sm">
                    <Star size={15} className="text-[#007aff]" /> 核心枢纽
                  </div>
                  <ul className="space-y-1.5">
                    {data.hubs.map((h) => (
                      <li key={h.id} className="text-sm flex items-center justify-between gap-2">
                        {titleBtn(h.id, h.title)}
                        <span className="text-[11px] text-[#aeaeb2] shrink-0">{h.degree} 关联</span>
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              {/* Gaps */}
              {(data.gaps.orphan_count > 0 || data.gaps.small_topics.length > 0) && (
                <section>
                  <div className="flex items-center gap-1.5 mb-2 text-[#1d1d1f] font-medium text-sm">
                    <Puzzle size={15} className="text-[#007aff]" /> 知识缺口
                  </div>
                  {data.gaps.orphan_count > 0 && (
                    <p className="text-xs text-[#6e6e73] mb-2">
                      {data.gaps.orphan_count} 篇孤岛文章还没建立任何关联：
                    </p>
                  )}
                  <ul className="space-y-1.5">
                    {data.gaps.orphans.map((o) => (
                      <li key={o.id} className="text-sm">{titleBtn(o.id, o.title)}</li>
                    ))}
                  </ul>
                  {data.gaps.small_topics.length > 0 && (
                    <p className="text-xs text-[#aeaeb2] mt-2">
                      过小主题：{data.gaps.small_topics.map((t) => `${t.label}(${t.size})`).join('、')}
                    </p>
                  )}
                </section>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
