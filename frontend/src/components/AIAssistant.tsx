"use client";

import React, { useState, useRef, useEffect, useCallback } from "react";
import {
  Brain, X, Send, Loader2, ChevronDown, ChevronUp, ExternalLink,
  Sparkles, Wrench, BookOpen, MessageSquare,
} from "lucide-react";
import { useRouter, usePathname } from "next/navigation";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { AskResponse, Citation } from "@/lib/types";

// ── Auto-detect heuristic (same rule as wechat_bot._is_complex_query) ──
const COMPLEX_KEYWORDS = [
  "梳理", "综述", "对比", "比较", "演化", "演变", "整理一下", "归纳",
  "哪些", "全面", "系统讲", "系统总结", "汇总", "不同观点",
  "演进", "发展脉络", "区别和联系",
];
function isComplexQuery(text: string): boolean {
  if (!text || text.length < 12) return false;
  return COMPLEX_KEYWORDS.some((kw) => text.includes(kw));
}

// ── Progress / Message types ──
interface ProgressEvent {
  stage: string;
  message: string;
  icon: string;
}

interface AssistantMessage {
  role: "user" | "assistant";
  content?: string;
  citations?: Citation[];
  progress?: ProgressEvent[];        // streaming-mode stages
  progressOpen?: boolean;            // user can collapse after final
  sparkArticleId?: string;           // /c result — link target
  mode?: "fast" | "research" | "agent" | "spark";
}

const STAGE_ICONS: Record<string, string> = {
  plan: "🧩", retrieve: "🔍", synthesize: "✍️",
  critique: "🪞", start: "🚀", thought: "💭",
  tool_call: "🔧", tool_result: "✓", final: "✅", error: "⚠️",
};

// Inline citation [[N]] → renderable JSX
function renderTextWithCites(
  text: string,
  citations: Citation[],
  onJump: (id: string) => void,
): React.ReactNode[] {
  const idxToCite = new Map(citations.map((c, i) => [i + 1, c]));
  const re = /\[\[(\d+)\]\]/g;
  const nodes: React.ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  let key = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) nodes.push(text.substring(last, m.index));
    const cite = idxToCite.get(parseInt(m[1], 10));
    if (cite) {
      nodes.push(
        <button
          key={`c-${key++}`}
          onClick={() => onJump(cite.article_id)}
          className="text-[var(--accent)] hover:underline font-medium"
        >
          《{cite.title}》
        </button>,
      );
    } else {
      nodes.push(m[0]);
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) nodes.push(text.substring(last));
  return nodes;
}

export default function AIAssistant() {
  const [isOpen, setIsOpen] = useState(false);
  const [question, setQuestion] = useState("");
  const [loading, setLoading] = useState(false);
  const [messages, setMessages] = useState<AssistantMessage[]>([]);
  const [expandedCitation, setExpandedCitation] = useState<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const router = useRouter();
  const pathname = usePathname();

  // 在文章详情页 /read/<id> 时,助手可锁定"本文问答"
  const articleId = (() => {
    const m = pathname?.match(/^\/read\/([^/?#]+)/);
    return m ? m[1] : null;
  })();
  // scope: "article" = 仅基于当前文章; "library" = 全库检索
  const [scope, setScope] = useState<"article" | "library">("library");
  // 进入/离开详情页时重置默认:详情页默认本文,其它页只能全库
  useEffect(() => {
    setScope(articleId ? "article" : "library");
  }, [articleId]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    if (isOpen) inputRef.current?.focus();
  }, [isOpen]);

  // ── Generic SSE consumer for /api/research/ask & /api/research/agent ──
  const streamResearch = useCallback(
    async (
      query: string,
      endpoint: string,
      msgIdx: number,
    ): Promise<void> => {
      const token = typeof window !== "undefined" ? localStorage.getItem("trove_token") : null;
      const res = await fetch(endpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ query }),
      });
      if (!res.ok || !res.body) {
        throw new Error(`HTTP ${res.status}`);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        while (buffer.includes("\n\n")) {
          const idx = buffer.indexOf("\n\n");
          const block = buffer.substring(0, idx);
          buffer = buffer.substring(idx + 2);
          for (const line of block.split("\n")) {
            if (!line.startsWith("data: ")) continue;
            try {
              const ev = JSON.parse(line.substring(6));
              const stage = ev.stage || "";
              const message = ev.message || "";
              const icon = STAGE_ICONS[stage] || "•";
              setMessages((prev) => {
                const next = [...prev];
                const m = next[msgIdx];
                if (!m) return prev;
                if (stage === "final") {
                  const data = ev.data || {};
                  m.content = data.answer || "(无最终答案)";
                  // critic / answer may include citations from research_agent
                  const citationsArr = data.citations;
                  if (Array.isArray(citationsArr)) {
                    m.citations = citationsArr.map((c: any, i: number) => ({
                      article_id: c.article_id || c.id || "",
                      title: c.title || "Untitled",
                      chunk: "",
                      relevance_score:
                        typeof c.distance === "number" ? Math.max(0, 1 - c.distance / 10) : 0,
                    }));
                  }
                  // also append critique (research mode) at the end of content
                  if (data.critique) {
                    m.content = `${m.content}\n\n---\n\n🪞 **自我审查**：${data.critique}`;
                  }
                } else if (stage === "error") {
                  m.content = `⚠️ ${message}`;
                } else {
                  m.progress = [...(m.progress || []), { stage, message, icon }];
                }
                return next;
              });
            } catch (e) {
              console.warn("SSE parse err", e);
            }
          }
        }
      }
    },
    [],
  );

  // ── Single-shot RAG (/api/assistant/ask) ──
  const runFastRAG = useCallback(async (q: string, msgIdx: number, articleId?: string | null) => {
    const token = typeof window !== "undefined" ? localStorage.getItem("trove_token") : null;
    const res = await fetch("/api/assistant/ask", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({
        question: q,
        top_k: 5,
        ...(articleId ? { article_id: articleId } : {}),
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data: AskResponse = await res.json();
    setMessages((prev) => {
      const next = [...prev];
      const m = next[msgIdx];
      if (m) {
        m.content = data.answer;
        m.citations = data.citations;
      }
      return next;
    });
  }, []);

  // ── Spark: one-shot full article generation (/api/articles/spark) ──
  const runSpark = useCallback(async (topic: string, msgIdx: number) => {
    const token = typeof window !== "undefined" ? localStorage.getItem("trove_token") : null;
    const res = await fetch("/api/articles/spark", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({ sentence: topic, enable_search: false }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    setMessages((prev) => {
      const next = [...prev];
      const m = next[msgIdx];
      if (m) {
        m.content = `✨ **已生成《${data.title || "Untitled"}》**\n\n${(data.content || "").slice(0, 600)}…\n\n👉 [打开完整文章](/read/${data.id})`;
        m.sparkArticleId = data.id;
      }
      return next;
    });
  }, []);

  // ── Main entry ──
  const ask = async () => {
    const raw = question.trim();
    if (!raw || loading) return;
    setQuestion("");

    // Determine mode
    let mode: "fast" | "research" | "agent" | "spark" = "fast";
    let query = raw;
    let explicitCmd = false;
    if (raw.startsWith("/c ") || raw.startsWith("/create ")) {
      mode = "spark";
      query = raw.replace(/^\/(c|create) /, "").trim();
      explicitCmd = true;
    } else if (raw.startsWith("/a ") || raw.startsWith("/agent ")) {
      mode = "agent";
      query = raw.replace(/^\/(a|agent) /, "").trim();
      explicitCmd = true;
    } else if (raw.startsWith("/r ") || raw.startsWith("/research ")) {
      mode = "research";
      query = raw.replace(/^\/(r|research) /, "").trim();
      explicitCmd = true;
    } else if (isComplexQuery(raw)) {
      mode = "research";
    }

    // 本文问答:未显式 /r /a /c 时,锁定当前文章走单点 RAG
    // (不被「梳理/对比」等复杂问题自动升级为全库深度研究)
    const useArticleScope = scope === "article" && !!articleId && !explicitCmd;
    if (useArticleScope) mode = "fast";

    if (!query) {
      setMessages((prev) => [
        ...prev,
        { role: "user", content: raw },
        { role: "assistant", content: "⚠️ 命令后没写内容，例如 `/r 梳理我对 Agent 的看法`" },
      ]);
      return;
    }

    setLoading(true);
    // Synchronously compute assistant index from current state. React 18 may run
    // the setMessages callback lazily (after `await` below), so we can't rely on
    // assigning idx inside it — otherwise downstream SSE handlers see idx = -1
    // and silently drop every event.
    const userMsg: AssistantMessage = { role: "user", content: raw };
    const assistantMsg: AssistantMessage = {
      role: "assistant",
      mode,
      progress: mode === "fast" || mode === "spark" ? undefined : [],
      progressOpen: true,
    };
    const baseLen = messages.length;
    const assistantIdx = baseLen + 1; // [..., user(baseLen), assistant(baseLen+1)]
    setMessages([...messages, userMsg, assistantMsg]);

    try {
      if (mode === "fast") {
        await runFastRAG(query, assistantIdx, useArticleScope ? articleId : null);
      } else if (mode === "research") {
        await streamResearch(query, "/api/research/ask", assistantIdx);
      } else if (mode === "agent") {
        await streamResearch(query, "/api/research/agent", assistantIdx);
      } else if (mode === "spark") {
        await runSpark(query, assistantIdx);
      }
    } catch (err: any) {
      setMessages((prev) => {
        const next = [...prev];
        const m = next[assistantIdx];
        if (m && !m.content) m.content = `⚠️ 请求失败：${err.message || err}`;
        return next;
      });
    } finally {
      setLoading(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      ask();
    }
  };

  const insertPrefix = (prefix: string) => {
    setQuestion((q) => (q.startsWith(prefix) ? q : prefix + q.replace(/^\/[a-zA-Z]+ /, "")));
    inputRef.current?.focus();
  };

  return (
    <>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="fixed bottom-32 right-8 z-40 w-14 h-14 rounded-2xl bg-[var(--accent)] text-white shadow-lg hover:bg-[var(--accent-hover)] transition-all flex items-center justify-center active:scale-95"
        title="AI 助手"
      >
        {isOpen ? <X size={28} /> : <Brain size={28} />}
      </button>

      {isOpen && (
        <div className="fixed bottom-48 right-6 z-50 w-[460px] max-w-[calc(100vw-2rem)] h-[640px] max-h-[calc(100vh-8rem)] bg-white dark:bg-gray-900 rounded-2xl shadow-2xl border border-gray-200 dark:border-gray-700 flex flex-col overflow-hidden">
          {/* Header */}
          <div className="flex items-center gap-3 px-5 py-4 border-b border-gray-100 dark:border-gray-800 shrink-0">
            <div className="w-9 h-9 rounded-xl bg-[var(--accent)] flex items-center justify-center">
              <Brain size={18} className="text-white" />
            </div>
            <div className="flex-1">
              <h3 className="font-semibold text-gray-900 dark:text-white text-sm">AI 助手</h3>
              <p className="text-xs text-gray-500 dark:text-gray-400">
                4 种模式 — 自动识别 / 深度研究 / 工具 Agent / 灵感创作
              </p>
            </div>
            <button
              onClick={() => setIsOpen(false)}
              className="w-8 h-8 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 flex items-center justify-center text-gray-400"
            >
              <X size={16} />
            </button>
          </div>

          {/* Messages */}
          <div className="flex-1 overflow-y-auto px-4 py-3 space-y-4">
            {messages.length === 0 && (
              <div className="text-center text-gray-400 dark:text-gray-500 py-8">
                <Brain size={36} className="mx-auto mb-3 opacity-40" />
                <p className="text-sm">向 AI 助手提问</p>
                <div className="mt-4 text-xs space-y-1 text-left max-w-xs mx-auto">
                  <p>💬 直接提问 → 单点 RAG（3-5s）</p>
                  <p>🔬 含「梳理/对比/演化」自动深度研究</p>
                  <p>🧠 <code>/r 问题</code> 强制 4 阶段研究</p>
                  <p>🤖 <code>/a 问题</code> 工具型 Agent（ReAct 循环）</p>
                  <p>✨ <code>/c 主题</code> 一句话生成完整文章</p>
                </div>
              </div>
            )}

            {messages.map((msg, i) => (
              <div
                key={i}
                className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}
              >
                <div
                  className={`max-w-[92%] rounded-2xl text-sm leading-relaxed ${
                    msg.role === "user"
                      ? "bg-[var(--accent)] text-white rounded-br-md px-4 py-3"
                      : "bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-gray-100 rounded-bl-md"
                  }`}
                >
                  {msg.role === "user" ? (
                    <div className="whitespace-pre-wrap">{msg.content}</div>
                  ) : (
                    <div className="px-4 py-3">
                      {/* Mode badge */}
                      {msg.mode && msg.mode !== "fast" && (
                        <div className="inline-flex items-center gap-1 px-2 py-0.5 mb-2 rounded text-[10px] bg-[var(--accent)]/15 text-[var(--accent)] font-medium">
                          {msg.mode === "research" && <><Sparkles size={10} /> 深度研究</>}
                          {msg.mode === "agent" && <><Wrench size={10} /> 工具 Agent</>}
                          {msg.mode === "spark" && <><BookOpen size={10} /> 灵感创作</>}
                        </div>
                      )}

                      {/* Progress events (research / agent modes) */}
                      {msg.progress && msg.progress.length > 0 && (
                        <div className="mb-3 space-y-1">
                          {msg.progress.map((p, pi) => (
                            <div
                              key={pi}
                              className="text-[11px] text-gray-500 dark:text-gray-400 flex items-start gap-1.5"
                            >
                              <span className="shrink-0">{p.icon}</span>
                              <span className="break-all">{p.message}</span>
                            </div>
                          ))}
                          {!msg.content && (
                            <div className="text-[11px] text-gray-400 flex items-center gap-1.5 pt-0.5">
                              <Loader2 size={10} className="animate-spin" /> 进行中…
                            </div>
                          )}
                        </div>
                      )}

                      {/* Final answer (markdown) — with [[N]] citation rendering */}
                      {msg.content && (
                        <div
                          className="prose prose-sm dark:prose-invert max-w-none
                            prose-headings:text-gray-900 dark:prose-headings:text-white
                            prose-p:text-gray-700 dark:prose-p:text-gray-200
                            prose-li:text-gray-700 dark:prose-li:text-gray-200
                            prose-strong:text-gray-900 dark:prose-strong:text-white
                            prose-code:bg-gray-200 dark:prose-code:bg-gray-700 prose-code:px-1 prose-code:py-0.5 prose-code:rounded
                            prose-a:text-[var(--accent)]
                            [&_ol]:list-decimal [&_ol]:pl-5 [&_ul]:list-disc [&_ul]:pl-5"
                        >
                          {msg.citations && msg.content.includes("[[") ? (
                            <p className="whitespace-pre-wrap">
                              {renderTextWithCites(
                                msg.content,
                                msg.citations,
                                (id) => {
                                  setIsOpen(false);
                                  router.push(`/read/${id}`);
                                },
                              )}
                            </p>
                          ) : (
                            <ReactMarkdown
                              remarkPlugins={[remarkGfm]}
                              components={{
                                a: ({ href, children }) => (
                                  <a
                                    href={href}
                                    onClick={(e) => {
                                      if (href?.startsWith("/")) {
                                        e.preventDefault();
                                        setIsOpen(false);
                                        router.push(href);
                                      }
                                    }}
                                  >
                                    {children}
                                  </a>
                                ),
                              }}
                            >
                              {msg.content}
                            </ReactMarkdown>
                          )}
                        </div>
                      )}

                      {/* Citations (fast-RAG result with chunks) */}
                      {msg.citations && msg.citations.length > 0 && msg.citations[0].chunk && (
                        <div className="mt-3 pt-3 border-t border-gray-200 dark:border-gray-700">
                          <p className="text-xs font-semibold text-gray-500 dark:text-gray-400 mb-2">
                            📚 引用来源 ({msg.citations.length})
                          </p>
                          <div className="space-y-2">
                            {msg.citations.map((cit, ci) => {
                              const key = `${i}-${ci}`;
                              return (
                                <div
                                  key={ci}
                                  className="bg-white dark:bg-gray-700/50 rounded-lg border border-gray-200 dark:border-gray-600 overflow-hidden"
                                >
                                  <button
                                    onClick={() =>
                                      setExpandedCitation(
                                        expandedCitation === key ? null : key,
                                      )
                                    }
                                    className="w-full px-3 py-2 flex items-center gap-2 text-left hover:bg-gray-50 dark:hover:bg-gray-700"
                                  >
                                    <span className="flex-shrink-0 w-5 h-5 rounded bg-blue-100 dark:bg-blue-900/50 text-blue-600 dark:text-blue-400 text-[10px] font-bold flex items-center justify-center">
                                      {ci + 1}
                                    </span>
                                    <span className="text-xs text-gray-700 dark:text-gray-300 truncate flex-1">
                                      {cit.title}
                                    </span>
                                    {expandedCitation === key ? (
                                      <ChevronUp size={14} className="text-gray-400" />
                                    ) : (
                                      <ChevronDown size={14} className="text-gray-400" />
                                    )}
                                  </button>
                                  {expandedCitation === key && (
                                    <div className="px-3 pb-3">
                                      <p className="text-xs text-gray-600 dark:text-gray-400 bg-gray-50 dark:bg-gray-800 rounded-lg p-2.5 max-h-32 overflow-y-auto whitespace-pre-wrap">
                                        {cit.chunk}
                                      </p>
                                      <button
                                        onClick={() => {
                                          setIsOpen(false);
                                          router.push(`/read/${cit.article_id}`);
                                        }}
                                        className="mt-2 text-xs text-[var(--accent)] hover:underline flex items-center gap-1"
                                      >
                                        <ExternalLink size={12} /> 查看文章
                                      </button>
                                    </div>
                                  )}
                                </div>
                              );
                            })}
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </div>
            ))}

            <div ref={messagesEndRef} />
          </div>

          {/* Mode shortcut chips */}
          <div className="px-4 pt-2 flex flex-wrap gap-1.5 border-t border-gray-100 dark:border-gray-800 shrink-0">
            <button
              onClick={() => insertPrefix("/r ")}
              className="text-[11px] px-2 py-0.5 rounded-md bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-700 flex items-center gap-1"
            >
              <Sparkles size={10} /> 深度研究
            </button>
            <button
              onClick={() => insertPrefix("/a ")}
              className="text-[11px] px-2 py-0.5 rounded-md bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-700 flex items-center gap-1"
            >
              <Wrench size={10} /> 工具 Agent
            </button>
            <button
              onClick={() => insertPrefix("/c ")}
              className="text-[11px] px-2 py-0.5 rounded-md bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-700 flex items-center gap-1"
            >
              <BookOpen size={10} /> 灵感创作
            </button>
          </div>

          {/* Input */}
          <div className="px-4 py-3 border-t border-gray-100 dark:border-gray-800 shrink-0">
            {/* Scope toggle — only on an article detail page */}
            {articleId && (
              <div className="pb-2 flex items-center gap-2">
                <span className="text-[11px] text-gray-400 dark:text-gray-500">范围</span>
                <div className="inline-flex rounded-lg bg-gray-100 dark:bg-gray-800 p-0.5">
                  <button
                    type="button"
                    onClick={() => setScope("article")}
                    className={`text-[11px] px-2.5 py-1 rounded-md transition-colors ${
                      scope === "article"
                        ? "bg-white dark:bg-gray-700 text-[var(--accent)] font-medium shadow-sm"
                        : "text-gray-500 dark:text-gray-400"
                    }`}
                  >
                    📄 本文
                  </button>
                  <button
                    type="button"
                    onClick={() => setScope("library")}
                    className={`text-[11px] px-2.5 py-1 rounded-md transition-colors ${
                      scope === "library"
                        ? "bg-white dark:bg-gray-700 text-[var(--accent)] font-medium shadow-sm"
                        : "text-gray-500 dark:text-gray-400"
                    }`}
                  >
                    📚 全库
                  </button>
                </div>
                {scope === "article" && (
                  <span className="text-[10px] text-gray-400 dark:text-gray-500">仅基于当前文章回答</span>
                )}
              </div>
            )}
            <div className="flex gap-2">
              <input
                ref={inputRef}
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="提问，或 /r /a /c 切换模式"
                disabled={loading}
                className="flex-1 px-4 py-2.5 text-sm bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl focus:outline-none focus:ring-2 focus:ring-[var(--accent)]/30 focus:border-[var(--accent)] text-gray-900 dark:text-white placeholder-gray-400 disabled:opacity-50"
              />
              <button
                onClick={ask}
                disabled={loading || !question.trim()}
                className="w-10 h-10 rounded-xl bg-[var(--accent)] text-white flex items-center justify-center hover:bg-[var(--accent-hover)] transition-all disabled:opacity-40 disabled:cursor-not-allowed shrink-0"
              >
                {loading ? (
                  <Loader2 size={18} className="animate-spin" />
                ) : (
                  <Send size={18} />
                )}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
