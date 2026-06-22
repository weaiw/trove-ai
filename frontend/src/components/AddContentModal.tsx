'use client';

import React, { useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import {
  Plus,
  X,
  Link2,
  Clipboard,
  FileUp,
  Sparkles,
  Loader2,
  CheckCircle2,
  AlertCircle,
  FileText,
  XCircle,
  Upload,
} from 'lucide-react';
import { api } from '@/lib/api';
import { extractUrl, isPureUrl } from '@/lib/url';
import type { SparkResponse } from '@/lib/types';

// ─── Helpers ──────────────────────────────────────────────────────────────

function detectPlatform(url: string): string {
  if (!url) return 'other';
  if (url.includes('toutiao.com')) return 'toutiao';
  if (url.includes('weixin.qq.com') || url.includes('mp.weixin.qq.com')) return 'wechat';
  if (url.includes('jianshu.com')) return 'jianshu';
  if (url.includes('csdn.net')) return 'csdn';
  if (url.includes('medium.com')) return 'medium';
  if (url.includes('juejin.cn')) return 'juejin';
  if (url.includes('github.com')) return 'github';
  return 'other';
}

// ─── Tab definitions ──────────────────────────────────────────────────────

const TABS = [
  { key: 'url', label: 'URL添加', icon: Link2 },
  { key: 'note', label: '写笔记', icon: FileText },
  { key: 'manual', label: '手动粘贴', icon: Clipboard },
  { key: 'file', label: '文件上传', icon: FileUp },
  { key: 'spark', label: '灵感创作', icon: Sparkles },
] as const;

type TabKey = (typeof TABS)[number]['key'];

// ─── Pipeline steps for spark ─────────────────────────────────────────────

const SPARK_STEPS = [
  { key: 'topic_expansion', label: '主题展开' },
  { key: 'outline', label: '大纲生成' },
  { key: 'chapters', label: '章节撰写' },
  { key: 'polish', label: '文章润色' },
];

// ─── Props ────────────────────────────────────────────────────────────────

interface AddContentModalProps {
  onSuccess?: () => void;
}

// ─── Component ────────────────────────────────────────────────────────────

export default function AddContentModal({ onSuccess }: AddContentModalProps) {
  const [open, setOpen] = useState(false);
  const [activeTab, setActiveTab] = useState<TabKey>('url');
  const router = useRouter();
  const [noteTitle, setNoteTitle] = useState('');
  const [noteLoading, setNoteLoading] = useState(false);

  const handleCreateNote = async () => {
    if (noteLoading) return;
    setNoteLoading(true);
    try {
      const note = await api.createNote(noteTitle.trim() || '无标题笔记', '');
      showToast('笔记已创建，进入编辑', 'success');
      setNoteTitle('');
      setOpen(false);
      router.push(`/read/${note.id}?edit=1`);
    } catch (err: any) {
      showToast(err.message || '创建笔记失败', 'error');
    } finally {
      setNoteLoading(false);
    }
  };

  // Toast
  const [toast, setToast] = useState<{ message: string; type: 'success' | 'error' } | null>(null);
  const showToast = (message: string, type: 'success' | 'error') => {
    setToast({ message, type });
    setTimeout(() => setToast(null), 3000);
  };

  // ─── Tab: URL ──────────────────────────────────────────────────────────
  const [urlInput, setUrlInput] = useState('');
  const [urlLoading, setUrlLoading] = useState(false);

  // Auto-detect URL inside share text (抖音/头条/小红书 etc.)
  const detectedUrl = !isPureUrl(urlInput) ? extractUrl(urlInput) : null;

  const handleUrlSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const url = (extractUrl(urlInput) || urlInput).trim();
    if (!url) return;
    setUrlLoading(true);
    try {
      await api.createArticle(url);
      showToast('文章添加成功！', 'success');
      setUrlInput('');
      setOpen(false);
      onSuccess?.();
    } catch (err: any) {
      showToast(err.message || '添加文章失败', 'error');
    } finally {
      setUrlLoading(false);
    }
  };

  // ─── Tab: Manual ───────────────────────────────────────────────────────
  const [manualTitle, setManualTitle] = useState('');
  const [manualUrl, setManualUrl] = useState('');
  const [manualContent, setManualContent] = useState('');
  const [manualLoading, setManualLoading] = useState(false);

  const handleManualSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const title = manualTitle.trim();
    const content = manualContent.trim();
    if (!title || !content) return;
    setManualLoading(true);
    try {
      await api.createArticleManual(
        manualUrl.trim() || `manual://${Date.now()}`,
        title,
        content,
        detectPlatform(manualUrl.trim()),
      );
      showToast('文章已保存到知识库！', 'success');
      setManualTitle('');
      setManualUrl('');
      setManualContent('');
      setOpen(false);
      onSuccess?.();
    } catch (err: any) {
      showToast(err.message || '保存文章失败', 'error');
    } finally {
      setManualLoading(false);
    }
  };

  // ─── Tab: File ─────────────────────────────────────────────────────────
  const [dragOver, setDragOver] = useState(false);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);

  const handleFileUpload = async (file: File) => {
    setUploading(true);
    setUploadProgress(0);
    try {
      const progressInterval = setInterval(() => {
        setUploadProgress((prev) => {
          if (prev >= 90) return prev;
          return prev + 10;
        });
      }, 200);

      await api.uploadFile(file);
      clearInterval(progressInterval);
      setUploadProgress(100);
      showToast(`文件 "${file.name}" 上传成功！`, 'success');
      setSelectedFile(null);
      setOpen(false);
      onSuccess?.();
    } catch (err: any) {
      showToast(err.message || '文件上传失败', 'error');
    } finally {
      setUploading(false);
      setUploadProgress(0);
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files[0];
    if (file) setSelectedFile(file);
  };

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    // Image upload size cap: 2 MB
    const isImage = /\.(png|jpe?g|webp|gif)$/i.test(file.name) || file.type.startsWith('image/');
    if (isImage && file.size > 2 * 1024 * 1024) {
      showToast(`图片不能超过 2MB（当前 ${(file.size / 1024 / 1024).toFixed(2)}MB）`, 'error');
      e.target.value = '';
      return;
    }
    setSelectedFile(file);
  };

  // ─── Tab: Spark ────────────────────────────────────────────────────────
  const [sparkInput, setSparkInput] = useState('');
  const [sparkLoading, setSparkLoading] = useState(false);
  const [sparkProgress, setSparkProgress] = useState<string[]>([]);
  const [sparkResult, setSparkResult] = useState<SparkResponse | null>(null);

  const handleSparkSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const sentence = sparkInput.trim();
    if (!sentence) return;
    setSparkLoading(true);
    setSparkProgress([]);
    setSparkResult(null);
    try {
      // Simulate progressive steps
      for (const step of SPARK_STEPS) {
        setSparkProgress((prev) => [...prev, step.key]);
        await new Promise((r) => setTimeout(r, 800 + Math.random() * 1200));
      }
      const result = await api.sparkArticle(sentence) as SparkResponse;
      setSparkResult(result);
      showToast('文章生成成功！已保存到知识库', 'success');
      onSuccess?.();
    } catch (err: any) {
      showToast(err.message || 'AI 生成失败，请重试', 'error');
    } finally {
      setSparkLoading(false);
    }
  };

  // ─── Render ────────────────────────────────────────────────────────────

  return (
    <>
      {/* ─── Toast ──────────────────────────────────────────────────────── */}
      {toast && (
        <div
          className={`fixed top-6 right-6 z-[60] animate-fade-in px-5 py-3 rounded-xl shadow-lg text-sm font-medium flex items-center gap-2.5 ${
            toast.type === 'success'
              ? 'bg-[var(--success)] text-white'
              : 'bg-[var(--danger)] text-white'
          }`}
        >
          {toast.type === 'success' ? <CheckCircle2 size={18} /> : <AlertCircle size={18} />}
          {toast.message}
        </div>
      )}

      {/* ─── FAB Button ─────────────────────────────────────────────────── */}
      <button
        onClick={() => setOpen(true)}
        className="fixed bottom-8 right-8 z-40 w-14 h-14 rounded-2xl bg-[var(--accent)] text-white shadow-lg hover:bg-[var(--accent-hover)] transition-all flex items-center justify-center active:scale-95"
      >
        <Plus size={28} />
      </button>

      {/* ─── Modal Overlay ──────────────────────────────────────────────── */}
      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center p-4"
          onClick={(e) => {
            if (e.target === e.currentTarget) setOpen(false);
          }}
        >
          {/* Backdrop */}
          <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" />

          {/* Modal */}
          <div className="relative bg-[var(--bg-primary)] rounded-2xl border border-[var(--border-color)] shadow-xl w-full max-w-2xl max-h-[90vh] flex flex-col animate-fade-in">
            {/* Header */}
            <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border-color)]">
              <h2 className="text-lg font-semibold text-[var(--text-primary)]">添加内容</h2>
              <button
                onClick={() => setOpen(false)}
                className="p-1.5 rounded-lg hover:bg-[var(--bg-secondary)] text-[var(--text-tertiary)] hover:text-[var(--text-primary)] transition-colors"
              >
                <X size={20} />
              </button>
            </div>

            {/* Tabs */}
            <div className="flex border-b border-[var(--border-color)] px-2 overflow-x-auto no-scrollbar">
              {TABS.map((tab) => {
                const Icon = tab.icon;
                const active = activeTab === tab.key;
                return (
                  <button
                    key={tab.key}
                    onClick={() => {
                      setActiveTab(tab.key);
                      setSparkResult(null);
                      setSparkProgress([]);
                    }}
                    className={`shrink-0 flex items-center gap-1.5 px-3 sm:px-4 py-3 text-sm font-medium border-b-2 transition-all ${
                      active
                        ? 'border-[var(--accent)] text-[var(--accent)]'
                        : 'border-transparent text-[var(--text-tertiary)] hover:text-[var(--text-secondary)]'
                    }`}
                  >
                    <Icon size={16} />
                    <span>{tab.label}</span>
                  </button>
                );
              })}
            </div>

            {/* Tab Content */}
            <div className="flex-1 overflow-y-auto p-6">
              {/* ── URL Tab ──────────────────────────────────────────────── */}
              {activeTab === 'url' && (
                <form onSubmit={handleUrlSubmit} className="space-y-4">
                  <div>
                    <label className="block text-sm font-medium text-[var(--text-primary)] mb-2">
                      粘贴文章链接
                    </label>
                    <input
                      type="text"
                      value={urlInput}
                      onChange={(e) => setUrlInput(e.target.value)}
                      placeholder="粘贴链接，或抖音/头条分享口令也行"
                      className="w-full h-12 px-4 rounded-xl border border-[var(--border-color)] bg-[var(--bg-secondary)] text-[var(--text-primary)] placeholder-[var(--text-tertiary)] text-sm focus:outline-none focus:ring-2 focus:ring-[var(--accent)]/30 focus:border-[var(--accent)] transition-all"
                      autoFocus
                    />
                  </div>
                  {detectedUrl && (
                    <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-[var(--accent-light)] text-xs">
                      <CheckCircle2 size={14} className="shrink-0 text-[var(--accent)]" />
                      <span className="text-[var(--text-secondary)] shrink-0">已识别链接：</span>
                      <span className="text-[var(--accent)] truncate flex-1" title={detectedUrl}>{detectedUrl}</span>
                      <button
                        type="button"
                        onClick={() => setUrlInput(detectedUrl)}
                        className="text-[var(--accent)] hover:underline shrink-0"
                      >
                        替换
                      </button>
                    </div>
                  )}
                  <p className="text-xs text-[var(--text-tertiary)]">
                    支持微信公众号、B 站(专栏/视频)、掘金、CSDN、头条、抖音、小红书、Medium 等
                  </p>
                  <button
                    type="submit"
                    disabled={urlLoading || !urlInput.trim()}
                    className="w-full h-11 rounded-xl bg-[var(--accent)] text-white font-semibold text-sm flex items-center justify-center gap-2 hover:bg-[var(--accent-hover)] active:scale-[0.98] disabled:opacity-40 disabled:cursor-not-allowed transition-all"
                  >
                    {urlLoading ? <Loader2 size={18} className="animate-spin" /> : <Plus size={18} />}
                    添加文章
                  </button>
                </form>
              )}

              {/* ── Note Tab ─────────────────────────────────────────────── */}
              {activeTab === 'note' && (
                <form
                  onSubmit={(e) => { e.preventDefault(); handleCreateNote(); }}
                  className="space-y-4"
                >
                  <div>
                    <label className="block text-sm font-medium text-[var(--text-primary)] mb-2">
                      笔记标题
                    </label>
                    <input
                      type="text"
                      value={noteTitle}
                      onChange={(e) => setNoteTitle(e.target.value)}
                      placeholder="无标题笔记"
                      className="w-full h-12 px-4 rounded-xl border border-[var(--border-color)] bg-[var(--bg-secondary)] text-[var(--text-primary)] placeholder-[var(--text-tertiary)] text-sm focus:outline-none focus:ring-2 focus:ring-[var(--accent)]/30 focus:border-[var(--accent)] transition-all"
                      autoFocus
                    />
                  </div>
                  <p className="text-xs text-[var(--text-tertiary)]">
                    创建一篇 Markdown 笔记，进入知识库（与文章共享标签、文件夹、知识图谱）
                  </p>
                  <button
                    type="submit"
                    disabled={noteLoading}
                    className="w-full h-11 rounded-xl bg-[var(--accent)] text-white font-semibold text-sm flex items-center justify-center gap-2 hover:bg-[var(--accent-hover)] active:scale-[0.98] disabled:opacity-40 transition-all"
                  >
                    {noteLoading ? <Loader2 size={18} className="animate-spin" /> : <FileText size={18} />}
                    创建并开始编辑
                  </button>
                </form>
              )}

              {/* ── Manual Tab ───────────────────────────────────────────── */}
              {activeTab === 'manual' && (
                <form onSubmit={handleManualSubmit} className="space-y-4">
                  <div>
                    <label className="block text-sm font-medium text-[var(--text-primary)] mb-2">
                      文章标题
                    </label>
                    <input
                      type="text"
                      value={manualTitle}
                      onChange={(e) => setManualTitle(e.target.value)}
                      placeholder="输入文章标题"
                      className="w-full h-11 px-4 rounded-xl border border-[var(--border-color)] bg-[var(--bg-secondary)] text-[var(--text-primary)] placeholder-[var(--text-tertiary)] text-sm focus:outline-none focus:ring-2 focus:ring-[var(--accent)]/30 transition-all"
                      required
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-[var(--text-primary)] mb-2">
                      原文链接（可选）
                    </label>
                    <input
                      type="url"
                      value={manualUrl}
                      onChange={(e) => setManualUrl(e.target.value)}
                      placeholder="https://...（用于记录来源）"
                      className="w-full h-11 px-4 rounded-xl border border-[var(--border-color)] bg-[var(--bg-secondary)] text-[var(--text-primary)] placeholder-[var(--text-tertiary)] text-sm focus:outline-none focus:ring-2 focus:ring-[var(--accent)]/30 transition-all"
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-[var(--text-primary)] mb-2">
                      文章内容
                    </label>
                    <textarea
                      value={manualContent}
                      onChange={(e) => setManualContent(e.target.value)}
                      placeholder="粘贴文章正文内容..."
                      rows={6}
                      className="w-full px-4 py-3 rounded-xl border border-[var(--border-color)] bg-[var(--bg-secondary)] text-[var(--text-primary)] placeholder-[var(--text-tertiary)] text-sm focus:outline-none focus:ring-2 focus:ring-[var(--accent)]/30 resize-y transition-all"
                      required
                    />
                  </div>
                  <button
                    type="submit"
                    disabled={manualLoading || !manualTitle.trim() || !manualContent.trim()}
                    className="w-full h-11 rounded-xl bg-[var(--accent)] text-white font-semibold text-sm flex items-center justify-center gap-2 hover:bg-[var(--accent-hover)] active:scale-[0.98] disabled:opacity-40 disabled:cursor-not-allowed transition-all"
                  >
                    {manualLoading ? <Loader2 size={18} className="animate-spin" /> : <Plus size={18} />}
                    保存到知识库
                  </button>
                </form>
              )}

              {/* ── File Tab ─────────────────────────────────────────────── */}
              {activeTab === 'file' && (
                <div className="space-y-4">
                  <div
                    onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
                    onDragLeave={() => setDragOver(false)}
                    onDrop={handleDrop}
                    className={`relative rounded-xl border-2 border-dashed p-8 text-center transition-all duration-200 ${
                      dragOver
                        ? 'border-[var(--accent)] bg-[var(--accent-light)]'
                        : selectedFile
                        ? 'border-[var(--success)] bg-[var(--success-light)]'
                        : 'border-[var(--border-color)] bg-[var(--bg-secondary)]'
                    }`}
                  >
                    {uploading ? (
                      <div className="space-y-3">
                        <Loader2 size={28} className="text-[var(--accent)] animate-spin mx-auto" />
                        <p className="text-sm font-medium text-[var(--text-primary)]">正在上传...</p>
                        <div className="w-full max-w-xs mx-auto bg-[var(--bg-tertiary)] rounded-full h-2 overflow-hidden">
                          <div
                            className="h-full bg-[var(--accent)] rounded-full transition-all duration-300"
                            style={{ width: `${uploadProgress}%` }}
                          />
                        </div>
                        <p className="text-xs text-[var(--text-tertiary)]">{uploadProgress}%</p>
                      </div>
                    ) : selectedFile ? (
                      <div className="space-y-3">
                        <FileText size={28} className="text-[var(--success)] mx-auto" />
                        <p className="text-sm font-medium text-[var(--text-primary)]">{selectedFile.name}</p>
                        <p className="text-xs text-[var(--text-secondary)]">
                          {(selectedFile.size / 1024 / 1024).toFixed(2)} MB
                        </p>
                        <div className="flex items-center justify-center gap-3">
                          <button
                            onClick={() => handleFileUpload(selectedFile)}
                            className="h-9 px-4 rounded-lg bg-[var(--accent)] text-white text-sm font-medium flex items-center gap-1.5 hover:bg-[var(--accent-hover)] transition-colors"
                          >
                            <Upload size={14} />
                            上传文件
                          </button>
                          <button
                            onClick={() => setSelectedFile(null)}
                            className="h-9 px-4 rounded-lg border border-[var(--border-color)] text-[var(--text-secondary)] text-sm font-medium flex items-center gap-1.5 hover:bg-[var(--bg-secondary)] transition-colors"
                          >
                            <XCircle size={14} />
                            取消
                          </button>
                        </div>
                      </div>
                    ) : (
                      <div className="space-y-2">
                        <FileUp size={28} className="text-[var(--text-tertiary)] mx-auto" />
                        <p className="text-sm font-medium text-[var(--text-primary)]">
                          拖拽文件到此处，或点击上传
                        </p>
                        <p className="text-xs text-[var(--text-tertiary)]">
                          支持 PDF、DOCX、XLSX、PPTX、PNG、JPG、TXT、HTML、EPUB、CSV、MD
                        </p>
                        <label className="inline-block cursor-pointer mt-2">
                          <input
                            type="file"
                            onChange={handleFileSelect}
                            accept=".pdf,.docx,.xlsx,.pptx,.png,.jpg,.jpeg,.txt,.html,.epub,.csv,.md"
                            className="hidden"
                          />
                          <span className="inline-block h-9 px-5 rounded-lg border border-[var(--border-color)] bg-[var(--bg-primary)] text-[var(--accent)] text-sm font-medium hover:bg-[var(--accent-light)] transition-colors leading-9">
                            选择文件
                          </span>
                        </label>
                      </div>
                    )}
                  </div>
                </div>
              )}

              {/* ── Spark Tab ─────────────────────────────────────────────── */}
              {activeTab === 'spark' && (
                <form onSubmit={handleSparkSubmit} className="space-y-4">
                  <div>
                    <label className="block text-sm font-medium text-[var(--text-primary)] mb-2">
                      输入一句话，AI 为你生成文章
                    </label>
                    <input
                      type="text"
                      value={sparkInput}
                      onChange={(e) => setSparkInput(e.target.value)}
                      placeholder="例如：如何构建个人知识管理系统"
                      className="w-full h-12 px-4 rounded-xl border border-[var(--border-color)] bg-[var(--bg-secondary)] text-[var(--text-primary)] placeholder-[var(--text-tertiary)] text-sm focus:outline-none focus:ring-2 focus:ring-[var(--accent)]/30 focus:border-[var(--accent)] transition-all"
                      autoFocus
                    />
                  </div>

                  {/* Progress visualization */}
                  {sparkLoading && (
                    <div className="space-y-2">
                      {SPARK_STEPS.map((step, idx) => {
                        const done = sparkProgress.includes(step.key);
                        const active = sparkProgress.length === idx;
                        return (
                          <div
                            key={step.key}
                            className={`flex items-center gap-3 px-3 py-2 rounded-lg border text-sm transition-all ${
                              done
                                ? 'border-[var(--success)] bg-[var(--success-light)] text-[var(--success)]'
                                : active
                                ? 'border-[var(--accent)] bg-[var(--accent-light)] text-[var(--accent)]'
                                : 'border-[var(--border-color)] bg-[var(--bg-secondary)] text-[var(--text-tertiary)]'
                            }`}
                          >
                            {done ? (
                              <CheckCircle2 size={16} />
                            ) : active ? (
                              <Loader2 size={16} className="animate-spin" />
                            ) : (
                              <div className="w-4 h-4 rounded-full border-2 border-[var(--text-tertiary)]" />
                            )}
                            <span className="font-medium">
                              第{idx + 1}步：{step.label}
                            </span>
                          </div>
                        );
                      })}
                    </div>
                  )}

                  {/* Result preview */}
                  {sparkResult && (
                    <div className="p-4 rounded-xl border border-[var(--success)] bg-[var(--success-light)]">
                      <p className="text-sm font-medium text-[var(--success)] mb-1">文章已生成</p>
                      <p className="text-sm font-semibold text-[var(--text-primary)]">{sparkResult.title}</p>
                      <p className="text-xs text-[var(--text-secondary)] mt-1 line-clamp-3">
                        {sparkResult.content?.substring(0, 200)}...
                      </p>
                    </div>
                  )}

                  <button
                    type="submit"
                    disabled={sparkLoading || !sparkInput.trim()}
                    className="w-full h-11 rounded-xl bg-[var(--accent)] text-white font-semibold text-sm flex items-center justify-center gap-2 hover:bg-[var(--accent-hover)] active:scale-[0.98] disabled:opacity-40 disabled:cursor-not-allowed transition-all"
                  >
                    {sparkLoading ? (
                      <Loader2 size={18} className="animate-spin" />
                    ) : (
                      <Sparkles size={18} />
                    )}
                    生成文章
                  </button>
                </form>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
