'use client';

import React, { useState, useEffect, useCallback, useRef } from 'react';
import {
  Search, X, LayoutGrid, Filter, Check, FolderOpen, ChevronRight,
  PanelLeftClose, PanelLeft, Plus, Loader2, Trash2, Tag, Edit2,
  FolderKanban, MoreHorizontal, GitMerge, Palette
} from 'lucide-react';
import { useSearchParams } from 'next/navigation';
import { api } from '@/lib/api';
import type { Article, Tag as TagType, TagWithCount, Folder, ArticleListResponse } from '@/lib/types';
import ArticleCard from '@/components/ArticleCard';
import AddContentModal from '@/components/AddContentModal';

const SOURCE_OPTIONS = [
  { value: '', label: '全部来源' },
  { value: 'wechat', label: '微信公众号' },
  { value: 'xhs', label: '小红书' },
  { value: 'douyin', label: '抖音' },
  { value: 'bilibili', label: 'B 站' },
  { value: 'juejin', label: '掘金' },
  { value: 'toutiao', label: '今日头条' },
  { value: '36kr', label: '36 氪' },
  { value: 'sspai', label: '少数派' },
  { value: 'jianshu', label: '简书' },
  { value: 'csdn', label: 'CSDN' },
  { value: 'weibo', label: '微博' },
  { value: 'douban', label: '豆瓣' },
  { value: 'medium', label: 'Medium' },
  { value: 'spark', label: 'AI 生成' },
  { value: 'upload', label: '文件上传' },
  { value: 'note', label: '笔记' },
  { value: 'other', label: '其他' },
];

const TAG_COLORS = [
  '#007aff', '#5856d6', '#ff9500', '#34c759', '#ff3b30',
  '#af52de', '#ff2d55', '#00c7be', '#007aff', '#ffcc00',
];

export default function LibraryPage() {
  const searchParams = useSearchParams();
  const [articles, setArticles] = useState<Article[]>([]);
  const [total, setTotal] = useState(0);
  const [searchModeUsed, setSearchModeUsed] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [username, setUsername] = useState('');

  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [tagFilter, setTagFilter] = useState('');
  const [folderFilter, setFolderFilter] = useState('');
  const [sourceFilter, setSourceFilter] = useState('');

  const [tags, setTags] = useState<TagWithCount[]>([]);
  const [folders, setFolders] = useState<Folder[]>([]);

  // Left panel state
  const [showFolderPanel, setShowFolderPanel] = useState(true);
  const [activeLeftTab, setActiveLeftTab] = useState<'folders' | 'tags'>('folders');

  // Folder state
  const [showNewFolder, setShowNewFolder] = useState(false);
  const [newFolderName, setNewFolderName] = useState('');
  const [creating, setCreating] = useState(false);
  const [editingFolderId, setEditingFolderId] = useState<string | null>(null);
  const [editingFolderName, setEditingFolderName] = useState('');

  // Tag management state
  const [tagSearch, setTagSearch] = useState('');
  const [showNewTag, setShowNewTag] = useState(false);
  const [newTagName, setNewTagName] = useState('');
  const [newTagColor, setNewTagColor] = useState(TAG_COLORS[0]);
  const [creatingTag, setCreatingTag] = useState(false);
  const [editingTag, setEditingTag] = useState<string | null>(null);
  const [editTagName, setEditTagName] = useState('');
  const [editTagColor, setEditTagColor] = useState('');
  const [savingTag, setSavingTag] = useState(false);
  const [mergeFromTag, setMergeFromTag] = useState<string | null>(null);

  // Article selection
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [moveToFolderId, setMoveToFolderId] = useState('');
  const [toast, setToast] = useState<{ message: string; type: 'success' | 'error' } | null>(null);
  const [showFilters, setShowFilters] = useState(false);

  // Apply URL search params
  useEffect(() => {
    const urlTag = searchParams.get('tag');
    const urlSource = searchParams.get('source_platform');
    const urlUsername = searchParams.get('username');
    if (urlTag) { setTagFilter(urlTag); setShowFilters(true); } else { setTagFilter(''); }
    if (urlSource) { setSourceFilter(urlSource); setShowFilters(true); } else { setSourceFilter(''); }
    if (urlUsername) { setUsername(urlUsername); } else { setUsername(''); }
  }, [searchParams]);

  const showToast = (m: string, t: 'success' | 'error') => {
    setToast({ message: m, type: t });
    setTimeout(() => setToast(null), 3000);
  };

  const searchRef = useRef(search);
  searchRef.current = search;

  const fetchArticles = useCallback(async () => {
    setLoading(true); setError('');
    try {
      const params: any = { page, page_size: 24, sort: 'created_at' };
      if (statusFilter) params.status = statusFilter;
      if (tagFilter) params.tag = tagFilter;
      if (folderFilter) params.folder_id = folderFilter;
      if (sourceFilter) params.source_platform = sourceFilter;
      if (searchRef.current) { params.search = searchRef.current; params.search_mode = "semantic"; }
      if (username) params.username = username;
      const data = await api.getArticles(params) as ArticleListResponse;
      setArticles(data.items); setTotal(data.total);
      setSearchModeUsed(data.search_mode_used || null);
    } catch (e: any) {
      setError(e.message || '加载失败');
    } finally { setLoading(false); }
  }, [page, statusFilter, tagFilter, folderFilter, sourceFilter, username]);

  const fetchTags = async () => { try { setTags(await api.getTags()); } catch {} };
  const fetchFolders = async () => { try { setFolders(await api.getFolders()); } catch {} };

  const fetchArticlesRef = useRef(fetchArticles);
  fetchArticlesRef.current = fetchArticles;

  useEffect(() => { fetchArticles(); }, [fetchArticles]);
  useEffect(() => { fetchTags(); fetchFolders(); }, []);

  // Poll while any article is still processing. Two scenarios:
  //   1) fetch_status === 'pending_agent' — waiting for local agent (mac may be off).
  //      Longer cap because agent latency is unpredictable.
  //   2) summary missing on a non-note article — AI background task in flight; usually fast.
  // Cap total attempts (not per-render) so re-renders during polling don't extend it.
  const pollAttemptsRef = useRef(0);
  const pendingIdsRef = useRef<string>('');
  useEffect(() => {
    const agentPending = articles.some(a => a.fetch_status === 'pending_agent');
    const summaryPending = articles.some(
      a => !a.summary && a.content_type !== 'note' && a.fetch_status !== 'pending_agent' && a.fetch_status !== 'failed'
    );
    const pendingIds = articles
      .filter(a =>
        a.fetch_status === 'pending_agent' ||
        (!a.summary && a.content_type !== 'note' && a.fetch_status !== 'failed')
      )
      .map(a => a.id)
      .sort()
      .join(',');
    if (pendingIds !== pendingIdsRef.current) {
      pendingIdsRef.current = pendingIds;
      pollAttemptsRef.current = 0;
    }
    if (!pendingIds) return;
    // Agent-pending: poll up to ~5min (60 × 5s). AI summary only: keep old ~32s.
    const cap = agentPending ? 60 : 8;
    const interval = agentPending ? 5000 : 4000;
    if (pollAttemptsRef.current >= cap) return;
    const t = setTimeout(() => {
      pollAttemptsRef.current += 1;
      fetchArticlesRef.current();
    }, interval);
    return () => clearTimeout(t);
  }, [articles]);

  // Debounced search via ref to avoid stale closure
  useEffect(() => { const t = setTimeout(() => { setPage(1); fetchArticlesRef.current(); }, 300); return () => clearTimeout(t); }, [search]);

  const toggleSelect = (id: string) => {
    const next = new Set(selectedIds);
    next.has(id) ? next.delete(id) : next.add(id);
    setSelectedIds(next);
  };

  const batchDelete = async () => {
    if (!confirm(`确定删除选中的 ${selectedIds.size} 篇文章？`)) return;
    for (const id of Array.from(selectedIds)) { try { await api.deleteArticle(id); } catch {} }
    setSelectedIds(new Set()); showToast('批量删除完成', 'success'); fetchArticles();
  };

  const batchArchive = async () => {
    for (const id of Array.from(selectedIds)) { try { await api.updateArticle(id, { status: 'archived' }); } catch {} }
    setSelectedIds(new Set()); showToast('已归档', 'success'); fetchArticles();
  };

  const batchMoveToFolder = async (folderId: string) => {
    const folderName = folders.find(f => f.id === folderId)?.name || '所选文件夹';
    try {
      await api.batchMoveArticles(Array.from(selectedIds), folderId);
      setSelectedIds(new Set()); setMoveToFolderId('');
      showToast(`已移动到「${folderName}」`, 'success'); fetchArticles();
    } catch (e: any) { showToast(e.message || '移动失败', 'error'); }
  };

  // Folder CRUD
  const createFolder = async () => {
    if (!newFolderName.trim()) return;
    setCreating(true);
    try {
      await api.createFolder(newFolderName.trim());
      setNewFolderName(''); setShowNewFolder(false);
      showToast('文件夹已创建', 'success'); fetchFolders();
    } catch (e: any) { showToast(e.message || '创建失败', 'error'); }
    finally { setCreating(false); }
  };

  const deleteFolder = async (id: string) => {
    if (!confirm('确认删除该文件夹？文章将移至根目录。')) return;
    try {
      await api.deleteFolder(id);
      if (folderFilter === id) setFolderFilter('');
      showToast('文件夹已删除', 'success'); fetchFolders();
    } catch (e: any) { showToast(e.message || '删除失败', 'error'); }
  };

  const startEditFolder = (f: Folder) => {
    setEditingFolderId(f.id);
    setEditingFolderName(f.name);
  };

  const cancelEditFolder = () => {
    setEditingFolderId(null);
    setEditingFolderName('');
  };

  const saveEditFolder = async () => {
    if (!editingFolderId) return;
    const name = editingFolderName.trim();
    if (!name) { cancelEditFolder(); return; }
    const original = folders.find(f => f.id === editingFolderId)?.name;
    if (name === original) { cancelEditFolder(); return; }
    try {
      await api.updateFolder(editingFolderId, { name });
      cancelEditFolder();
      showToast('文件夹已重命名', 'success');
      fetchFolders();
    } catch (e: any) {
      showToast(e.message || '重命名失败', 'error');
    }
  };

  // Tag CRUD
  const createTag = async () => {
    if (!newTagName.trim()) return;
    setCreatingTag(true);
    try {
      await api.createTag(newTagName.trim(), newTagColor);
      setNewTagName(''); setShowNewTag(false); setNewTagColor(TAG_COLORS[0]);
      showToast('标签已创建', 'success'); fetchTags();
    } catch (e: any) { showToast(e.message || '创建失败', 'error'); }
    finally { setCreatingTag(false); }
  };

  const updateTag = async (id: string) => {
    if (!editTagName.trim()) return;
    setSavingTag(true);
    try {
      await api.updateTag(id, { name: editTagName.trim(), color: editTagColor });
      setEditingTag(null);
      showToast('标签已更新', 'success'); fetchTags();
    } catch (e: any) { showToast(e.message || '更新失败', 'error'); }
    finally { setSavingTag(false); }
  };

  const deleteTag = async (id: string) => {
    if (!confirm('确定删除该标签？')) return;
    try {
      await api.deleteTag(id);
      if (tagFilter === id) setTagFilter('');
      showToast('标签已删除', 'success'); fetchTags();
    } catch (e: any) { showToast(e.message || '删除失败', 'error'); }
  };

  const mergeTag = async (fromId: string, toId: string) => {
    if (!confirm('确认合并？源标签将被删除。')) return;
    try {
      await api.mergeTags(fromId, toId);
      setMergeFromTag(null);
      if (tagFilter === fromId) setTagFilter(toId);
      showToast('标签已合并', 'success'); fetchTags();
    } catch (e: any) { showToast(e.message || '合并失败', 'error'); }
  };

  const filteredTags = tags.filter(t => t.name.toLowerCase().includes(tagSearch.toLowerCase()));

  const statusTabs = [
    { value: '', label: '全部' },
    { value: 'unread', label: '未读' },
    { value: 'completed', label: '已读' },
    { value: 'favorite', label: '⭐ 收藏' },
    { value: 'archived', label: '已归档' },
  ];

  const selectedFolderName = folders.find(f => f.id === folderFilter)?.name;

  return (
    <>
    <div className="flex h-[calc(100vh-0px)] max-w-7xl mx-auto">
      {/* Toast */}
      {toast && (
        <div className={`fixed top-20 right-8 z-50 px-5 py-3 rounded-xl shadow-lg transition-all ${
          toast.type === 'success' ? 'bg-[#34c759] text-white' : 'bg-[#ff3b30] text-white'
        }`}>{toast.message}</div>
      )}

      {/* Left Panel: Folders + Tags (desktop) */}
      <div className={`hidden md:block border-r border-[var(--border-color)] bg-[var(--bg-primary)] transition-all duration-200 ${
        showFolderPanel ? 'w-60' : 'w-0 overflow-hidden border-r-0'
      }`}>
        <div className="p-3 h-full flex flex-col">
          {/* Tab bar */}
          <div className="flex mb-3 p-0.5 bg-[var(--bg-secondary)] rounded-lg">
            <button
              onClick={() => setActiveLeftTab('folders')}
              className={`flex-1 flex items-center justify-center gap-1.5 py-1.5 text-xs font-medium rounded-md transition-all ${
                activeLeftTab === 'folders'
                  ? 'bg-[var(--bg-primary)] text-[var(--accent)] shadow-sm'
                  : 'text-[var(--text-tertiary)] hover:text-[var(--text-primary)]'
              }`}
            >
              <FolderKanban size={13} /> 文件夹
            </button>
            <button
              onClick={() => setActiveLeftTab('tags')}
              className={`flex-1 flex items-center justify-center gap-1.5 py-1.5 text-xs font-medium rounded-md transition-all ${
                activeLeftTab === 'tags'
                  ? 'bg-[var(--bg-primary)] text-[var(--accent)] shadow-sm'
                  : 'text-[var(--text-tertiary)] hover:text-[var(--text-primary)]'
              }`}
            >
              <Tag size={13} /> 标签
            </button>
          </div>

          {/* Folders Tab */}
          {activeLeftTab === 'folders' && (
            <>
              <div className="flex items-center justify-between mb-2">
                <span className="text-[11px] font-semibold text-[var(--text-tertiary)] uppercase tracking-wider">全部文件夹</span>
                <button onClick={() => setShowNewFolder(!showNewFolder)} className="p-1 rounded hover:bg-[var(--bg-secondary)] text-[var(--text-tertiary)] hover:text-[var(--accent)] transition-colors" title="新建文件夹">
                  <Plus size={14} />
                </button>
              </div>

              {showNewFolder && (
                <div className="flex items-center gap-1 mb-2">
                  <input type="text" value={newFolderName} onChange={e => setNewFolderName(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter') createFolder(); if (e.key === 'Escape') { setShowNewFolder(false); setNewFolderName(''); } }}
                    placeholder="文件夹名称..." autoFocus
                    className="flex-1 px-2 py-1.5 bg-[var(--bg-secondary)] rounded text-xs outline-none focus:ring-2 focus:ring-[var(--accent)]/20" />
                  <button onClick={createFolder} disabled={creating || !newFolderName.trim()} className="p-1 text-[var(--accent)] hover:bg-[var(--accent-light)] rounded disabled:opacity-40">
                    {creating ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
                  </button>
                  <button onClick={() => { setShowNewFolder(false); setNewFolderName(''); }} className="p-1 text-[var(--text-tertiary)] hover:text-[var(--text-primary)] rounded">
                    <X size={12} />
                  </button>
                </div>
              )}

              <button onClick={() => setFolderFilter('')}
                className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-sm transition-colors mb-0.5 ${
                  !folderFilter ? 'bg-[var(--accent-light)] text-[var(--accent)] font-medium' : 'text-[var(--text-secondary)] hover:bg-[var(--bg-secondary)]'
                }`}>
                <LayoutGrid size={14} /> <span className="truncate flex-1 text-left">全部文章</span>
              </button>

              <div className="flex-1 overflow-y-auto mt-1 space-y-0.5">
                {folders.map(f => {
                  const isEditing = editingFolderId === f.id;
                  return (
                    <div key={f.id} className="group relative">
                      {isEditing ? (
                        <div className="flex items-center gap-1 px-2 py-1 rounded-md bg-[var(--bg-secondary)]">
                          <FolderOpen size={14} style={{ color: f.color || 'var(--accent)' }} className="shrink-0" />
                          <input
                            autoFocus
                            value={editingFolderName}
                            onChange={e => setEditingFolderName(e.target.value)}
                            onKeyDown={e => {
                              if (e.key === 'Enter') saveEditFolder();
                              if (e.key === 'Escape') cancelEditFolder();
                            }}
                            onBlur={saveEditFolder}
                            className="flex-1 min-w-0 bg-transparent text-sm outline-none text-[var(--text-primary)]"
                          />
                        </div>
                      ) : (
                        <>
                          <button onClick={() => setFolderFilter(f.id)}
                            className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-sm transition-colors pr-12 ${
                              folderFilter === f.id ? 'bg-[var(--accent-light)] text-[var(--accent)] font-medium' : 'text-[var(--text-secondary)] hover:bg-[var(--bg-secondary)]'
                            }`}>
                            <FolderOpen size={14} style={{ color: f.color || 'var(--accent)' }} />
                            <span className="truncate flex-1 text-left">{f.name}</span>
                            {folderFilter === f.id && <ChevronRight size={12} />}
                          </button>
                          <div className="absolute right-1 top-1/2 -translate-y-1/2 flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                            <button onClick={() => startEditFolder(f)}
                              className="p-1 rounded hover:bg-[var(--bg-tertiary)] text-[var(--text-tertiary)] hover:text-[var(--accent)]"
                              title="重命名">
                              <Edit2 size={11} />
                            </button>
                            <button onClick={() => deleteFolder(f.id)}
                              className="p-1 rounded hover:bg-[var(--danger-light)] text-[var(--text-tertiary)] hover:text-[#ff3b30]"
                              title="删除">
                              <Trash2 size={11} />
                            </button>
                          </div>
                        </>
                      )}
                    </div>
                  );
                })}
              </div>
            </>
          )}

          {/* Tags Tab */}
          {activeLeftTab === 'tags' && (
            <>
              <div className="flex items-center justify-between mb-2">
                <span className="text-[11px] font-semibold text-[var(--text-tertiary)] uppercase tracking-wider">{tags.length} 个标签</span>
                <button onClick={() => setShowNewTag(!showNewTag)} className="p-1 rounded hover:bg-[var(--bg-secondary)] text-[var(--text-tertiary)] hover:text-[var(--accent)] transition-colors" title="新建标签">
                  <Plus size={14} />
                </button>
              </div>

              {/* Tag search + new tag form */}
              <div className="relative mb-2">
                <Search size={12} className="absolute left-2 top-1/2 -translate-y-1/2 text-[var(--text-tertiary)]" />
                <input type="text" value={tagSearch} onChange={e => setTagSearch(e.target.value)}
                  placeholder="搜索标签..." className="w-full pl-7 pr-3 py-1.5 bg-[var(--bg-secondary)] rounded text-xs outline-none focus:ring-1 focus:ring-[var(--accent)]/20" />
              </div>

              {showNewTag && (
                <div className="mb-2 p-2 bg-[var(--bg-secondary)] rounded-lg space-y-2">
                  <input type="text" value={newTagName} onChange={e => setNewTagName(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter') createTag(); if (e.key === 'Escape') { setShowNewTag(false); setNewTagName(''); } }}
                    placeholder="标签名称..." autoFocus className="w-full px-2 py-1.5 bg-[var(--bg-primary)] rounded text-xs outline-none" />
                  <div className="flex gap-1 flex-wrap">
                    {TAG_COLORS.map(c => (
                      <button key={c} onClick={() => setNewTagColor(c)}
                        className={`w-5 h-5 rounded-full border-2 transition-all ${newTagColor === c ? 'border-[var(--text-primary)] scale-110' : 'border-transparent'}`}
                        style={{ backgroundColor: c }} />
                    ))}
                  </div>
                  <div className="flex gap-1">
                    <button onClick={createTag} disabled={creatingTag || !newTagName.trim()}
                      className="flex-1 py-1 text-xs bg-[var(--accent)] text-white rounded hover:bg-[var(--accent-hover)] disabled:opacity-40">
                      {creatingTag ? <Loader2 size={12} className="animate-spin mx-auto" /> : '创建'}
                    </button>
                    <button onClick={() => { setShowNewTag(false); setNewTagName(''); }}
                      className="px-3 py-1 text-xs text-[var(--text-tertiary)] hover:text-[var(--text-primary)] rounded">取消</button>
                  </div>
                </div>
              )}

              <div className="flex-1 overflow-y-auto space-y-1">
                {filteredTags.map(t => (
                  <div key={t.id} className="group relative">
                    {editingTag === t.id ? (
                      <div className="p-2 bg-[var(--bg-secondary)] rounded-lg space-y-2">
                        <input type="text" value={editTagName} onChange={e => setEditTagName(e.target.value)}
                          onKeyDown={e => { if (e.key === 'Enter') updateTag(t.id); if (e.key === 'Escape') setEditingTag(null); }}
                          autoFocus className="w-full px-2 py-1 bg-[var(--bg-primary)] rounded text-xs outline-none" />
                        <div className="flex gap-1 flex-wrap">
                          {TAG_COLORS.map(c => (
                            <button key={c} onClick={() => setEditTagColor(c)}
                              className={`w-5 h-5 rounded-full border-2 ${editTagColor === c ? 'border-[var(--text-primary)] scale-110' : 'border-transparent'}`}
                              style={{ backgroundColor: c }} />
                          ))}
                        </div>
                        <div className="flex gap-1">
                          <button onClick={() => updateTag(t.id)} disabled={savingTag}
                            className="flex-1 py-1 text-xs bg-[var(--accent)] text-white rounded disabled:opacity-40">
                            {savingTag ? <Loader2 size={10} className="animate-spin mx-auto" /> : '保存'}
                          </button>
                          <button onClick={() => setEditingTag(null)} className="px-2 py-1 text-xs rounded hover:bg-[var(--bg-tertiary)]">取消</button>
                        </div>
                      </div>
                    ) : (
                      <div
                        onClick={() => {
                          setTagFilter(tagFilter === t.name ? '' : t.name);
                          if (activeLeftTab === 'tags') setShowFilters(true);
                        }}
                        className={`flex items-center gap-2 px-2 py-1.5 rounded-md text-sm cursor-pointer transition-colors pr-16 ${
                          tagFilter === t.name
                            ? 'bg-[var(--accent-light)] text-[var(--accent)] font-medium'
                            : 'text-[var(--text-secondary)] hover:bg-[var(--bg-secondary)]'
                        }`}>
                        <div className="w-2.5 h-2.5 rounded-full flex-shrink-0" style={{ backgroundColor: t.color || '#007aff' }} />
                        <span className="truncate flex-1">{t.name}</span>
                        {t.article_count !== undefined && (
                          <span className="text-[10px] text-[var(--text-tertiary)] bg-[var(--bg-tertiary)] px-1.5 py-0.5 rounded-full">{t.article_count}</span>
                        )}
                        {/* Hover actions */}
                        <div className="absolute right-1 top-1/2 -translate-y-1/2 flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                          {mergeFromTag === t.id ? (
                            <>
                              <span className="text-[10px] text-[var(--text-tertiary)]">合并到:</span>
                              <select
                                size={1}
                                onChange={e => { if (e.target.value) mergeTag(t.id, e.target.value); setMergeFromTag(null); }}
                                className="text-[10px] bg-[var(--bg-primary)] rounded border border-[var(--border-color)] px-1 py-0.5"
                                onClick={e => e.stopPropagation()}
                              >
                                <option value="">选择...</option>
                                {tags.filter(ot => ot.id !== t.id).map(ot => (
                                  <option key={ot.id} value={ot.id}>{ot.name}</option>
                                ))}
                              </select>
                              <button onClick={e => { e.stopPropagation(); setMergeFromTag(null); }} className="p-0.5 text-[var(--text-tertiary)]">
                                <X size={10} />
                              </button>
                            </>
                          ) : (
                            <>
                              <button onClick={e => { e.stopPropagation(); setEditingTag(t.id); setEditTagName(t.name); setEditTagColor(t.color || '#007aff'); }}
                                className="p-0.5 rounded hover:bg-[var(--bg-tertiary)] text-[var(--text-tertiary)] hover:text-[var(--accent)]" title="编辑">
                                <Edit2 size={10} />
                              </button>
                              <button onClick={e => { e.stopPropagation(); setMergeFromTag(t.id); }}
                                className="p-0.5 rounded hover:bg-[var(--bg-tertiary)] text-[var(--text-tertiary)] hover:text-[var(--accent)]" title="合并">
                                <GitMerge size={10} />
                              </button>
                              <button onClick={e => { e.stopPropagation(); deleteTag(t.id); }}
                                className="p-0.5 rounded hover:bg-[var(--danger-light)] text-[var(--text-tertiary)] hover:text-[#ff3b30]" title="删除">
                                <Trash2 size={10} />
                              </button>
                            </>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                ))}
                {filteredTags.length === 0 && !showNewTag && (
                  <div className="text-center py-8 text-[var(--text-tertiary)] text-xs">
                    {tagSearch ? '无匹配标签' : '暂无标签'}
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </div>

      {/* Right: Main content */}
      <div className="flex-1 min-w-0 p-4 md:p-6 overflow-y-auto">
        {/* Mobile folder selector */}
        <div className="md:hidden mb-4">
          <select value={folderFilter} onChange={e => setFolderFilter(e.target.value)}
            className="w-full px-3 py-2 bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-lg text-sm outline-none text-[var(--text-primary)]">
            <option value="">📁 全部文件夹</option>
            {folders.map(f => <option key={f.id} value={f.id}>{f.name}</option>)}
          </select>
        </div>

        {/* Header */}
        <div className="flex flex-col md:flex-row items-start md:items-center justify-between mb-6 gap-4">
          <div className="flex items-center gap-3">
            <button onClick={() => setShowFolderPanel(!showFolderPanel)}
              className="hidden md:flex p-1.5 rounded-lg hover:bg-[var(--bg-secondary)] text-[var(--text-tertiary)] hover:text-[var(--text-primary)] transition-colors"
              title={showFolderPanel ? '收起面板' : '展开面板'}>
              {showFolderPanel ? <PanelLeftClose size={18} /> : <PanelLeft size={18} />}
            </button>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-2xl font-bold text-[var(--text-primary)]">知识库</h1>
                {selectedFolderName && (
                  <span className="flex items-center gap-1 px-2 py-0.5 bg-[var(--accent-light)] text-[var(--accent)] text-xs font-medium rounded-full">
                    <FolderOpen size={12} /> {selectedFolderName}
                  </span>
                )}
                {tagFilter && (
                  <span className="flex items-center gap-1 px-2 py-0.5 bg-[var(--accent-light)] text-[var(--accent)] text-xs font-medium rounded-full">
                    <Tag size={12} /> {tagFilter}
                  </span>
                )}
              </div>
              <p className="text-sm text-[var(--text-tertiary)] mt-1">{total} 篇文章{searchModeUsed === "hybrid (keyword-only)" && <span className="text-amber-600 dark:text-amber-400 ml-2">语义搜索暂时不可用，已降级为关键词搜索</span>}</p>
            </div>
          </div>
          <div className="flex gap-2">
            <button onClick={() => setShowFilters(!showFilters)}
              className="flex items-center gap-2 px-4 py-2 text-sm border border-[var(--border-color)] rounded-lg hover:bg-[var(--bg-secondary)] transition-colors">
              <Filter size={16} /> 筛选
            </button>
          </div>
        </div>

        {/* Search & Filter Bar */}
        <div className="bg-[var(--bg-primary)] rounded-xl p-4 mb-6 border border-[var(--border-color)] space-y-4">
          <div className="relative">
            <Search size={18} className="absolute left-3 top-1/2 -translate-y-1/2 text-[var(--text-tertiary)]" />
            <input type="text" value={search} onChange={e => setSearch(e.target.value)}
              placeholder="搜索文章标题或内容..."
              className="w-full pl-10 pr-4 py-2.5 bg-[var(--bg-secondary)] rounded-lg text-sm outline-none focus:ring-2 focus:ring-[#007aff]/20 transition-all" />
          </div>

          <div className="flex gap-1 p-1 bg-[var(--bg-secondary)] rounded-lg">
            {statusTabs.map(tab => (
              <button key={tab.value} onClick={() => { setStatusFilter(tab.value); setPage(1); }}
                className={`flex-1 py-2 text-sm rounded-md transition-all ${
                  statusFilter === tab.value ? 'bg-[var(--bg-primary)] text-[var(--accent)] shadow-sm font-medium' : 'text-[var(--text-secondary)] hover:text-[var(--text-primary)]'
                }`}>{tab.label}</button>
            ))}
          </div>

          {showFilters && (
            <div className="flex flex-wrap gap-3 pt-2 border-t border-[var(--border-color)]">
              <select value={tagFilter} onChange={e => { setTagFilter(e.target.value); setPage(1); }}
                className="px-3 py-2 bg-[var(--bg-secondary)] rounded-lg text-sm outline-none text-[var(--text-primary)]">
                <option value="">所有标签</option>
                {tags.map(t => <option key={t.id} value={t.name}>{t.name}</option>)}
              </select>
              <select value={sourceFilter} onChange={e => { setSourceFilter(e.target.value); setPage(1); }}
                className="px-3 py-2 bg-[var(--bg-secondary)] rounded-lg text-sm outline-none text-[var(--text-primary)]">
                {SOURCE_OPTIONS.map(opt => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
              </select>
              {(tagFilter || sourceFilter) && (
                <button onClick={() => { setTagFilter(''); setSourceFilter(''); setPage(1); }}
                  className="px-3 py-2 text-sm text-[var(--text-tertiary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-secondary)] rounded-lg transition-colors flex items-center gap-1">
                  <X size={14} /> 清除筛选
                </button>
              )}
            </div>
          )}
        </div>

        {/* Batch Actions */}
        {selectedIds.size > 0 && (
          <div className="flex items-center gap-3 mb-4 p-3 bg-[var(--accent-light)] rounded-xl">
            <span className="text-sm font-medium text-[var(--accent)]">已选 {selectedIds.size} 篇</span>
            <button onClick={batchArchive} className="px-3 py-1.5 text-xs bg-[var(--bg-primary)] rounded-lg hover:shadow-sm">归档</button>
            <div className="relative">
              <button
                onClick={() => setMoveToFolderId(moveToFolderId ? '' : '__open__')}
                className="px-3 py-1.5 text-xs bg-[var(--accent)] text-white rounded-lg hover:bg-[var(--accent-hover)] flex items-center gap-1"
              >
                <FolderOpen size={13} /> 移动到
              </button>
              {moveToFolderId === '__open__' && (
                <div className="absolute top-full mt-1 left-0 bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-lg shadow-lg z-20 py-1 min-w-[160px] max-h-48 overflow-y-auto">
                  <button
                    onClick={() => { batchMoveToFolder(''); }}
                    className="w-full text-left px-3 py-1.5 text-xs text-[var(--text-secondary)] hover:bg-[var(--bg-secondary)]"
                  >📁 根目录（无文件夹）</button>
                  {folders.map(f => (
                    <button
                      key={f.id}
                      onClick={() => batchMoveToFolder(f.id)}
                      className="w-full text-left px-3 py-1.5 text-xs text-[var(--text-secondary)] hover:bg-[var(--bg-secondary)] flex items-center gap-1.5"
                    >
                      <FolderOpen size={12} style={{ color: f.color || 'var(--accent)' }} />
                      {f.name}
                    </button>
                  ))}
                </div>
              )}
            </div>
            <button onClick={batchDelete} className="px-3 py-1.5 text-xs bg-[#ff3b30] text-white rounded-lg hover:bg-[#e0352b]">删除</button>
            <button onClick={() => setSelectedIds(new Set())} className="ml-auto text-sm text-[var(--text-secondary)] hover:text-[var(--text-primary)]"><X size={16} /></button>
          </div>
        )}

        {/* Article Grid */}
        {loading ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {Array.from({ length: 9 }).map((_, i) => (
              <div key={i} className="bg-[var(--bg-primary)] rounded-xl p-5 border border-[var(--border-color)] animate-pulse">
                <div className="h-5 bg-[var(--bg-secondary)] rounded w-3/4 mb-3" />
                <div className="h-4 bg-[var(--bg-secondary)] rounded w-full mb-2" />
                <div className="h-4 bg-[var(--bg-secondary)] rounded w-2/3 mb-3" />
                <div className="flex gap-2 mb-3"><div className="h-5 bg-[var(--bg-secondary)] rounded-full w-16" /><div className="h-5 bg-[var(--bg-secondary)] rounded-full w-16" /></div>
                <div className="h-3 bg-[var(--bg-secondary)] rounded w-1/2" />
              </div>
            ))}
          </div>
        ) : error ? (
          <div className="text-center py-16">
            <p className="text-[#ff3b30] mb-4">{error}</p>
            <button onClick={fetchArticles} className="px-6 py-2 bg-[var(--accent)] text-white rounded-lg hover:bg-[var(--accent-hover)]">重试</button>
          </div>
        ) : articles.length === 0 ? (
          <div className="text-center py-16">
            <div className="w-20 h-20 mx-auto mb-4 rounded-2xl bg-[var(--bg-secondary)] flex items-center justify-center">
              <LayoutGrid size={32} className="text-[var(--text-tertiary)]" />
            </div>
            <p className="text-[var(--text-secondary)] text-lg mb-2">{folderFilter ? `「${selectedFolderName}」文件夹为空` : '还没有文章'}</p>
            <p className="text-[var(--text-tertiary)] text-sm">{folderFilter ? '在知识库中为文章分配到此文件夹' : '粘贴文章链接开始构建你的知识库'}</p>
          </div>
        ) : (
          <>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
              {articles.map(article => (
                <div key={article.id} className="relative group">
                  <div className="absolute top-3 left-3 z-10">
                    <button onClick={(e) => { e.preventDefault(); toggleSelect(article.id); }}
                      className={`w-5 h-5 rounded border-2 flex items-center justify-center transition-all ${
                        selectedIds.has(article.id) ? 'bg-[var(--accent)] border-[#007aff]' : 'border-[#aeaeb2] opacity-0 group-hover:opacity-100'
                      }`}>
                      {selectedIds.has(article.id) && <Check size={12} className="text-white" />}
                    </button>
                  </div>
                  <ArticleCard article={article} />
                </div>
              ))}
            </div>
            {total > 24 && (
              <div className="flex justify-center items-center gap-2 mt-8">
                <button onClick={() => setPage(p => Math.max(1, p - 1))} disabled={page === 1}
                  className="px-4 py-2 text-sm rounded-lg disabled:opacity-30 hover:bg-[var(--bg-secondary)]">上一页</button>
                {Array.from({ length: Math.min(5, Math.ceil(total / 24)) }, (_, i) => {
                  const current = Math.max(1, page - 2) + i;
                  if (current > Math.ceil(total / 24)) return null;
                  return (<button key={current} onClick={() => setPage(current)}
                    className={`w-9 h-9 text-sm rounded-lg ${page === current ? 'bg-[var(--accent)] text-white' : 'hover:bg-[var(--bg-secondary)]'}`}>{current}</button>);
                })}
                <button onClick={() => setPage(p => p + 1)} disabled={page >= Math.ceil(total / 24)}
                  className="px-4 py-2 text-sm rounded-lg disabled:opacity-30 hover:bg-[var(--bg-secondary)]">下一页</button>
              </div>
            )}
          </>
        )}
      </div>
    </div>
    <AddContentModal onSuccess={fetchArticles} />
    </>
  );
}