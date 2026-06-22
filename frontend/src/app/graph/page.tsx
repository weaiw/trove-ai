'use client';

import React, { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import dynamic from 'next/dynamic';
import { useSearchParams } from 'next/navigation';
import {
  RefreshCw, Search, Filter, GitGraph, ExternalLink, Loader2,
  Box, Square, Focus, Star, X, ChevronDown, Sparkles,
} from 'lucide-react';
import { api } from '@/lib/api';
import type { GraphData, GraphNode, KnowledgeEdge } from '@/lib/types';
import InsightsPanel from '@/components/InsightsPanel';

const ForceGraph2D = dynamic(() => import('react-force-graph-2d'), { ssr: false });
const ForceGraph3D = dynamic(() => import('react-force-graph-3d'), { ssr: false });

const TAG_PALETTE = [
  '#007aff', '#34c759', '#ff9500', '#ff3b30', '#af52de',
  '#5ac8fa', '#ffcc00', '#ff2d55', '#5856d6', '#00c7be',
  '#a2845e', '#8e8e93',
];

function hashColor(key: string): string {
  if (!key) return '#aeaeb2';
  let h = 0;
  for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) | 0;
  return TAG_PALETTE[Math.abs(h) % TAG_PALETTE.length];
}

const REL_LABEL: Record<string, string> = {
  related: '相关', prerequisite: '前置', extends: '延伸', contradicts: '对立',
};
const REL_COLOR: Record<string, string> = {
  related: '#aeaeb2', prerequisite: '#ff9500', extends: '#007aff', contradicts: '#ff3b30',
};

type FGNode = GraphNode & { degree: number; primaryTag: string; color: string; x?: number; y?: number; z?: number };
type FGLink = KnowledgeEdge & { source: string | FGNode; target: string | FGNode };
type FGData = { nodes: FGNode[]; links: FGLink[] };

export default function GraphPage() {
  const searchParams = useSearchParams();
  const viewUsername = searchParams.get('username') || '';

  const [raw, setRaw] = useState<GraphData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selectedNode, setSelectedNode] = useState<FGNode | null>(null);
  const [toast, setToast] = useState<{ message: string; type: 'success' | 'error' } | null>(null);

  // View controls
  const [is3D, setIs3D] = useState(false);
  const [hubOnly, setHubOnly] = useState(false);
  const [focusMode, setFocusMode] = useState(false);
  const [focusDepth, setFocusDepth] = useState(1);
  const [search, setSearch] = useState('');
  const [showFilters, setShowFilters] = useState(false);
  const [tagFilter, setTagFilter] = useState<Set<string>>(new Set());
  const [highlightId, setHighlightId] = useState<string | null>(null);
  const [showInsights, setShowInsights] = useState(false);

  const graphRef = useRef<any>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 800, h: 600 });

  const fetchGraph = async () => {
    setLoading(true);
    setError('');
    try {
      const data = (await api.getGraph(viewUsername || undefined)) as GraphData;
      setRaw(data);
    } catch (e: any) {
      setError(e.message || 'Failed to load graph');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchGraph(); }, []);

  // Resize observer
  useEffect(() => {
    if (!containerRef.current) return;
    const ro = new ResizeObserver(entries => {
      const r = entries[0].contentRect;
      setSize({ w: r.width, h: r.height });
    });
    ro.observe(containerRef.current);
    return () => ro.disconnect();
  }, []);

  // Build derived nodes (with degree + tag color) and full link set
  const fullData: FGData | null = useMemo(() => {
    if (!raw) return null;
    const degree: Record<string, number> = {};
    raw.edges.forEach(e => {
      degree[e.source] = (degree[e.source] || 0) + 1;
      degree[e.target] = (degree[e.target] || 0) + 1;
    });
    const nodes: FGNode[] = raw.nodes.map(n => {
      const primaryTag = (n.tags && n.tags[0]) || '';
      return {
        ...n,
        degree: degree[n.id] || 0,
        primaryTag,
        color: hashColor(primaryTag),
      };
    });
    const links: FGLink[] = raw.edges.map(e => ({ ...e }));
    return { nodes, links };
  }, [raw]);

  // All distinct tags (for filter chips)
  const allTags = useMemo(() => {
    if (!raw) return [];
    const counts = new Map<string, number>();
    raw.nodes.forEach(n => (n.tags || []).forEach(t => counts.set(t, (counts.get(t) || 0) + 1)));
    return Array.from(counts.entries()).sort((a, b) => b[1] - a[1]).slice(0, 30);
  }, [raw]);

  // Filtered/focused data
  const viewData: FGData | null = useMemo(() => {
    if (!fullData) return null;
    let nodes = fullData.nodes;
    let links = fullData.links;

    // Tag filter
    if (tagFilter.size > 0) {
      nodes = nodes.filter(n => (n.tags || []).some(t => tagFilter.has(t)));
    }
    // Hub-only
    if (hubOnly) {
      nodes = nodes.filter(n => n.degree >= 2);
    }
    // Focus mode (BFS from selected node)
    if (focusMode && selectedNode) {
      const visited = new Set<string>([selectedNode.id]);
      let frontier = new Set<string>([selectedNode.id]);
      for (let d = 0; d < focusDepth; d++) {
        const next = new Set<string>();
        fullData.links.forEach(l => {
          const s = typeof l.source === 'string' ? l.source : (l.source as FGNode).id;
          const t = typeof l.target === 'string' ? l.target : (l.target as FGNode).id;
          if (frontier.has(s) && !visited.has(t)) { next.add(t); visited.add(t); }
          if (frontier.has(t) && !visited.has(s)) { next.add(s); visited.add(s); }
        });
        frontier = next;
        if (next.size === 0) break;
      }
      nodes = nodes.filter(n => visited.has(n.id));
    }

    const nodeIds = new Set(nodes.map(n => n.id));
    links = links.filter(l => {
      const s = typeof l.source === 'string' ? l.source : (l.source as FGNode).id;
      const t = typeof l.target === 'string' ? l.target : (l.target as FGNode).id;
      return nodeIds.has(s) && nodeIds.has(t);
    });
    return { nodes, links };
  }, [fullData, hubOnly, focusMode, focusDepth, selectedNode, tagFilter]);

  // Search results (for dropdown)
  const searchResults = useMemo(() => {
    if (!search.trim() || !fullData) return [];
    const q = search.toLowerCase();
    return fullData.nodes
      .filter(n => n.title?.toLowerCase().includes(q) || (n.tags || []).some(t => t.toLowerCase().includes(q)))
      .slice(0, 8);
  }, [search, fullData]);

  const flyTo = useCallback((node: FGNode) => {
    setHighlightId(node.id);
    setSelectedNode(node);
    setTimeout(() => {
      const g = graphRef.current;
      if (!g) return;
      if (is3D) {
        const dist = 120;
        const distRatio = 1 + dist / Math.hypot(node.x || 1, node.y || 1, node.z || 1);
        g.cameraPosition(
          { x: (node.x || 0) * distRatio, y: (node.y || 0) * distRatio, z: (node.z || 0) * distRatio },
          { x: node.x || 0, y: node.y || 0, z: node.z || 0 },
          800,
        );
      } else {
        if (typeof node.x === 'number' && typeof node.y === 'number') {
          g.centerAt(node.x, node.y, 800);
          g.zoom(3, 800);
        }
      }
    }, 50);
    setTimeout(() => setHighlightId(null), 2500);
  }, [is3D]);

  const showToast = (message: string, type: 'success' | 'error') => {
    setToast({ message, type });
    setTimeout(() => setToast(null), 3000);
  };

  const regenerate = async () => {
    try {
      await api.regenerateGraph(viewUsername || undefined);
      showToast('已开始后台重新生成，约 30-60s 后自动刷新', 'success');
      // Poll graph every 10s up to 6 times (60s window) — backend processes in batches
      let attempts = 0;
      const poll = setInterval(() => {
        attempts += 1;
        fetchGraph();
        if (attempts >= 6) clearInterval(poll);
      }, 10000);
    } catch {
      showToast('重新生成失败', 'error');
    }
  };

  const onSearchSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (searchResults[0]) flyTo(searchResults[0]);
  };

  const toggleTag = (tag: string) => {
    setTagFilter(prev => {
      const next = new Set(prev);
      next.has(tag) ? next.delete(tag) : next.add(tag);
      return next;
    });
  };

  const clearFilters = () => {
    setTagFilter(new Set());
    setHubOnly(false);
    setFocusMode(false);
  };

  // Common props for both 2D and 3D
  const commonGraphProps = {
    graphData: viewData || { nodes: [], links: [] },
    nodeLabel: (n: any) => `${n.title || ''}\n${(n.tags || []).join(' · ')}`,
    nodeRelSize: 6,
    nodeVal: (n: any) => Math.max(1, Math.sqrt((n as FGNode).degree + 1)),
    linkColor: (l: any) => REL_COLOR[l.relation_type] || '#d1d1d6',
    linkWidth: (l: any) => Math.max(0.5, (l.weight || 0.3) * 2),
    linkDirectionalArrowLength: 4,
    linkDirectionalArrowRelPos: 1,
    onNodeClick: (n: any) => {
      setSelectedNode(n as FGNode);
      flyTo(n as FGNode);
    },
    onBackgroundClick: () => setSelectedNode(null),
    cooldownTicks: 100,
    width: size.w,
    height: size.h,
  };

  const node2DCanvas = (n: any, ctx: CanvasRenderingContext2D, scale: number) => {
    const node = n as FGNode;
    const r = Math.max(3, Math.sqrt(node.degree + 1) * 2.5);
    const isHi = highlightId === node.id;
    const isSel = selectedNode?.id === node.id;
    ctx.beginPath();
    ctx.arc(n.x, n.y, r + (isHi ? 4 : 0), 0, 2 * Math.PI);
    ctx.fillStyle = node.color;
    ctx.globalAlpha = isSel || isHi ? 1 : 0.85;
    ctx.fill();
    if (isSel || isHi) {
      ctx.strokeStyle = '#1d1d1f';
      ctx.lineWidth = 2 / scale;
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
    if (scale > 1.2) {
      ctx.fillStyle = '#1d1d1f';
      ctx.font = `${Math.max(10, 11 / scale)}px -apple-system, system-ui, sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      const label = (node.title || '').slice(0, 14);
      ctx.fillText(label, n.x, n.y + r + 2);
    }
  };

  const isEmpty = !loading && !error && (!raw || raw.nodes.length === 0);

  return (
    <div className="p-4 md:p-8 max-w-7xl mx-auto h-[calc(100vh-2rem)]">
      {/* Toast */}
      {toast && (
        <div className={`fixed top-20 right-8 z-50 px-5 py-3 rounded-xl shadow-lg ${
          toast.type === 'success' ? 'bg-[#34c759] text-white' : 'bg-[#ff3b30] text-white'
        }`}>
          {toast.message}
        </div>
      )}

      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
        <div>
          <h1 className="text-2xl font-bold text-[#1d1d1f]">知识图谱</h1>
          <p className="text-sm text-[#aeaeb2] mt-1">
            {viewData
              ? `${viewData.nodes.length}/${raw?.nodes.length ?? 0} 节点 · ${viewData.links.length}/${raw?.edges.length ?? 0} 关系`
              : '加载中...'}
          </p>
        </div>

        <div className="flex items-center gap-2">
          {/* Search */}
          <form onSubmit={onSearchSubmit} className="relative">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-[#aeaeb2]" />
            <input
              type="text"
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="搜索节点 / 标签..."
              className="pl-9 pr-3 py-2 text-sm bg-[#f5f5f7] rounded-lg border border-transparent focus:border-[#007aff] focus:bg-white outline-none w-56"
            />
            {searchResults.length > 0 && search && (
              <div className="absolute top-full mt-1 left-0 right-0 bg-white rounded-lg shadow-xl border border-[#e5e5ea] z-30 max-h-72 overflow-auto">
                {searchResults.map(n => (
                  <button
                    key={n.id}
                    type="button"
                    onClick={() => { flyTo(n); setSearch(''); }}
                    className="w-full text-left px-3 py-2 hover:bg-[#f5f5f7] flex items-center gap-2 text-sm"
                  >
                    <div className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: n.color }} />
                    <span className="truncate">{n.title}</span>
                    <span className="text-[10px] text-[#aeaeb2] ml-auto">{n.degree}</span>
                  </button>
                ))}
              </div>
            )}
          </form>

          {/* 2D / 3D toggle */}
          <div className="flex bg-[#f5f5f7] rounded-lg p-0.5">
            <button
              onClick={() => setIs3D(false)}
              className={`px-2.5 py-1.5 rounded-md text-xs flex items-center gap-1 ${
                !is3D ? 'bg-white shadow-sm text-[#1d1d1f]' : 'text-[#6e6e73]'
              }`}
              title="2D"
            >
              <Square size={12} /> 2D
            </button>
            <button
              onClick={() => setIs3D(true)}
              className={`px-2.5 py-1.5 rounded-md text-xs flex items-center gap-1 ${
                is3D ? 'bg-white shadow-sm text-[#1d1d1f]' : 'text-[#6e6e73]'
              }`}
              title="3D"
            >
              <Box size={12} /> 3D
            </button>
          </div>

          {/* Quick toggles */}
          <button
            onClick={() => setHubOnly(v => !v)}
            className={`p-2 rounded-lg text-xs flex items-center gap-1 ${
              hubOnly ? 'bg-[#007aff] text-white' : 'bg-[#f5f5f7] text-[#6e6e73] hover:bg-[#ebebf0]'
            }`}
            title="只看枢纽节点（连接 ≥ 2）"
          >
            <Star size={12} /> 枢纽
          </button>

          <button
            onClick={() => setFocusMode(v => !v)}
            disabled={!selectedNode}
            className={`p-2 rounded-lg text-xs flex items-center gap-1 ${
              focusMode ? 'bg-[#007aff] text-white' : 'bg-[#f5f5f7] text-[#6e6e73] hover:bg-[#ebebf0] disabled:opacity-40'
            }`}
            title={selectedNode ? `局部图谱（${focusDepth} 跳邻居）` : '先选中一个节点'}
          >
            <Focus size={12} /> 局部
          </button>

          {focusMode && (
            <select
              value={focusDepth}
              onChange={e => setFocusDepth(Number(e.target.value))}
              className="text-xs bg-[#f5f5f7] rounded-lg px-2 py-2 outline-none"
            >
              <option value={1}>1 跳</option>
              <option value={2}>2 跳</option>
              <option value={3}>3 跳</option>
            </select>
          )}

          <button
            onClick={() => setShowFilters(v => !v)}
            className={`p-2 rounded-lg text-xs flex items-center gap-1 ${
              showFilters || tagFilter.size > 0 ? 'bg-[#007aff] text-white' : 'bg-[#f5f5f7] text-[#6e6e73] hover:bg-[#ebebf0]'
            }`}
            title="标签过滤"
          >
            <Filter size={12} /> 过滤{tagFilter.size > 0 ? `(${tagFilter.size})` : ''}
            <ChevronDown size={10} className={showFilters ? 'rotate-180 transition-transform' : 'transition-transform'} />
          </button>

          <button
            onClick={() => setShowInsights(true)}
            className="flex items-center gap-1.5 px-3 py-2 text-xs bg-[#f5f5f7] text-[#6e6e73] rounded-lg hover:bg-[#ebebf0] transition-colors"
            title="社区聚类 / 意外连接 / 知识缺口"
          >
            <Sparkles size={12} /> 洞察
          </button>

          <button
            onClick={regenerate}
            className="flex items-center gap-1.5 px-3 py-2 text-xs bg-[#007aff] text-white rounded-lg hover:bg-[#0062cc] transition-colors"
          >
            <RefreshCw size={12} /> 重新生成
          </button>
        </div>
      </div>

      {showInsights && (
        <InsightsPanel
          username={viewUsername || undefined}
          onClose={() => setShowInsights(false)}
          onNavigate={(id) => { window.location.href = `/read/${id}`; }}
        />
      )}

      {/* Filter panel */}
      {showFilters && (
        <div className="mb-3 p-3 bg-white border border-[#e5e5ea] rounded-xl">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-medium text-[#6e6e73]">按标签过滤（点击多选）</span>
            {(tagFilter.size > 0 || hubOnly || focusMode) && (
              <button
                onClick={clearFilters}
                className="text-xs text-[#007aff] hover:text-[#0062cc] flex items-center gap-1"
              >
                <X size={12} /> 清空
              </button>
            )}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {allTags.map(([tag, count]) => {
              const active = tagFilter.has(tag);
              return (
                <button
                  key={tag}
                  onClick={() => toggleTag(tag)}
                  className={`px-2.5 py-1 rounded-full text-xs flex items-center gap-1 transition-colors ${
                    active
                      ? 'text-white'
                      : 'bg-[#f5f5f7] text-[#6e6e73] hover:bg-[#ebebf0]'
                  }`}
                  style={active ? { backgroundColor: hashColor(tag) } : undefined}
                >
                  {tag}
                  <span className="text-[10px] opacity-70">{count}</span>
                </button>
              );
            })}
          </div>
        </div>
      )}

      {/* Graph Container */}
      <div
        ref={containerRef}
        className="relative bg-white rounded-2xl border border-[#e5e5ea] overflow-hidden"
        style={{ height: 'calc(100vh - 240px)' }}
      >
        {loading && (
          <div className="absolute inset-0 flex flex-col items-center justify-center z-10">
            <Loader2 size={40} className="animate-spin text-[#007aff] mb-4" />
            <p className="text-[#6e6e73]">加载知识图谱...</p>
          </div>
        )}
        {error && !loading && (
          <div className="absolute inset-0 flex flex-col items-center justify-center">
            <p className="text-[#ff3b30] mb-4">{error}</p>
            <button onClick={fetchGraph} className="px-6 py-2 bg-[#007aff] text-white rounded-lg">重试</button>
          </div>
        )}
        {isEmpty && (
          <div className="absolute inset-0 flex flex-col items-center justify-center">
            <div className="w-20 h-20 rounded-2xl bg-[#f5f5f7] flex items-center justify-center mb-4">
              <GitGraph size={32} className="text-[#aeaeb2]" />
            </div>
            <p className="text-[#6e6e73] text-lg mb-2">知识图谱为空</p>
            <p className="text-[#aeaeb2] text-sm mb-4">添加更多文章后，AI 会自动构建知识图谱</p>
          </div>
        )}

        {viewData && !loading && !error && raw && raw.nodes.length > 0 && (
          is3D ? (
            <ForceGraph3D
              ref={graphRef}
              {...commonGraphProps}
              backgroundColor="#0a0a0f"
              nodeColor={(n: any) => (n as FGNode).color}
              nodeOpacity={0.95}
              linkOpacity={0.5}
            />
          ) : (
            <ForceGraph2D
              ref={graphRef}
              {...commonGraphProps}
              backgroundColor="#fafafa"
              nodeCanvasObject={node2DCanvas}
              nodeCanvasObjectMode={() => 'replace'}
            />
          )
        )}

        {/* Node Detail Panel */}
        {selectedNode && (
          <div className="absolute top-4 right-4 w-72 bg-white rounded-xl shadow-xl border border-[#e5e5ea] p-5 z-20">
            <div className="flex items-start justify-between mb-3">
              <div className="flex items-center gap-2 min-w-0">
                <div className="w-3 h-3 rounded-full shrink-0" style={{ backgroundColor: selectedNode.color }} />
                <h3 className="font-semibold text-sm truncate">{selectedNode.title}</h3>
              </div>
              <button
                onClick={() => setSelectedNode(null)}
                className="p-1 hover:bg-[#f5f5f7] rounded shrink-0"
              >
                <X size={14} />
              </button>
            </div>
            <div className="text-[10px] text-[#aeaeb2] mb-3">连接数 {selectedNode.degree}</div>
            {selectedNode.summary && (
              <p className="text-xs text-[#6e6e73] mb-3 line-clamp-3">{selectedNode.summary}</p>
            )}
            {selectedNode.tags?.length > 0 && (
              <div className="flex flex-wrap gap-1 mb-3">
                {selectedNode.tags.map((tag, i) => (
                  <span
                    key={i}
                    className="px-2 py-0.5 rounded-full text-[10px] text-white"
                    style={{ backgroundColor: hashColor(tag) }}
                  >
                    {tag}
                  </span>
                ))}
              </div>
            )}
            <div className="flex gap-2 mb-3">
              <a
                href={`/read/${selectedNode.id}`}
                className="flex-1 flex items-center justify-center gap-1.5 text-xs px-3 py-1.5 bg-[#007aff] text-white rounded-lg hover:bg-[#0062cc]"
              >
                <ExternalLink size={11} /> 查看文章
              </a>
              <button
                onClick={() => setFocusMode(true)}
                className="flex items-center justify-center gap-1.5 text-xs px-3 py-1.5 bg-[#f5f5f7] text-[#6e6e73] rounded-lg hover:bg-[#ebebf0]"
              >
                <Focus size={11} /> 聚焦
              </button>
            </div>
            {raw && (
              <div className="border-t border-[#e5e5ea] pt-3">
                <p className="text-[10px] text-[#aeaeb2] mb-2 uppercase tracking-wider">Related Articles</p>
                {raw.edges
                  .filter(e => e.source === selectedNode.id || e.target === selectedNode.id)
                  .slice(0, 6)
                  .map(edge => {
                    const otherId = edge.source === selectedNode.id ? edge.target : edge.source;
                    const otherNode = fullData?.nodes.find(n => n.id === otherId);
                    return otherNode ? (
                      <button
                        key={edge.id}
                        onClick={() => flyTo(otherNode)}
                        className="w-full flex items-center justify-between py-1.5 hover:bg-[#f5f5f7] rounded px-1 -mx-1 text-left"
                      >
                        <span className="text-xs text-[#6e6e73] truncate flex-1">{otherNode.title}</span>
                        <span
                          className="text-[10px] px-1.5 py-0.5 rounded-full ml-2 shrink-0"
                          style={{
                            backgroundColor: (REL_COLOR[edge.relation_type] || '#aeaeb2') + '22',
                            color: REL_COLOR[edge.relation_type] || '#6e6e73',
                          }}
                        >
                          {REL_LABEL[edge.relation_type] || edge.relation_type}
                        </span>
                      </button>
                    ) : null;
                  })}
              </div>
            )}
          </div>
        )}

        {/* Legend */}
        <div className="absolute bottom-4 left-4 flex gap-3 px-3 py-2 bg-white/85 backdrop-blur rounded-lg border border-[#e5e5ea]">
          {(['related', 'prerequisite', 'extends', 'contradicts'] as const).map(type => (
            <div key={type} className="flex items-center gap-1.5 text-[11px] text-[#6e6e73]">
              <div className="w-4 h-0.5 rounded" style={{ backgroundColor: REL_COLOR[type] }} />
              {REL_LABEL[type]}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
