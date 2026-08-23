'use client';

import React, { useState, useEffect } from 'react';
import {
  Trash2, RefreshCw, CheckCircle2, AlertCircle,
  Loader2, Zap, Cpu, Download, Upload, Settings2,
  Eye, EyeOff, Activity, Server, HardDrive, Cog, Globe,
  Github, ExternalLink, GitBranch, Shield, MessageSquare, Route
} from 'lucide-react';

const API_BASE = '/api/system';

// 系统配置接口现要求超管 token（后端 require_superadmin_strict），所有请求都带上登录态
function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const token = typeof window !== 'undefined' ? localStorage.getItem('trove_token') : null;
  return { ...(extra || {}), ...(token ? { Authorization: `Bearer ${token}` } : {}) };
}

interface ConfigField {
  key: string;
  label: string;
  type: 'string' | 'text' | 'url' | 'password' | 'select' | 'number';
  required: boolean;
  placeholder?: string;
  options?: { label: string; value: string }[];
  default?: string;
}

interface ConfigGroup {
  name: string;
  fields: ConfigField[];
  _values?: Record<string, string>;
}

interface VersionInfo {
  name: string;
  version: string;
  commit?: string | null;
  repo: string;
  releases_url: string;
  update_check_enabled: boolean;
}

interface UpdateInfo {
  ok: boolean;
  disabled?: boolean;
  current: string;
  latest?: string | null;
  has_update: boolean;
  release_url?: string;
  published_at?: string;
  checked_at?: string;
  message: string;
}

export default function SettingsPage() {
  // Cache state
  const [cacheSize, setCacheSize] = useState<number | null>(null);
  const [cacheLoading, setCacheLoading] = useState(false);
  const [cacheMessage, setCacheMessage] = useState<{text:string;ok:boolean}|null>(null);

  // Config state
  const [configs, setConfigs] = useState<ConfigGroup[]>([]);
  const [editGroup, setEditGroup] = useState<string | null>(null);
  const [editValues, setEditValues] = useState<Record<string, string>>({});
  const [showPasswords, setShowPasswords] = useState<Record<string, boolean>>({});
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ok:boolean;message:string;latency_ms?:number}|null>(null);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [versionInfo, setVersionInfo] = useState<VersionInfo | null>(null);
  const [updateInfo, setUpdateInfo] = useState<UpdateInfo | null>(null);
  const [checkingUpdate, setCheckingUpdate] = useState(false);


  // Load cache stats
  useEffect(() => {
    fetch(`${API_BASE}/stats`, { headers: authHeaders() })
      .then(r => r.json())
      .then(d => setCacheSize(d.cache_size_mb))
      .catch(() => {});
  }, []);

  // Load configs
  useEffect(() => {
    fetch(`${API_BASE}/config`, { headers: authHeaders() })
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(d => {
        if (d.groups && Array.isArray(d.groups)) {
          setConfigs(d.groups);
        } else {
          setLoadError('配置格式异常');
        }
      })
      .catch(e => setLoadError(e.message));
  }, []);

  useEffect(() => {
    fetch(`${API_BASE}/version`)
      .then(r => r.json())
      .then(d => setVersionInfo(d))
      .catch(() => {});
  }, []);

  // Cache actions
  const handleClearCache = async () => {
    setCacheLoading(true);
    setCacheMessage(null);
    try {
      const r = await fetch(`${API_BASE}/cache`, { method: 'DELETE', headers: authHeaders() });
      const d = await r.json();
      setCacheMessage({ text: d.message || '缓存已清除', ok: d.success });
      if (d.success) setCacheSize(0);
    } catch {
      setCacheMessage({ text: '清除失败', ok: false });
    }
    setCacheLoading(false);
  };

  const handleRebuild = async () => {
    if (!confirm('确定要重新构建前端？这需要2-3分钟。')) return;
    setCacheLoading(true);
    setCacheMessage(null);
    try {
      const r = await fetch(`${API_BASE}/rebuild`, { method: 'POST', headers: authHeaders() });
      const d = await r.json();
      setCacheMessage({ text: d.message, ok: d.success });
    } catch {
      setCacheMessage({ text: '重建失败', ok: false });
    }
    setCacheLoading(false);
  };

  const handleUpdateCheck = async () => {
    setCheckingUpdate(true);
    setUpdateInfo(null);
    try {
      const r = await fetch(`${API_BASE}/update-check`, { headers: authHeaders() });
      const d = await r.json();
      setUpdateInfo(d);
    } catch {
      setUpdateInfo({
        ok: false,
        current: versionInfo?.version || 'unknown',
        latest: null,
        has_update: false,
        message: '网络错误，无法检查更新',
      });
    }
    setCheckingUpdate(false);
  };

  // Config actions
  const startEdit = (group: ConfigGroup) => {
    const vals: Record<string, string> = {};
    group.fields.forEach(f => {
      // For password fields, DON'T prefill the masked "abcd****wxyz" placeholder
      // — otherwise hitting "测试连接" without retyping sends the masked string
      // as the api_key and upstream returns 401. Backend now merges empty/masked
      // values with saved values automatically.
      const v = (group._values?.[f.key] as string) || '';
      vals[f.key] = f.type === 'password' ? '' : v;
    });
    setEditValues(vals);
    setEditGroup(group.name);
    setTestResult(null);
  };

  const cancelEdit = () => {
    setEditGroup(null);
    setEditValues({});
    setTestResult(null);
  };

  const updateField = (key: string, value: string) => {
    setEditValues(prev => ({ ...prev, [key]: value }));
  };

  const handleTest = async (groupName: string) => {
    setTesting(true);
    setTestResult(null);
    try {
      const r = await fetch(`${API_BASE}/config/${groupName}/test`, {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(editValues),
      });
      const d = await r.json();
      setTestResult({
        ok: d.ok,
        message: d.message || d.error,
        latency_ms: d.latency_ms,
      });
    } catch {
      setTestResult({ ok: false, message: '网络错误' });
    }
    setTesting(false);
  };

  const handleSave = async (groupName: string) => {
    setSaving(true);
    try {
      const r = await fetch(`${API_BASE}/config/${groupName}`, {
        method: 'PUT',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(editValues),
      });
      const d = await r.json();
      if (d.success) {
        setEditGroup(null);
        // Reload configs to get masked values
        const r2 = await fetch(`${API_BASE}/config`, { headers: authHeaders() });
        const d2 = await r2.json();
        if (d2.groups) setConfigs(d2.groups);
        setTestResult(null);
      } else {
        setTestResult({ ok: false, message: d.message || '保存失败：连通性测试未通过' });
      }
    } catch {
      setTestResult({ ok: false, message: '保存失败：网络错误' });
    }
    setSaving(false);
  };

  const maskValue = (val: string) => {
    if (!val || val.length <= 8) return val ? '****' : '';
    return val.slice(0, 4) + '****' + val.slice(-4);
  };

  const groupIcons: Record<string, React.ReactNode> = {
    llm: <Zap className="w-5 h-5 text-amber-500" />,
    embedding: <Cpu className="w-5 h-5 text-blue-500" />,
    search: <Globe className="w-5 h-5 text-emerald-500" />,
    proxy: <Shield className="w-5 h-5 text-cyan-600" />,
    lark: <MessageSquare className="w-5 h-5 text-blue-600" />,
    fetch_routing: <Route className="w-5 h-5 text-orange-500" />,
  };

  const groupLabels: Record<string, string> = {
    llm: 'AI 对话模型',
    embedding: '嵌入模型',
    search: '联网搜索 (Tavily)',
    proxy: '订阅代理',
    lark: '飞书 Bot',
    fetch_routing: '采集路由',
  };

  const groupDescs: Record<string, string> = {
    llm: '用于文章摘要、灵感创作、标签生成等 AI 功能',
    embedding: '用于语义搜索和知识图谱的向量化',
    search: '用于 AI 助手联网搜索、做「库内 vs 库外」对比',
    proxy: '导入 Clash/Mihomo 订阅，为指定平台提供稳定采集出口',
    lark: '配置飞书自建应用，接收文本、链接、文件、图片和语音',
    fetch_routing: '逐个平台选择服务器直连或订阅代理',
  };

  return (
    <div className="p-6 md:p-8 max-w-4xl mx-auto space-y-8">
      <div>
        <h1 className="text-2xl font-bold text-[var(--foreground)]">系统管理</h1>
        <p className="text-sm text-[var(--text-secondary)] mt-1">管理缓存、API 配置与系统状态</p>
      </div>

      {/* === OSS / VERSION SECTION === */}
      <div className="bg-[var(--bg-primary)] rounded-xl border border-[var(--border-color)] p-5">
        <div className="flex flex-col md:flex-row md:items-start md:justify-between gap-4 mb-4">
          <div className="flex items-start gap-3 min-w-0">
            <div className="w-9 h-9 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-color)] flex items-center justify-center shrink-0">
              <Github size={18} className="text-[var(--foreground)]" />
            </div>
            <div>
              <h2 className="font-semibold text-[var(--foreground)]">开源与版本</h2>
              <p className="text-sm text-[var(--text-secondary)] mt-1">
                Trove AI 是开源私有化部署工具；这里显示当前部署版本和公开仓维护入口。
              </p>
            </div>
          </div>
          <div className="flex flex-wrap md:flex-nowrap gap-2 md:shrink-0">
            <a
              href={versionInfo?.repo || 'https://github.com/weaiw/trove-ai'}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-2 text-sm px-3 py-1.5 rounded-lg border border-[var(--border-color)] hover:bg-[var(--bg-tertiary)] text-[var(--foreground)] whitespace-nowrap"
            >
              <Github size={14} />
              GitHub
              <ExternalLink size={12} className="text-[var(--text-tertiary)]" />
            </a>
            <a
              href={versionInfo?.releases_url || 'https://github.com/weaiw/trove-ai/releases'}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-2 text-sm px-3 py-1.5 rounded-lg border border-[var(--border-color)] hover:bg-[var(--bg-tertiary)] text-[var(--foreground)] whitespace-nowrap"
            >
              <GitBranch size={14} />
              Changelog
              <ExternalLink size={12} className="text-[var(--text-tertiary)]" />
            </a>
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-3 mb-4">
          <div className="rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-color)] p-3">
            <div className="text-xs text-[var(--text-tertiary)] mb-1">当前版本</div>
            <div className="font-mono text-sm text-[var(--foreground)]">
              v{versionInfo?.version || '—'}
            </div>
          </div>
          <div className="rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-color)] p-3">
            <div className="text-xs text-[var(--text-tertiary)] mb-1">Commit</div>
            <div className="font-mono text-sm text-[var(--foreground)] truncate">
              {versionInfo?.commit || '未内置'}
            </div>
          </div>
          <div className="rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-color)] p-3">
            <div className="text-xs text-[var(--text-tertiary)] mb-1">更新检查</div>
            <div className="text-sm text-[var(--foreground)]">
              {versionInfo?.update_check_enabled === false ? '已关闭' : '可用'}
            </div>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <button
            onClick={handleUpdateCheck}
            disabled={checkingUpdate}
            className="inline-flex items-center gap-2 text-sm px-3 py-1.5 rounded-lg bg-[var(--accent)] text-white hover:opacity-90 disabled:opacity-50"
          >
            {checkingUpdate ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
            检查更新
          </button>
          {updateInfo && (
            <div className={`text-xs px-3 py-2 rounded-lg border flex items-center gap-2 ${
              updateInfo.has_update
                ? 'bg-[var(--warning-light)] border-[var(--warning)] text-[var(--foreground)]'
                : updateInfo.ok
                  ? 'bg-green-50 dark:bg-green-900/20 border-green-200 dark:border-green-800 text-green-800 dark:text-green-200'
                  : 'bg-red-50 dark:bg-red-900/20 border-red-200 dark:border-red-800 text-red-800 dark:text-red-200'
            }`}>
              {updateInfo.has_update ? <AlertCircle size={14} /> : updateInfo.ok ? <CheckCircle2 size={14} /> : <AlertCircle size={14} />}
              <span>
                {updateInfo.has_update
                  ? `发现新版本 v${updateInfo.latest}`
                  : updateInfo.ok
                    ? `已是最新版本 v${updateInfo.current}`
                    : updateInfo.message}
              </span>
              {updateInfo.has_update && updateInfo.release_url && (
                <a
                  href={updateInfo.release_url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 underline"
                >
                  查看
                  <ExternalLink size={11} />
                </a>
              )}
            </div>
          )}
        </div>
      </div>

      {/* === CACHE SECTION === */}
      <div className="bg-[var(--bg-primary)] rounded-xl border border-[var(--border-color)] p-5">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <HardDrive size={18} className="text-[var(--accent)]" />
            <h2 className="font-semibold text-[var(--foreground)]">系统缓存</h2>
          </div>
          {cacheSize !== null && (
            <span className="text-xs text-[var(--text-tertiary)] bg-[var(--bg-secondary)] px-2 py-1 rounded">
              {cacheSize} MB
            </span>
          )}
        </div>
        <p className="text-sm text-[var(--text-secondary)] mb-4">管理 Next.js 构建缓存，解决页面更新不生效问题</p>

        <div className="flex flex-wrap gap-3 mb-4">
          <button
            onClick={handleClearCache}
            disabled={cacheLoading}
            className="inline-flex items-center gap-2 text-sm px-3 py-1.5 rounded-lg border border-[var(--border-color)] hover:bg-[var(--bg-tertiary)] disabled:opacity-50"
          >
            {cacheLoading ? <Loader2 size={14} className="animate-spin" /> : <Trash2 size={14} />}
            清除缓存
          </button>
          <button
            onClick={handleRebuild}
            disabled={cacheLoading}
            className="inline-flex items-center gap-2 text-sm px-3 py-1.5 rounded-lg border border-[var(--border-color)] hover:bg-[var(--bg-tertiary)] disabled:opacity-50"
          >
            <RefreshCw size={14} />
            一键重建
          </button>
        </div>
        {cacheMessage && (
          <div className={`px-3 py-2 rounded-lg border flex items-start gap-2 text-xs ${
            cacheMessage.ok
              ? 'bg-green-50 dark:bg-green-900/20 border-green-200 dark:border-green-800'
              : 'bg-red-50 dark:bg-red-900/20 border-red-200 dark:border-red-800'
          }`}>
            {cacheMessage.ok
              ? <CheckCircle2 size={14} className="text-[#34c759] mt-0.5 shrink-0" />
              : <AlertCircle size={14} className="text-red-600 mt-0.5 shrink-0" />}
            <span className={cacheMessage.ok ? 'text-green-800 dark:text-green-200' : 'text-red-800 dark:text-red-200'}>
              {cacheMessage.text}
            </span>
          </div>
        )}
      </div>

      {/* === API CONFIG SECTION === */}
      {loadError && (
        <div className="p-4 rounded-lg bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-400 text-sm flex items-center gap-2">
          <AlertCircle className="w-4 h-4" />
          加载配置失败：{loadError}
        </div>
      )}

      {configs.map(group => {
        const isEditing = editGroup === group.name;
        return (
          <div key={group.name} className="bg-[var(--bg-primary)] rounded-xl border border-[var(--border-color)] p-5">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2">
                {groupIcons[group.name] || <Cog size={18} className="text-[var(--accent)]" />}
                <h2 className="font-semibold text-[var(--foreground)]">{groupLabels[group.name] || group.name}</h2>
              </div>
              {!isEditing && (
                <button
                  onClick={() => startEdit(group)}
                  className="inline-flex items-center gap-1 text-sm px-3 py-1.5 rounded-lg border border-[var(--border-color)] hover:bg-[var(--bg-tertiary)]"
                >
                  <Settings2 size={14} />
                  编辑
                </button>
              )}
            </div>
            <p className="text-sm text-[var(--text-secondary)] mb-4">{groupDescs[group.name] || ''}</p>

            {!isEditing ? (
              /* View mode */
              <div className="border border-[var(--border-color)] rounded-lg p-4 bg-[var(--bg-secondary)] space-y-2">
                {group.fields.map(field => {
                  const val = (group._values?.[field.key] as string) || '';
                  return (
                    <div key={field.key} className="flex items-center justify-between text-sm">
                      <span className="text-[var(--text-tertiary)]">{field.label}</span>
                      <span className="text-[var(--foreground)] font-mono text-xs max-w-[300px] truncate">
                        {field.type === 'password' && val ? maskValue(val) : (val || '—')}
                      </span>
                    </div>
                  );
                })}
              </div>
            ) : (
              /* Edit mode */
              <div className="space-y-4">
                {group.fields.map(field => (
                  <div key={field.key}>
                    <label className="block text-sm font-medium text-[var(--foreground)] mb-1">
                      {field.label}
                      {field.required && <span className="text-red-500 ml-0.5">*</span>}
                    </label>
                    <div className="relative">
                      {field.type === 'select' ? (
                        <select
                          value={editValues[field.key] || ''}
                          onChange={e => updateField(field.key, e.target.value)}
                          className="w-full px-3 py-2 text-sm rounded-lg border border-[var(--border-color)] bg-[var(--bg-primary)] text-[var(--foreground)] focus:outline-none focus:ring-2 focus:ring-[var(--accent)]"
                        >
                          {field.options?.map(opt => (
                            <option key={opt.value} value={opt.value}>{opt.label}</option>
                          ))}
                        </select>
                      ) : (
                        <input
                          type={field.type === 'password' && !showPasswords[field.key] ? 'password' : 'text'}
                          value={editValues[field.key] || ''}
                          onChange={e => updateField(field.key, e.target.value)}
                          placeholder={field.type === 'password' ? '已保存，留空则保持当前值' : (field.placeholder || '')}
                          className="w-full px-3 py-2 pr-10 text-sm rounded-lg border border-[var(--border-color)] bg-[var(--bg-primary)] text-[var(--foreground)] focus:outline-none focus:ring-2 focus:ring-[var(--accent)]"
                        />
                      )}
                      {field.type === 'password' && (
                        <button
                          type="button"
                          onClick={() => setShowPasswords(p => ({ ...p, [field.key]: !p[field.key] }))}
                          className="absolute right-2 top-1/2 -translate-y-1/2 text-[var(--text-tertiary)] hover:text-[var(--foreground)]"
                        >
                          {showPasswords[field.key] ? <EyeOff size={16} /> : <Eye size={16} />}
                        </button>
                      )}
                    </div>
                  </div>
                ))}

                {/* Test result */}
                {testResult && (
                  <div className={`px-3 py-2 rounded-lg border flex items-start gap-2 text-xs ${
                    testResult.ok
                      ? 'bg-green-50 dark:bg-green-900/20 border-green-200 dark:border-green-800'
                      : 'bg-red-50 dark:bg-red-900/20 border-red-200 dark:border-red-800'
                  }`}>
                    {testResult.ok
                      ? <CheckCircle2 size={14} className="text-[#34c759] mt-0.5 shrink-0" />
                      : <AlertCircle size={14} className="text-red-600 mt-0.5 shrink-0" />}
                    <span className={testResult.ok ? 'text-green-800 dark:text-green-200' : 'text-red-800 dark:text-red-200'}>
                      {testResult.message}
                      {testResult.latency_ms ? ` (${testResult.latency_ms}ms)` : ''}
                    </span>
                  </div>
                )}

                {/* Action buttons */}
                <div className="flex items-center gap-3 pt-2">
                  <button
                    onClick={() => handleTest(group.name)}
                    disabled={testing || saving}
                    className="inline-flex items-center gap-2 text-sm px-3 py-1.5 rounded-lg border border-[var(--border-color)] hover:bg-[var(--bg-tertiary)] disabled:opacity-50"
                  >
                    {testing ? <Loader2 size={14} className="animate-spin" /> : <Activity size={14} />}
                    测试连接
                  </button>
                  <button
                    onClick={() => handleSave(group.name)}
                    disabled={testing || saving || !testResult?.ok}
                    className="inline-flex items-center gap-2 text-sm px-3 py-1.5 rounded-lg bg-[var(--accent)] text-white hover:opacity-90 disabled:opacity-30 disabled:cursor-not-allowed"
                  >
                    {saving ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />}
                    保存配置
                  </button>
                  <button
                    onClick={cancelEdit}
                    disabled={testing || saving}
                    className="text-sm px-3 py-1.5 rounded-lg text-[var(--text-tertiary)] hover:text-[var(--foreground)] disabled:opacity-50"
                  >
                    取消
                  </button>
                </div>
              </div>
            )}
          </div>
        );
      })}

      {!loadError && configs.length === 0 && (
        <div className="text-center py-8 text-[var(--text-tertiary)] text-sm">
          <Loader2 className="w-5 h-5 animate-spin mx-auto mb-2" />
          正在加载配置...
        </div>
      )}

      <div className="text-center text-xs text-[var(--text-tertiary)] py-4">
        Trove AI v{versionInfo?.version || '1.4.1'}
      </div>
    </div>
  );
}
