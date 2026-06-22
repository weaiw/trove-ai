'use client';

import React, { useState, useEffect, useCallback, useRef } from 'react';
import dynamic from 'next/dynamic';
import { useRouter } from 'next/navigation';
import Link from 'next/link';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';

const NoteEditor = dynamic(() => import('@/components/NoteEditor'), {
  ssr: false,
  loading: () => <div className="text-sm text-[var(--text-tertiary)] py-8 text-center">加载编辑器...</div>,
});
import {
  ArrowLeft,
  Star,
  Archive,
  Trash2,
  Pencil,
  RefreshCw,
  Clock,
  BookOpen,
  Tag,
  Folder,
  ChevronDown,
  Share2,
  Loader2,
  AlertCircle,
  CheckCircle2,
  ExternalLink,
  Search,
  Plus,
  X,
  GitGraph,
  GitFork,
} from 'lucide-react';
import { api } from '@/lib/api';
import type { ArticleDetail, Folder as FolderType, Tag as TagType, RelatedArticlesResponse } from '@/lib/types';
import { format, parseISO } from 'date-fns';
import { zhCN } from 'date-fns/locale';

// ─── Constants ───────────────────────────────────────────────────────────────

const STATUS_LABELS: Record<string, string> = {
  unread: '未读',
  reading: '阅读中',
  completed: '已读完',
  archived: '已归档',
};

const STATUS_STYLES: Record<string, string> = {
  unread: 'bg-[#fff3e0] text-[#ff9500]',
  reading: 'bg-[#e8f2ff] text-[#007aff]',
  completed: 'bg-[#e8f8ed] text-[#34c759]',
  archived: 'bg-[#f2f2f7] text-[#6e6e73]',
};

const PLATFORM_STYLES: Record<string, string> = {
  wechat: 'bg-[#e8f8ed] text-[#07c160]',
  juejin: 'bg-[#e8f2ff] text-[#1e80ff]',
  medium: 'bg-[#f2f2f7] text-[#1d1d1f]',
  github: 'bg-[#f2f2f7] text-[#1d1d1f]',
  default: 'bg-[#f2f2f7] text-[#6e6e73]',
};

// ─── Helpers ─────────────────────────────────────────────────────────────────

function formatReadingTime(minutes: number): string {
  if (minutes <= 0) return '不到 1 分钟';
  if (minutes < 60) return `${minutes} 分钟`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m > 0 ? `${h} 小时 ${m} 分钟` : `${h} 小时`;
}

function formatDate(dateStr?: string): string {
  if (!dateStr) return '';
  try {
    const d = parseISO(dateStr);
    return format(d, 'yyyy年M月d日', { locale: zhCN });
  } catch {
    return dateStr;
  }
}

function getPlatformStyle(platform?: string): string {
  if (!platform) return PLATFORM_STYLES.default;
  const key = platform.toLowerCase();
  return PLATFORM_STYLES[key] || PLATFORM_STYLES.default;
}

function getPlatformLabel(platform?: string): string {
  if (!platform) return '网页';
  const map: Record<string, string> = {
    wechat: '微信公众号',
    juejin: '掘金',
    medium: 'Medium',
    github: 'GitHub',
  };
  return map[platform.toLowerCase()] || platform;
}

// ─── Skeleton Component ──────────────────────────────────────────────────────

function ReaderSkeleton() {
  return (
    <div className="max-w-3xl mx-auto py-6 px-4 sm:px-6 sm:py-10 animate-fade-in">
      {/* Title skeleton */}
      <div className="loading-pulse mb-8">
        <div className="h-10 bg-[#e8e8ed] rounded-lg w-3/4 mb-3" />
        <div className="h-5 bg-[#e8e8ed] rounded w-1/2 mb-2" />
        <div className="flex gap-3 mb-4">
          <div className="h-4 w-20 bg-[#e8e8ed] rounded" />
          <div className="h-4 w-16 bg-[#e8e8ed] rounded" />
          <div className="h-4 w-24 bg-[#e8e8ed] rounded" />
        </div>
        <div className="h-52 bg-[#e8e8ed] rounded-xl w-full mb-6" />
      </div>

      {/* Summary skeleton */}
      <div className="loading-pulse mb-8">
        <div className="bg-white rounded-2xl p-6 border border-[#e5e5ea] shadow-sm">
          <div className="h-5 bg-[#e8e8ed] rounded w-20 mb-3" />
          <div className="h-4 bg-[#e8e8ed] rounded w-full mb-2" />
          <div className="h-4 bg-[#e8e8ed] rounded w-5/6 mb-2" />
          <div className="h-4 bg-[#e8e8ed] rounded w-2/3" />
        </div>
      </div>

      {/* Content skeleton */}
      <div className="bg-white rounded-2xl p-8 border border-[#e5e5ea] shadow-sm loading-pulse">
        <div className="h-4 bg-[#e8e8ed] rounded w-full mb-3" />
        <div className="h-4 bg-[#e8e8ed] rounded w-full mb-3" />
        <div className="h-4 bg-[#e8e8ed] rounded w-4/5 mb-3" />
        <div className="h-4 bg-[#e8e8ed] rounded w-full mb-3" />
        <div className="h-4 bg-[#e8e8ed] rounded w-2/3 mb-6" />
        <div className="h-4 bg-[#e8e8ed] rounded w-full mb-3" />
        <div className="h-4 bg-[#e8e8ed] rounded w-full mb-3" />
        <div className="h-4 bg-[#e8e8ed] rounded w-3/4 mb-3" />
        <div className="h-4 bg-[#e8e8ed] rounded w-full mb-3" />
        <div className="h-4 bg-[#e8e8ed] rounded w-5/6 mb-6" />
        <div className="h-4 bg-[#e8e8ed] rounded w-full mb-3" />
        <div className="h-4 bg-[#e8e8ed] rounded w-2/3 mb-3" />
        <div className="h-4 bg-[#e8e8ed] rounded w-full mb-3" />
        <div className="h-4 bg-[#e8e8ed] rounded w-4/5" />
      </div>
    </div>
  );
}

// ─── Page Component ──────────────────────────────────────────────────────────

export default function ReaderPage({ params }: { params: { id: string } }) {
  const router = useRouter();
  const contentRef = useRef<HTMLDivElement>(null);

  // ── State ──────────────────────────────────────────────────────────────
  const [article, setArticle] = useState<ArticleDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [summaryOpen, setSummaryOpen] = useState(true);
  const [favorited, setFavorited] = useState(false);
  const [status, setStatus] = useState<string>('unread');
  const [readingProgress, setReadingProgress] = useState(0);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [toast, setToast] = useState<{ message: string; type: 'success' | 'error' } | null>(null);
  const [folders, setFolders] = useState<FolderType[]>([]);
  const [folderOpen, setFolderOpen] = useState(false);
  const folderRef = useRef<HTMLDivElement>(null);

  // Tag editor state
  const [showTagEditor, setShowTagEditor] = useState(false);
  const [editTags, setEditTags] = useState<{ id: string; name: string; color: string; is_ai_generated: boolean }[]>([]);
  const [allTags, setAllTags] = useState<TagType[]>([]);
  const [tagSearch, setTagSearch] = useState('');
  const [tagSaving, setTagSaving] = useState(false);

  // Related articles state
  const [relatedArticles, setRelatedArticles] = useState<RelatedArticlesResponse | null>(null);

  // Title editing state
  const [isEditingTitle, setIsEditingTitle] = useState(false);
  const [editTitleValue, setEditTitleValue] = useState("");
  const [titleSaving, setTitleSaving] = useState(false);

  // Note content editing state
  const [editMode, setEditMode] = useState(false);
  const [editContent, setEditContent] = useState('');
  const [savingContent, setSavingContent] = useState(false);
  const [splitView, setSplitView] = useState(true);

  // ── Toast helper ───────────────────────────────────────────────────────
  const showToast = useCallback((message: string, type: 'success' | 'error') => {
    setToast({ message, type });
    setTimeout(() => setToast(null), 3000);
  }, []);

  const handleSaveContent = useCallback(async () => {
    if (!article || savingContent) return;
    setSavingContent(true);
    try {
      const updated = await api.updateArticleContent(article.id, editContent);
      setArticle({ ...article, clean_content: editContent, word_count: updated.word_count });
      setEditMode(false);
      showToast('已保存', 'success');
    } catch (err: any) {
      showToast(err.message || '保存失败', 'error');
    } finally {
      setSavingContent(false);
    }
  }, [article, editContent, savingContent, showToast]);

  const handleCancelEdit = useCallback(() => {
    if (!article) return;
    setEditContent(article.clean_content || article.raw_content || '');
    setEditMode(false);
  }, [article]);

  // ── Fetch article ──────────────────────────────────────────────────────
  const fetchArticle = useCallback(async () => {
    setLoading(true);
    setError(null);
    setNotFound(false);
    try {
      const data = (await api.getArticle(params.id)) as ArticleDetail;
      setArticle(data);
      setFavorited(data.is_favorited);
      setStatus(data.status);
      setEditContent(data.clean_content || data.raw_content || '');
      // Auto-enter edit mode for notes when ?edit=1 is present in URL
      if (data.content_type === 'note' && typeof window !== 'undefined') {
        const sp = new URLSearchParams(window.location.search);
        if (sp.get('edit') === '1') setEditMode(true);
      }
      // Mark as reading if currently unread
      if (data.status === 'unread') {
        api.updateArticle(params.id, { status: 'reading' }).catch(() => {});
        setStatus('reading');
      }
    } catch (err: any) {
      const msg = err.message || '加载文章失败';
      if (msg.includes('404') || msg.includes('not found') || msg.includes('Not Found')) {
        setNotFound(true);
      } else {
        setError(msg);
      }
    } finally {
      setLoading(false);
    }
  }, [params.id]);

  // ── Fetch folders ──────────────────────────────────────────────────────
  const fetchFolders = useCallback(async () => {
    try {
      const data = (await api.getFolders()) as FolderType[];
      setFolders(Array.isArray(data) ? data : []);
    } catch {
      // Silently fail for folders
    }
  }, []);

  useEffect(() => {
    fetchArticle();
    fetchFolders();
  }, [fetchArticle, fetchFolders]);

  // Auto-poll while article is pending agent fetch OR AI is still summarizing.
  // Cap total attempts so we don't poll forever when the agent is offline.
  const pollRef = useRef(0);
  const lastPollStateRef = useRef<string>('');
  useEffect(() => {
    if (!article) return;
    const pending = article.fetch_status === 'pending_agent';
    const summarizing =
      !article.summary &&
      article.content_type !== 'note' &&
      article.fetch_status !== 'failed' &&
      article.fetch_status !== 'pending_agent';
    if (!pending && !summarizing) return;
    // Reset counter when state changes (e.g. just transitioned from pending → summarizing)
    const stateKey = `${article.fetch_status || ''}|${article.summary ? 'has' : 'no'}`;
    if (stateKey !== lastPollStateRef.current) {
      lastPollStateRef.current = stateKey;
      pollRef.current = 0;
    }
    const cap = pending ? 60 : 8;        // ~5min agent / ~32s AI
    const interval = pending ? 5000 : 4000;
    if (pollRef.current >= cap) return;
    const t = setTimeout(() => {
      pollRef.current += 1;
      fetchArticle();
    }, interval);
    return () => clearTimeout(t);
  }, [article, fetchArticle]);

  // ── Scroll progress ────────────────────────────────────────────────────
  useEffect(() => {
    const handleScroll = () => {
      if (!contentRef.current) return;
      const element = contentRef.current;
      const scrollTop = window.scrollY - element.offsetTop + window.innerHeight / 2;
      const scrollHeight = element.scrollHeight;
      if (scrollHeight <= 0) return;
      const progress = Math.min(100, Math.max(0, (scrollTop / scrollHeight) * 100));
      setReadingProgress(Math.round(progress));
    };

    window.addEventListener('scroll', handleScroll, { passive: true });
    return () => window.removeEventListener('scroll', handleScroll);
  }, [loading]);

  // Sync article tags to editor when article loads
  useEffect(() => {
    if (article?.tags) {
      setEditTags(article.tags.map(t => ({ id: t.id, name: t.name, color: t.color, is_ai_generated: t.is_ai_generated })));
    }
  }, [article]);

  // Fetch all available tags for autocomplete
  useEffect(() => {
    const fetchTags = async () => {
      try { const data = await api.getTags() as TagType[]; setAllTags(data); } catch {}
    };
    fetchTags();
  }, []);

  // Fetch related articles
  useEffect(() => {
    const fetchRelated = async () => {
      try {
        const data = await api.getRelatedArticles(params.id) as RelatedArticlesResponse;
        setRelatedArticles(data);
      } catch {
        // Silently fail — related articles are optional
      }
    };
    fetchRelated();
  }, [params.id]);

  // ── Close folder dropdown on outside click ────────────────────────────
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (folderRef.current && !folderRef.current.contains(e.target as Node)) {
        setFolderOpen(false);
      }
    };
    if (folderOpen) {
      document.addEventListener('mousedown', handleClickOutside);
    }
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [folderOpen]);

  // ── Actions ────────────────────────────────────────────────────────────
  const goBack = useCallback(() => {
    router.back();
  }, [router]);

  const handleToggleFavorite = useCallback(async () => {
    if (!article) return;
    setActionLoading('favorite');
    try {
      await api.updateArticle(params.id, { is_favorited: !favorited });
      setFavorited(!favorited);
      showToast(!favorited ? '已收藏' : '已取消收藏', 'success');
    } catch (err: any) {
      showToast(err.message || '操作失败', 'error');
    } finally {
      setActionLoading(null);
    }
  }, [article, favorited, params.id, showToast]);

  const handleToggleArchive = useCallback(async () => {
    if (!article) return;
    setActionLoading('archive');
    const newStatus = status === 'archived' ? 'reading' : 'archived';
    try {
      await api.updateArticle(params.id, { status: newStatus });
      setStatus(newStatus);
      showToast(newStatus === 'archived' ? '已归档' : '已取消归档', 'success');
    } catch (err: any) {
      showToast(err.message || '操作失败', 'error');
    } finally {
      setActionLoading(null);
    }
  }, [article, status, params.id, showToast]);

  const handleMarkCompleted = useCallback(async () => {
    setActionLoading('complete');
    const newStatus = status === 'completed' ? 'reading' : 'completed';
    try {
      await api.updateArticle(params.id, { status: newStatus });
      setStatus(newStatus);
      showToast(newStatus === 'completed' ? '已标记为读完' : '已标记为阅读中', 'success');
    } catch (err: any) {
      showToast(err.message || '操作失败', 'error');
    } finally {
      setActionLoading(null);
    }
  }, [params.id, status, showToast]);

  const handleDelete = useCallback(async () => {
    if (!confirm('确定要删除这篇文章吗？此操作不可撤销。')) return;
    setActionLoading('delete');
    try {
      await api.deleteArticle(params.id);
      showToast('文章已删除', 'success');
      setTimeout(() => router.push('/'), 500);
    } catch (err: any) {
      showToast(err.message || '删除失败', 'error');
      setActionLoading(null);
    }
  }, [params.id, router, showToast]);

  const handleReprocess = useCallback(async () => {
    if (!confirm('确定要重新解析这篇文章吗？AI 将重新生成摘要和关键点。')) return;
    setActionLoading('reprocess');
    try {
      await api.reprocessArticle(params.id);
      showToast('AI 重新解析已启动，请稍后刷新查看', 'success');
      // Refetch after a short delay
      setTimeout(() => {
        fetchArticle();
        setActionLoading(null);
      }, 2000);
    } catch (err: any) {
      showToast(err.message || '重新解析失败', 'error');
      setActionLoading(null);
    }
  }, [params.id, fetchArticle, showToast]);

  // ── Tag Management ───────────────────────────────────────────────────
  const handleRemoveTag = useCallback((tagId: string) => {
    setEditTags(prev => prev.filter(t => t.id !== tagId));
  }, []);

  const handleAddTag = useCallback((tag: TagType) => {
    setEditTags(prev => {
      if (prev.some(t => t.id === tag.id)) return prev;
      return [...prev, { id: tag.id, name: tag.name, color: tag.color, is_ai_generated: tag.is_ai_generated }];
    });
    setTagSearch('');
  }, []);

  const handleCreateAndAddTag = useCallback(async () => {
    if (!tagSearch.trim()) return;
    try {
      const newTag = await api.createTag(tagSearch.trim()) as TagType;
      setAllTags(prev => [...prev, newTag]);
      setEditTags(prev => [...prev, { id: newTag.id, name: newTag.name, color: newTag.color, is_ai_generated: false }]);
      setTagSearch('');
      showToast(`标签「${newTag.name}」已创建`, 'success');
    } catch (err: any) {
      showToast(err.message || '创建标签失败', 'error');
    }
  }, [tagSearch, showToast]);

  const handleSaveTags = useCallback(async () => {
    setTagSaving(true);
    try {
      const tagIds = editTags.map(t => t.id);
      await api.updateArticleTags(params.id, tagIds);
      showToast('标签已更新', 'success');
      setShowTagEditor(false);
      fetchArticle();
    } catch (err: any) {
      showToast(err.message || '保存标签失败', 'error');
    } finally {
      setTagSaving(false);
    }
  }, [editTags, params.id, fetchArticle, showToast]);

  const filteredTagOptions = allTags.filter(
    t => !editTags.some(et => et.id === t.id) && t.name.toLowerCase().includes(tagSearch.toLowerCase())
  );

  const handleMoveToFolder = useCallback(
    async (folderId: string | null) => {
      setActionLoading('folder');
      try {
        await api.updateArticle(params.id, { folder_id: folderId });
        setArticle((prev) => (prev ? { ...prev, folder_id: folderId || undefined } : prev));
        showToast(folderId ? '已移动到文件夹' : '已从文件夹移除', 'success');
      } catch (err: any) {
        showToast(err.message || '移动失败', 'error');
      } finally {
        setActionLoading(null);
        setFolderOpen(false);
      }
    },
    [params.id, showToast]
  );

  const handleShare = useCallback(async () => {
    if (!article) return;
    const shareUrl = article.url || window.location.href;
    try {
      await navigator.clipboard.writeText(shareUrl);
      showToast('链接已复制到剪贴板', 'success');
    } catch {
      // Fallback
      const input = document.createElement('input');
      input.value = shareUrl;
      document.body.appendChild(input);
      input.select();
      document.execCommand('copy');
      document.body.removeChild(input);
      showToast('链接已复制到剪贴板', 'success');
    }
  }, [article, showToast]);

  // ── Render: Not Found ──────────────────────────────────────────────────
  if (notFound) {
    return (
      <div className="max-w-lg mx-auto py-20 px-6 text-center animate-fade-in">
        <div className="w-20 h-20 rounded-full bg-[#ffe8e6] flex items-center justify-center mx-auto mb-6">
          <AlertCircle size={36} className="text-[#ff3b30]" />
        </div>
        <h1 className="text-2xl font-bold text-[#1d1d1f] mb-2">文章未找到</h1>
        <p className="text-[#6e6e73] mb-8">
          这篇文章可能已被删除，或链接地址不正确。
        </p>
        <div className="flex gap-3 justify-center">
          <button
            onClick={() => router.push('/')}
            className="px-6 py-2.5 rounded-xl bg-[#007aff] text-white font-medium text-sm hover:bg-[#0066d6] active:scale-[0.98] transition-all duration-200 shadow-sm"
          >
            返回首页
          </button>
          <button
            onClick={() => router.back()}
            className="px-6 py-2.5 rounded-xl bg-white text-[#1d1d1f] font-medium text-sm border border-[#e5e5ea] hover:bg-[#f5f5f7] active:scale-[0.98] transition-all duration-200"
          >
            返回上一页
          </button>
        </div>
      </div>
    );
  }

  // ── Render: Error ──────────────────────────────────────────────────────
  if (error && !loading) {
    return (
      <div className="max-w-lg mx-auto py-20 px-6 text-center animate-fade-in">
        <div className="w-20 h-20 rounded-full bg-[#fff3e0] flex items-center justify-center mx-auto mb-6">
          <AlertCircle size={36} className="text-[#ff9500]" />
        </div>
        <h1 className="text-2xl font-bold text-[#1d1d1f] mb-2">加载失败</h1>
        <p className="text-[#6e6e73] mb-2">{error}</p>
        <div className="flex gap-3 justify-center mt-8">
          <button
            onClick={fetchArticle}
            className="px-6 py-2.5 rounded-xl bg-[#007aff] text-white font-medium text-sm hover:bg-[#0066d6] active:scale-[0.98] transition-all duration-200 shadow-sm"
          >
            重试
          </button>
          <button
            onClick={() => router.push('/')}
            className="px-6 py-2.5 rounded-xl bg-white text-[#1d1d1f] font-medium text-sm border border-[#e5e5ea] hover:bg-[#f5f5f7] active:scale-[0.98] transition-all duration-200"
          >
            返回首页
          </button>
        </div>
      </div>
    );
  }

  // ── Render: Loading ────────────────────────────────────────────────────
  if (loading) {
    return <ReaderSkeleton />;
  }

  // ── Guard ──────────────────────────────────────────────────────────────
  if (!article) return null;

  // ── Derived data ───────────────────────────────────────────────────────
  const currentFolder = folders.find((f) => f.id === article.folder_id);
  const displayFolderName = article.folder?.name || currentFolder?.name;

  // ── Render: Article ────────────────────────────────────────────────────
  return (
    <>
      {/* ─── Toast ──────────────────────────────────────────────────────── */}
      {toast && (
        <div
          className={`fixed top-4 right-4 sm:top-6 sm:right-6 z-50 animate-fade-in px-4 sm:px-5 py-2.5 sm:py-3 rounded-xl shadow-lg text-sm font-medium flex items-center gap-2 sm:gap-2.5 ${
            toast.type === 'success'
              ? 'bg-[#34c759] text-white'
              : 'bg-[#ff3b30] text-white'
          }`}
        >
          {toast.type === 'success' ? <CheckCircle2 size={18} /> : <AlertCircle size={18} />}
          {toast.message}
        </div>
      )}

      {/* ─── Top Action Bar ─────────────────────────────────────────────── */}
      <div className="sticky top-0 z-30 bg-[#f5f5f7]/80 backdrop-blur-xl border-b border-[#e5e5ea]">
        <div className="max-w-3xl mx-auto px-3 sm:px-6 h-12 flex items-center justify-between">
          {/* Left: back */}
          <button
            onClick={goBack}
            className="flex items-center gap-1.5 text-sm text-[#6e6e73] hover:text-[#1d1d1f] transition-colors"
          >
            <ArrowLeft size={16} />
            <span className="hidden sm:inline">返回</span>
          </button>

          {/* Right: actions */}
          <div className="flex items-center gap-1">
            {/* Mind Map */}
            <Link
              href={`/mindmap/${params.id}`}
              className="h-8 px-2.5 flex items-center gap-1 rounded-lg hover:bg-white/70 transition-colors text-xs text-[#6e6e73]"
              title="思维导图"
            >
              <GitFork size={14} />
              <span className="hidden sm:inline">思维导图</span>
            </Link>

            {/* Favorite */}
            <button
              onClick={handleToggleFavorite}
              disabled={actionLoading === 'favorite'}
              className="h-8 w-8 flex items-center justify-center rounded-lg hover:bg-white/70 transition-colors disabled:opacity-40"
              title={favorited ? '取消收藏' : '收藏'}
            >
              <Star
                size={16}
                className={
                  favorited ? 'fill-[#ff9500] text-[#ff9500]' : 'text-[#6e6e73]'
                }
              />
            </button>

            {/* Archive */}
            <button
              onClick={handleToggleArchive}
              disabled={actionLoading === 'archive'}
              className="h-8 w-8 flex items-center justify-center rounded-lg hover:bg-white/70 transition-colors disabled:opacity-40"
              title={status === 'archived' ? '取消归档' : '归档'}
            >
              <Archive
                size={16}
                className={status === 'archived' ? 'text-[#007aff]' : 'text-[#6e6e73]'}
              />
            </button>

            {/* Mark as completed / reading */}
            <button
              onClick={handleMarkCompleted}
              disabled={actionLoading === 'complete'}
              className="h-8 px-2.5 flex items-center gap-1 rounded-lg hover:bg-white/70 transition-colors disabled:opacity-40 text-xs text-[#6e6e73]"
              title={status === 'completed' ? '标记为阅读中' : '标记为已读完'}
            >
              <CheckCircle2
                size={14}
                className={status === 'completed' ? 'text-[#34c759]' : 'text-[#6e6e73]'}
              />
              <span className="hidden sm:inline">
                {status === 'completed' ? '已读完' : '标记读完'}
              </span>
            </button>

            {/* Reprocess */}
            <button
              onClick={handleReprocess}
              disabled={actionLoading === 'reprocess'}
              className="h-8 px-2.5 flex items-center gap-1 rounded-lg hover:bg-white/70 transition-colors disabled:opacity-40 text-xs text-[#6e6e73]"
              title="AI 重新解析"
            >
              {actionLoading === 'reprocess' ? (
                <Loader2 size={14} className="animate-spin" />
              ) : (
                <RefreshCw size={14} />
              )}
              <span className="hidden sm:inline">重新解析</span>
            </button>

            {/* Share */}
            <button
              onClick={handleShare}
              className="h-8 w-8 flex items-center justify-center rounded-lg hover:bg-white/70 transition-colors"
              title="复制链接"
            >
              <Share2 size={16} className="text-[#6e6e73]" />
            </button>

            {/* Delete */}
            <button
              onClick={handleDelete}
              disabled={actionLoading === 'delete'}
              className="h-8 w-8 flex items-center justify-center rounded-lg hover:bg-[#ffe8e6] transition-colors disabled:opacity-40"
              title="删除"
            >
              {actionLoading === 'delete' ? (
                <Loader2 size={16} className="animate-spin text-[#ff3b30]" />
              ) : (
                <Trash2 size={16} className="text-[#ff3b30]" />
              )}
            </button>
          </div>
        </div>
      </div>

      {/* ─── Main Content ───────────────────────────────────────────────── */}
      <div
        ref={contentRef}
        className={`mx-auto py-6 px-4 sm:px-6 sm:py-10 animate-fade-in transition-[max-width] duration-200 ${
          editMode && article?.content_type === 'note' ? 'max-w-[1400px]' : 'max-w-3xl'
        }`}
      >
        {/* ── Header ────────────────────────────────────────────────────── */}
        <header className="mb-8">
          {/* Title — click to edit */}
          {isEditingTitle ? (
            <div className="mb-4">
              <input
                type="text"
                value={editTitleValue}
                onChange={(e) => setEditTitleValue(e.target.value)}
                onKeyDown={async (e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    if (editTitleValue.trim() && editTitleValue !== article?.title) {
                      setTitleSaving(true);
                      try {
                        await api.updateArticle(article!.id, { title: editTitleValue.trim() });
                        setArticle({ ...article!, title: editTitleValue.trim() });
                        showToast('标题已更新', 'success');
                      } catch { showToast('更新失败', 'error'); }
                      finally { setTitleSaving(false); }
                    }
                    setIsEditingTitle(false);
                  }
                  if (e.key === 'Escape') {
                    setIsEditingTitle(false);
                    setEditTitleValue('');
                  }
                }}
                onBlur={async () => {
                  if (titleSaving) return;
                  if (editTitleValue.trim() && editTitleValue !== article?.title) {
                    setTitleSaving(true);
                    try {
                      await api.updateArticle(article!.id, { title: editTitleValue.trim() });
                      setArticle({ ...article!, title: editTitleValue.trim() });
                      showToast('标题已更新', 'success');
                    } catch { showToast('更新失败', 'error'); }
                    finally { setTitleSaving(false); }
                  }
                  setIsEditingTitle(false);
                }}
                className="text-3xl sm:text-4xl font-bold text-[#1d1d1f] leading-tight tracking-tight w-full bg-transparent border-b-2 border-[#007aff] outline-none pb-1"
                autoFocus
                disabled={titleSaving}
              />
              {titleSaving && <span className="text-xs text-[#86868b] mt-1 inline-block">保存中…</span>}
            </div>
          ) : (
            <h1
              className="text-3xl sm:text-4xl font-bold text-[#1d1d1f] leading-tight tracking-tight mb-4 group cursor-pointer hover:text-[#007aff] transition-colors"
              onClick={() => { setIsEditingTitle(true); setEditTitleValue(article?.title || ''); }}
              title="点击编辑标题"
            >
              {article?.title}
              <Pencil size={18} className="inline-block ml-2 opacity-0 group-hover:opacity-100 text-[#86868b] align-baseline" />
            </h1>
          )}

          {/* Meta row */}
          <div className="flex flex-wrap items-center gap-3 text-sm text-[#6e6e73] mb-5">
            {/* Author */}
            {article.author && (
              <span className="flex items-center gap-1.5">
                <BookOpen size={14} />
                {article.author}
              </span>
            )}

            {/* Source platform */}
            {article.source_platform && (
              <span
                className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${getPlatformStyle(article.source_platform)}`}
              >
                {getPlatformLabel(article.source_platform)}
              </span>
            )}

            {/* Reading time */}
            <span className="flex items-center gap-1.5">
              <Clock size={14} />
              {formatReadingTime(article.reading_time)}
            </span>

            {/* Word count */}
            {article.word_count > 0 && (
              <span className="text-[#aeaeb2]">
                {article.word_count.toLocaleString()} 字
              </span>
            )}

            {/* Published date */}
            {article.published_at && (
              <span className="text-[#aeaeb2]">{formatDate(article.published_at)}</span>
            )}
          </div>

          {/* Status badge + folder */}
          <div className="flex flex-wrap items-center gap-2 mb-5">
            <span
              className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${STATUS_STYLES[status] || STATUS_STYLES.unread}`}
            >
              {STATUS_LABELS[status] || STATUS_LABELS.unread}
            </span>

            {/* Folder selector */}
            <div ref={folderRef} className="relative">
              <button
                onClick={() => setFolderOpen(!folderOpen)}
                disabled={actionLoading === 'folder'}
                className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-medium bg-[#f2f2f7] text-[#6e6e73] hover:bg-[#e8e8ed] transition-colors disabled:opacity-40"
              >
                <Folder size={12} />
                {displayFolderName || '未分类'}
                <ChevronDown size={10} />
              </button>

              {folderOpen && (
                <div className="absolute top-full mt-1 left-0 w-48 bg-white rounded-xl border border-[#e5e5ea] shadow-lg z-40 py-1 animate-fade-in">
                  <button
                    onClick={() => handleMoveToFolder(null)}
                    className="w-full text-left px-4 py-2 text-sm text-[#6e6e73] hover:bg-[#f5f5f7] transition-colors"
                  >
                    未分类
                  </button>
                  {folders.map((folder) => (
                    <button
                      key={folder.id}
                      onClick={() => handleMoveToFolder(folder.id)}
                      className={`w-full text-left px-4 py-2 text-sm hover:bg-[#f5f5f7] transition-colors flex items-center gap-2 ${
                        folder.id === article.folder_id
                          ? 'text-[#007aff] font-medium'
                          : 'text-[#1d1d1f]'
                      }`}
                    >
                      <span
                        className="w-2 h-2 rounded-full flex-shrink-0"
                        style={{ backgroundColor: folder.color || '#007aff' }}
                      />
                      {folder.name}
                      {folder.id === article.folder_id && (
                        <CheckCircle2 size={14} className="ml-auto" />
                      )}
                    </button>
                  ))}
                  {folders.length === 0 && (
                    <p className="px-4 py-2 text-sm text-[#aeaeb2]">暂无文件夹</p>
                  )}
                </div>
              )}
            </div>

            {/* Original link */}
            {article.url && (
              <a
                href={article.url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-medium bg-[#f2f2f7] text-[#6e6e73] hover:bg-[#e8e8ed] transition-colors"
              >
                <ExternalLink size={11} />
                原文
              </a>
            )}
          </div>

          {/* Cover image */}
          {article.cover_image && (
            <div className="mb-6 rounded-2xl overflow-hidden shadow-md">
              <img
                src={article.cover_image}
                alt={article.title}
                className="w-full h-auto object-cover max-h-96"
                onError={(e) => {
                  (e.target as HTMLImageElement).style.display = 'none';
                }}
              />
            </div>
          )}
        </header>

        {/* ── AI Summary Card ────────────────────────────────────────────── */}
        {article.summary && (
          <div
            className="mb-8 rounded-2xl overflow-hidden border border-[#c5d9f2] shadow-sm"
            style={{ backgroundColor: '#e8f2ff' }}
          >
            <button
              onClick={() => setSummaryOpen(!summaryOpen)}
              className="w-full flex items-center justify-between px-4 sm:px-6 py-3 sm:py-4 text-left hover:bg-[#dce9f8] transition-colors"
            >
              <div className="flex items-center gap-2.5">
                <div className="w-7 h-7 rounded-lg bg-[#007aff] flex items-center justify-center">
                  <ZapIcon size={14} className="text-white" />
                </div>
                <span className="font-semibold text-[#1d1d1f] text-sm">AI 摘要</span>
              </div>
              <ChevronDown
                size={18}
                className={`text-[#6e6e73] transition-transform duration-200 ${
                  summaryOpen ? 'rotate-180' : ''
                }`}
              />
            </button>
            {summaryOpen && (
              <div className="px-4 sm:px-6 pb-4 sm:pb-5 animate-fade-in">
                <p className="text-[#1d1d1f] text-[15px] leading-relaxed">
                  {article.summary}
                </p>
              </div>
            )}
          </div>
        )}

        {/* ── Key Points ─────────────────────────────────────────────────── */}
        {(() => {
          const safePoints = Array.isArray(article.key_points)
            ? article.key_points.filter((p: unknown): p is string => typeof p === 'string' && p.trim().length > 0)
            : [];
          if (safePoints.length === 0) return null;
          return (
            <div className="mb-8 bg-white rounded-2xl p-4 sm:p-6 border border-[#e5e5ea] shadow-sm">
              <h3 className="font-semibold text-[#1d1d1f] text-sm mb-4 flex items-center gap-2">
                <div className="w-6 h-6 rounded-lg bg-[#fff3e0] flex items-center justify-center">
                  <ZapIcon size={13} className="text-[#ff9500]" />
                </div>
                关键要点
              </h3>
              <ul className="space-y-3">
                {safePoints.map((point: string, i: number) => (
                  <li key={i} className="flex items-start gap-3">
                    <span className="flex-shrink-0 w-6 h-6 rounded-full bg-[#f5f5f7] flex items-center justify-center text-xs font-semibold text-[#6e6e73] mt-0.5">
                      {i + 1}
                    </span>
                    <span className="text-[15px] text-[#1d1d1f] leading-relaxed">
                      {point}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          );
        })()}

        {/* ── Tags (editable) ──────────────────────────────────────────────── */}
        <div className="mb-8">
          <div className="flex items-center gap-2 mb-2">
            <Tag size={14} className="text-[var(--text-tertiary)]" />
            <span className="text-xs font-medium text-[var(--text-secondary)] uppercase tracking-wider">标签</span>
            <button
              onClick={() => setShowTagEditor(!showTagEditor)}
              className="text-xs px-2 py-0.5 rounded-md border border-[var(--border-color)] hover:bg-[var(--bg-secondary)] text-[var(--text-tertiary)] hover:text-[var(--text-primary)] transition-colors"
            >
              {showTagEditor ? '取消编辑' : '编辑标签'}
            </button>
          </div>

          {showTagEditor ? (
            <div className="bg-[var(--bg-secondary)] rounded-xl p-4 space-y-3 border border-[var(--border-color)]">
              {/* Current tags */}
              <div className="flex flex-wrap gap-2">
                {editTags.map((tag) => (
                  <span
                    key={tag.id}
                    className="inline-flex items-center gap-1 px-3 py-1 rounded-full text-xs font-medium"
                    style={{
                      backgroundColor: tag.color ? `${tag.color}18` : 'var(--bg-tertiary)',
                      color: tag.color || 'var(--text-secondary)',
                    }}
                  >
                    {tag.name}
                    <button
                      onClick={() => handleRemoveTag(tag.id)}
                      className="ml-0.5 p-0.5 rounded-full hover:bg-black/10 transition-colors"
                    >
                      <X size={10} />
                    </button>
                  </span>
                ))}
                {editTags.length === 0 && (
                  <span className="text-xs text-[var(--text-tertiary)]">暂无标签，从下方添加</span>
                )}
              </div>

              {/* Tag search + add */}
              <div className="flex items-center gap-2">
                <div className="relative flex-1">
                  <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[var(--text-tertiary)]" />
                  <input
                    type="text"
                    value={tagSearch}
                    onChange={e => setTagSearch(e.target.value)}
                    onKeyDown={e => {
                      if (e.key === 'Enter' && filteredTagOptions.length > 0) {
                        handleAddTag(filteredTagOptions[0]);
                      }
                    }}
                    placeholder="搜索或创建标签..."
                    className="w-full pl-8 pr-3 py-2 bg-[var(--bg-primary)] rounded-lg text-sm outline-none focus:ring-2 focus:ring-[var(--accent)]/20 border border-[var(--border-color)]"
                  />
                </div>
              </div>

              {/* Tag suggestions */}
              {tagSearch && (
                <div className="space-y-1 max-h-32 overflow-y-auto">
                  {filteredTagOptions.slice(0, 8).map(tag => (
                    <button
                      key={tag.id}
                      onClick={() => handleAddTag(tag)}
                      className="w-full flex items-center gap-2 px-3 py-1.5 text-sm text-left rounded-lg hover:bg-[var(--bg-primary)] transition-colors"
                    >
                      <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: tag.color }} />
                      <span className="text-[var(--text-primary)]">{tag.name}</span>
                      {tag.is_ai_generated && <span className="text-[10px] text-[var(--text-tertiary)] ml-auto">AI</span>}
                    </button>
                  ))}
                  {tagSearch && filteredTagOptions.length === 0 && (
                    <button
                      onClick={handleCreateAndAddTag}
                      className="w-full flex items-center gap-2 px-3 py-1.5 text-sm text-left rounded-lg hover:bg-[var(--bg-primary)] text-[var(--accent)] transition-colors"
                    >
                      <Plus size={14} />
                      <span>创建标签「{tagSearch}」</span>
                    </button>
                  )}
                </div>
              )}

              {/* Save button */}
              <div className="flex justify-end gap-2 pt-2 border-t border-[var(--border-color)]">
                <button
                  onClick={() => setShowTagEditor(false)}
                  className="px-4 py-1.5 text-sm rounded-lg text-[var(--text-secondary)] hover:bg-[var(--bg-primary)] transition-colors"
                >
                  取消
                </button>
                <button
                  onClick={handleSaveTags}
                  disabled={tagSaving}
                  className="px-4 py-1.5 text-sm rounded-lg bg-[var(--accent)] text-white font-medium hover:bg-[var(--accent-hover)] disabled:opacity-50 transition-colors flex items-center gap-1"
                >
                  {tagSaving && <Loader2 size={12} className="animate-spin" />}
                  保存标签
                </button>
              </div>
            </div>
          ) : (
            <div className="flex flex-wrap items-center gap-2">
              {article.tags && article.tags.length > 0 ? article.tags.map((tag) => (
                <span
                  key={tag.id}
                  className="inline-flex items-center px-3 py-1 rounded-full text-xs font-medium transition-colors"
                  style={{
                    backgroundColor: tag.color ? `${tag.color}18` : '#f2f2f7',
                    color: tag.color || '#6e6e73',
                  }}
                >
                  {tag.name}
                </span>
              )) : (
                <span className="text-xs text-[var(--text-tertiary)]">暂无标签</span>
              )}
            </div>
          )}
        </div>

        {/* ── Note edit toolbar ──────────────────────────────────────────── */}
        {article.content_type === 'note' && (
          <div className="flex items-center justify-end gap-2 mb-3 flex-wrap">
            {editMode && (
              <button
                onClick={() => setSplitView(v => !v)}
                className={`mr-auto hidden md:flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg transition-colors ${
                  splitView
                    ? 'bg-[var(--accent)] text-white hover:bg-[var(--accent-hover)]'
                    : 'bg-[var(--bg-secondary)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]'
                }`}
                title="分屏：左编辑 / 右阅读样式预览"
              >
                {splitView ? '关闭分屏' : '开启分屏'}
              </button>
            )}
            {editMode ? (
              <>
                <button
                  onClick={handleCancelEdit}
                  disabled={savingContent}
                  className="px-3 py-1.5 text-xs rounded-lg bg-[var(--bg-secondary)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] disabled:opacity-40"
                >
                  取消
                </button>
                <button
                  onClick={handleSaveContent}
                  disabled={savingContent}
                  className="px-3 py-1.5 text-xs rounded-lg bg-[var(--accent)] text-white hover:bg-[var(--accent-hover)] disabled:opacity-40 flex items-center gap-1.5"
                >
                  {savingContent && <Loader2 size={12} className="animate-spin" />}
                  保存
                </button>
              </>
            ) : (
              <button
                onClick={() => setEditMode(true)}
                className="px-3 py-1.5 text-xs rounded-lg bg-[var(--bg-secondary)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] flex items-center gap-1.5"
              >
                <Pencil size={12} /> 编辑
              </button>
            )}
          </div>
        )}

        {/* ── Article Content ────────────────────────────────────────────── */}
        <article className="bg-[var(--bg-primary)] rounded-2xl p-4 sm:p-10 border border-[var(--border-color)] shadow-sm">
          {editMode && article.content_type === 'note' ? (
            splitView ? (
              <div className="md:grid md:grid-cols-2 md:gap-6">
                <div className="md:border-r md:border-[var(--border-color)] md:pr-6 flex flex-col">
                  <div className="text-[10px] uppercase tracking-wider text-[var(--text-tertiary)] mb-2">Markdown 源码</div>
                  <textarea
                    value={editContent}
                    onChange={(e) => setEditContent(e.target.value)}
                    placeholder="用 Markdown 语法编辑…&#10;&#10;# 一级标题&#10;## 二级标题&#10;- 列表&#10;**粗体**"
                    spellCheck={false}
                    className="flex-1 w-full min-h-[60vh] resize-none bg-[var(--bg-secondary)] rounded-lg border border-[var(--border-color)] p-4 text-[var(--text-primary)] text-[13px] leading-relaxed focus:outline-none focus:ring-2 focus:ring-[var(--accent)]/20 font-mono"
                  />
                </div>
                <div className="hidden md:flex md:flex-col md:pl-2 mt-4 md:mt-0">
                  <div className="text-[10px] uppercase tracking-wider text-[var(--text-tertiary)] mb-2">渲染编辑（带工具栏）</div>
                  <NoteEditor value={editContent} onChange={setEditContent} />
                </div>
              </div>
            ) : (
              <NoteEditor value={editContent} onChange={setEditContent} />
            )
          ) : (article.clean_content || article.raw_content) ? (
            <div className="reader-content">
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                rehypePlugins={[rehypeHighlight]}
              >
                {(article.clean_content || article.raw_content)!}
              </ReactMarkdown>
            </div>
          ) : article.content_type === 'note' ? (
            <button
              onClick={() => setEditMode(true)}
              className="w-full text-center py-16 text-[var(--text-tertiary)] hover:text-[var(--accent)] transition-colors"
            >
              这是一篇空白笔记 · 点击开始撰写
            </button>
          ) : article.fetch_status === 'pending_agent' ? (
            <div className="text-center py-16">
              <Loader2 size={40} className="mx-auto text-[var(--accent)] mb-4 animate-spin" />
              <p className="text-[var(--text-primary)] text-base mb-2">等待本地代采</p>
              <p className="text-[var(--text-tertiary)] text-sm">
                该链接走本地 agent 抓取（抖音/视频号等）。<br />
                请确认 mac 上 agent 容器在跑——抓完页面会自动刷新。
              </p>
            </div>
          ) : article.fetch_status === 'failed' ? (
            <div className="text-center py-16">
              <BookOpen size={40} className="mx-auto text-[var(--danger,#ef4444)] mb-4" />
              <p className="text-[var(--text-primary)] text-base mb-2">代采失败</p>
              <p className="text-[var(--text-tertiary)] text-sm">
                可能原因：链接已失效、内容私密/区域限制，或 agent 抓取超时。<br />
                可以删除这条后重新添加链接试试。
              </p>
            </div>
          ) : (
            <div className="text-center py-16">
              <BookOpen size={40} className="mx-auto text-[#aeaeb2] mb-4" />
              <p className="text-[#6e6e73] text-base mb-2">暂无内容</p>
              <p className="text-[#aeaeb2] text-sm">
                文章内容尚未解析，请尝试重新解析
              </p>
              <button
                onClick={handleReprocess}
                disabled={actionLoading === 'reprocess'}
                className="mt-6 inline-flex items-center gap-2 px-5 py-2.5 rounded-xl bg-[#007aff] text-white font-medium text-sm hover:bg-[#0066d6] active:scale-[0.98] transition-all duration-200 disabled:opacity-40"
              >
                {actionLoading === 'reprocess' ? (
                  <Loader2 size={16} className="animate-spin" />
                ) : (
                  <RefreshCw size={16} />
                )}
                AI 重新解析
              </button>
            </div>
          )}
        </article>

        {/* ── Related Articles ─────────────────────────────────────────────── */}
        {relatedArticles && relatedArticles.groups && relatedArticles.groups.length > 0 && (
          <section className="mt-6">
            <div className="bg-white rounded-2xl border border-[#e5e5ea] shadow-sm overflow-hidden">
              {/* Header */}
              <div className="flex items-center gap-2 px-4 sm:px-6 py-4 border-b border-[#e5e5ea] bg-[#fafafc]">
                <GitGraph size={18} className="text-[#007aff]" />
                <h2 className="text-sm font-semibold text-[#1d1d1f]">关联文章</h2>
              </div>

              {/* Groups */}
              <div className="divide-y divide-[#e5e5ea]">
                {relatedArticles.groups.map((group, gi) => {
                  const headerConfig: Record<string, { label: string; color: string; bg: string }> = {
                    related: { label: '相关文章', color: '#007aff', bg: '#e8f2ff' },
                    prerequisite: { label: '前置知识', color: '#ff9500', bg: '#fff3e0' },
                    extends: { label: '延伸阅读', color: '#34c759', bg: '#e8f8ed' },
                    contradicts: { label: '观点对立', color: '#ff3b30', bg: '#ffe8e6' },
                  };
                  const config = headerConfig[group.relation_type] || headerConfig.related;

                  return (
                    <div key={gi}>
                      <div
                        className="px-4 sm:px-6 py-2 text-xs font-medium"
                        style={{ backgroundColor: config.bg, color: config.color }}
                      >
                        {config.label}
                      </div>
                      <div className="divide-y divide-[#f2f2f7]">
                        {group.articles.map((article) => (
                          <Link
                            key={article.id}
                            href={`/read/${article.id}`}
                            className="flex items-start gap-2 sm:gap-3 px-4 sm:px-6 py-3 hover:bg-[#f5f5f7] transition-colors group"
                          >
                            <div className="flex-1 min-w-0">
                              <h3 className="text-sm font-medium text-[#1d1d1f] truncate group-hover:text-[#007aff] transition-colors">
                                {article.title}
                              </h3>
                              {article.summary && (
                                <p className="text-xs text-[#6e6e73] mt-0.5 line-clamp-1">
                                  {article.summary.length > 50
                                    ? article.summary.slice(0, 50) + '...'
                                    : article.summary}
                                </p>
                              )}
                            </div>
                            {article.relation_desc && (
                              <span className="text-[10px] text-[#aeaeb2] mt-0.5 shrink-0">
                                {article.relation_desc}
                              </span>
                            )}
                          </Link>
                        ))}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          </section>
        )}

        {/* ── Bottom spacer for sticky bar ───────────────────────────────── */}
        <div className="h-16" />
      </div>

      {/* ─── Sticky Bottom Bar ───────────────────────────────────────────── */}
      <div className="fixed bottom-0 left-0 md:left-60 right-0 z-30 bg-[#f5f5f7]/90 backdrop-blur-xl border-t border-[#e5e5ea]">
        <div className="max-w-3xl mx-auto px-3 sm:px-6 h-12 flex items-center justify-between">
          {/* Progress bar */}
          <div className="flex items-center gap-3 flex-1 mr-4">
            <div className="flex-1 h-1.5 bg-[#e8e8ed] rounded-full overflow-hidden max-w-xs">
              <div
                className="h-full bg-[#007aff] rounded-full transition-all duration-300 ease-out"
                style={{ width: `${readingProgress}%` }}
              />
            </div>
            <span className="text-xs font-medium text-[#6e6e73] tabular-nums w-10 text-right">
              {readingProgress}%
            </span>
          </div>

          {/* Bottom actions */}
          <div className="flex items-center gap-1">
            <button
              onClick={handleToggleFavorite}
              disabled={actionLoading === 'favorite'}
              className="h-8 w-8 flex items-center justify-center rounded-lg hover:bg-white/70 transition-colors disabled:opacity-40"
              title={favorited ? '取消收藏' : '收藏'}
            >
              <Star
                size={16}
                className={
                  favorited ? 'fill-[#ff9500] text-[#ff9500]' : 'text-[#6e6e73]'
                }
              />
            </button>
            <button
              onClick={handleMarkCompleted}
              disabled={actionLoading === 'complete'}
              className="h-8 w-8 flex items-center justify-center rounded-lg hover:bg-white/70 transition-colors disabled:opacity-40"
              title={status === 'completed' ? '标记为阅读中' : '标记为已读完'}
            >
              <CheckCircle2
                size={16}
                className={status === 'completed' ? 'text-[#34c759]' : 'text-[#6e6e73]'}
              />
            </button>
            <button
              onClick={handleShare}
              className="h-8 w-8 flex items-center justify-center rounded-lg hover:bg-white/70 transition-colors"
              title="复制链接"
            >
              <Share2 size={16} className="text-[#6e6e73]" />
            </button>
            <button
              onClick={handleReprocess}
              disabled={actionLoading === 'reprocess'}
              className="h-8 w-8 flex items-center justify-center rounded-lg hover:bg-white/70 transition-colors disabled:opacity-40"
              title="AI 重新解析"
            >
              {actionLoading === 'reprocess' ? (
                <Loader2 size={16} className="animate-spin" />
              ) : (
                <RefreshCw size={16} className="text-[#6e6e73]" />
              )}
            </button>
            <button
              onClick={handleDelete}
              disabled={actionLoading === 'delete'}
              className="h-8 w-8 flex items-center justify-center rounded-lg hover:bg-[#ffe8e6] transition-colors disabled:opacity-40"
              title="删除"
            >
              {actionLoading === 'delete' ? (
                <Loader2 size={16} className="animate-spin text-[#ff3b30]" />
              ) : (
                <Trash2 size={16} className="text-[#ff3b30]" />
              )}
            </button>
          </div>
        </div>
      </div>
    </>
  );
}

// ─── Custom Zap icon (inline to avoid adding lucide dep) ────────────────────

function ZapIcon({ size = 16, className = '' }: { size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
    >
      <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
    </svg>
  );
}