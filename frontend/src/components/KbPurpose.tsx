'use client';

import React, { useCallback, useEffect, useState } from 'react';
import { Loader2, Compass } from 'lucide-react';
import { api } from '@/lib/api';

const EXAMPLES = [
  'AI 产品经理的行业情报库，关注大模型能力、定价、出海与竞品动向',
  '个人投资研究：宏观、行业轮动、个股逻辑，偏长期价值',
  '前端工程知识库：框架源码、性能优化、工程化最佳实践',
];

/**
 * 知识库定位(kb_purpose)——借鉴 llm_wiki 的 purpose.md。
 * 用户自述这个库是干嘛的,后端会注入到 RAG 问答 / 深度研究的 system prompt,
 * 让 AI 的理解与回答贴合该用途的视角和术语。
 */
export default function KbPurpose() {
  const [value, setValue] = useState('');
  const [initial, setInitial] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState('');

  const refresh = useCallback(async () => {
    try {
      const me = await api.getMe();
      setValue(me.kb_purpose || '');
      setInitial(me.kb_purpose || '');
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const dirty = value.trim() !== initial.trim();

  const handleSave = async () => {
    setSaving(true);
    setToast('');
    try {
      const r = await api.setKbPurpose(value.trim());
      setInitial(r.kb_purpose || '');
      setValue(r.kb_purpose || '');
      setToast(r.kb_purpose ? '已保存，AI 问答会按此定位作答' : '已清除');
      setTimeout(() => setToast(''), 2500);
    } catch (e: any) {
      setToast(`保存失败: ${e.message || e}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="bg-[var(--bg-primary)] rounded-xl border border-[var(--border-color)] p-5">
      <div className="flex items-center gap-2 mb-3">
        <Compass size={18} className="text-[var(--accent)]" />
        <h2 className="font-semibold text-[var(--foreground)]">知识库定位</h2>
      </div>
      <p className="text-sm text-[var(--text-secondary)] mb-4">
        一句话说明「这个知识库是干嘛的」。AI 问答和深度研究会按这个定位的视角、优先级和术语来回答，结果更对路。留空则保持通用。
      </p>

      {loading ? (
        <div className="flex items-center gap-2 text-sm text-[var(--text-tertiary)]">
          <Loader2 size={14} className="animate-spin" /> 加载中…
        </div>
      ) : (
        <>
          <textarea
            value={value}
            onChange={(e) => setValue(e.target.value.slice(0, 2000))}
            rows={3}
            placeholder="例如：AI 产品经理的行业情报库，关注大模型能力、定价、出海与竞品动向"
            className="w-full px-3 py-2 text-sm rounded-lg border border-[var(--border-color)] bg-[var(--bg-secondary)] text-[var(--text-primary)] resize-y focus:outline-none focus:border-[var(--accent)]"
          />
          <div className="flex flex-wrap gap-1.5 mt-2">
            {EXAMPLES.map((ex, i) => (
              <button
                key={i}
                onClick={() => setValue(ex)}
                className="px-2 py-1 text-xs rounded-md bg-[var(--bg-secondary)] text-[var(--text-tertiary)] border border-[var(--border-color)] hover:text-[var(--foreground)]"
              >
                {ex.length > 18 ? ex.slice(0, 18) + '…' : ex}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-3 mt-4">
            <button
              onClick={handleSave}
              disabled={saving || !dirty}
              className="px-4 py-2 text-sm rounded-lg bg-[var(--accent)] text-white hover:bg-[var(--accent-hover)] disabled:opacity-50"
            >
              {saving ? '保存中…' : '保存'}
            </button>
            <span className="text-xs text-[var(--text-tertiary)]">{value.length}/2000</span>
            {toast && <span className="text-xs text-[var(--text-secondary)]">{toast}</span>}
          </div>
        </>
      )}
    </div>
  );
}
