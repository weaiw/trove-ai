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

// ── Agent 意图：整理/连接/联网/记忆类指令 → 自动走知识管理 Agent（无需 /a 前缀）──
const AGENT_KEYWORDS = [
  "整理", "归类", "归到", "归入", "分类到", "文件夹",
  "打标签", "贴标签", "加标签", "标签",
  "建关系", "建立关系", "关联起来", "连起来", "连接", "建立联系",
  "概念页", "概念词条", "词条", "合成一篇", "合成概念",
  "复习计划", "定期复习", "排复习",
  "联网", "上网", "搜一下", "网上搜", "搜搜", "查查网", "外部最新", "库外", "对比一下网",
  "找重复", "去重", "重复的",
  "记住", "记一下", "帮我记",
  "总结", "汇总", "盘点", "帮我看看", "帮我整理",
];
function isAgentQuery(text: string): boolean {
  if (!text) return false;
  return AGENT_KEYWORDS.some((kw) => text.includes(kw));
}

// ── 灵感创作意图：写/生成「一篇/一份」→ spark（LLM 现写一篇并存库）──
// 必须是明确的"写一篇/生成一篇"句式；光句子里出现"创作"二字（如"视频创作"主题）不算。
function isSparkQuery(text: string): boolean {
  if (!text) return false;
  return /(写|生成|创作|起草|帮我出)一?[篇份]/.test(text);
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
  pendingConfirm?: { name: string; args: any }; // agent 写操作待确认 → 展示"执行"按钮（直执行用）
  confirmHandled?: boolean;          // 已点过执行，按钮隐藏
  streamText?: string;               // agent 正文流式增量（逐字显示）
}

const SUGGESTED_PROMPTS = [
  { label: "整理最近收藏", prompt: "帮我整理最近收藏，并给出可执行的归类建议" },
  { label: "库内外对比", prompt: "联网查一下 MCP 最近有什么变化，并和我库里的内容对比" },
  { label: "合成概念页", prompt: "把我库里关于 AI Agent 的文章合成一页概念页" },
  { label: "记住偏好", prompt: "请记住我是产品经理，喜欢结论先行" },
];

const STAGE_ICONS: Record<string, string> = {
  plan: "🧩", retrieve: "🔍", synthesize: "✍️",
  critique: "🪞", start: "🚀", thought: "💭",
  tool_call: "🔧", tool_result: "✓", confirm: "⏸", final: "✅", error: "⚠️",
};

const WRITE_TOOL_LABELS: Record<string, string> = {
  tag_articles: "打标签",
  move_to_folder: "归类到文件夹",
  save_url_to_folder: "链接入库并归类",
  link_articles: "建立知识关系",
  synthesize_concept: "合成概念页",
  configure_review: "配置复习简报",
};

function getConfirmSummary(pending: { name: string; args: any }) {
  const args = pending.args || {};
  const toolLabel = WRITE_TOOL_LABELS[pending.name] || pending.name;
  const details: string[] = [];
  if (Array.isArray(args.article_ids)) details.push(`影响 ${args.article_ids.length} 篇文章`);
  if (args.folder_name) details.push(`文件夹：${args.folder_name}`);
  if (args.url) details.push(`链接：${args.url}`);
  if (args.tag) details.push(`标签：${args.tag}`);
  if (args.topic) details.push(`主题：${args.topic}`);
  if (args.relation_type) details.push(`关系：${args.relation_type}`);
  if (args.frequency_days) details.push(`频率：每 ${args.frequency_days} 天`);
  if (args.time_of_day) details.push(`时间：${args.time_of_day}`);
  return { toolLabel, details };
}

function normalizeMarkdown(text: string) {
  return (text || "").replace(/\\n/g, "\n").trim();
}

function withMarkdownCitations(text: string, citations?: Citation[]) {
  if (!citations?.length) return normalizeMarkdown(text);
  const idxToCite = new Map(citations.map((c, i) => [i + 1, c]));
  return normalizeMarkdown(text).replace(/\[\[(\d+)\]\]/g, (token, rawIdx) => {
    const cite = idxToCite.get(parseInt(rawIdx, 10));
    if (!cite?.article_id) return token;
    return `[《${cite.title || "引用来源"}》](/read/${cite.article_id})`;
  });
}

function lastAssistantMode(messages: AssistantMessage[]) {
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (msg.role === "assistant" && msg.mode) return msg.mode;
  }
  return null;
}

function isFollowUpQuestion(text: string, messages: AssistantMessage[]) {
  if (!text || messages.length < 2) return false;
  const compact = text.replace(/\s+/g, "");
  const mode = lastAssistantMode(messages);
  if (mode !== "agent" && mode !== "research") return false;
  if (compact.length <= 42 && /(是什么|什么意思|怎么理解|为啥|为什么|这个|那个|上面|刚才|前面|其中|它|展开|继续|详细说)/.test(compact)) {
    return true;
  }
  return false;
}

function lastAssistantExcerpt(messages: AssistantMessage[]) {
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (msg.role === "assistant" && msg.content) {
      return normalizeMarkdown(msg.content).slice(-900);
    }
  }
  return "";
}

export default function AIAssistant() {
  const [isOpen, setIsOpen] = useState(false);
  const [question, setQuestion] = useState("");
  const [loading, setLoading] = useState(false);
  const [messages, setMessages] = useState<AssistantMessage[]>([]);
  const [expandedCitation, setExpandedCitation] = useState<string | null>(null);
  const [agentSession, setAgentSession] = useState<string | null>(null);  // 知识管理 Agent 会话(带记忆)
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const router = useRouter();
  const pathname = usePathname();

  // Detect whether we're on an article detail page (/read/<id>) → enable "本文问答"
  const articleId = (() => {
    const m = pathname?.match(/^\/read\/([^/?#]+)/);
    return m ? m[1] : null;
  })();
  // scope: "article" = 仅基于当前文章; "library" = 全库检索
  const [scope, setScope] = useState<"article" | "library">("library");
  // Entering/leaving a read page resets the default: 详情页默认本文,其它页只能全库
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
      extra?: { session_id?: string | null; confirmed?: boolean },
    ): Promise<void> => {
      const token = typeof window !== "undefined" ? localStorage.getItem("trove_token") : null;
      const res = await fetch(endpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          query,
          ...(extra?.session_id ? { session_id: extra.session_id } : {}),
          ...(extra?.confirmed ? { confirmed: true } : {}),
        }),
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
              // session 事件：记下 session_id 用于续上下文（带记忆），不渲染
              if (stage === "session") {
                const sid = ev.data?.session_id;
                if (sid) setAgentSession(sid);
                continue;
              }
              setMessages((prev) => {
                const next = [...prev];
                const m = next[msgIdx];
                if (!m) return prev;
                if (stage === "token") {
                  // 正文流式增量：逐字累加，实时显示
                  m.streamText = (m.streamText || "") + (ev.data?.delta || "");
                } else if (stage === "confirm") {
                  // 写操作待确认：记录进度 + 存下精确的 name/args 供"确认执行"直执行
                  m.progress = [...(m.progress || []), { stage, message, icon }];
                  m.pendingConfirm = { name: ev.data?.name, args: ev.data?.args || {} };
                } else if (stage === "final") {
                  const data = ev.data || {};
                  m.content = data.answer || m.streamText || "(无最终答案)";
                  m.streamText = "";   // 最终答案落定，清掉流式缓冲
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
                  // tool_call 等：进入新工具阶段，清掉上一段流式正文（多为思考片段）
                  if (stage === "tool_call") m.streamText = "";
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

  // ── Single-shot RAG (/api/assistant/ask) — pass articleId to scope to one article ──
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
  // 用户手输的"确认"类词（没点按钮而是打字时也能触发确认）
  const CONFIRM_WORDS = [
    "执行", "确认", "确认执行", "执行吧", "确定", "可以", "好的", "好", "行", "同意", "ok", "yes", "go",
  ];

  const ask = async () => {
    const raw = question.trim();
    if (!raw || loading) return;

    // 若上一条助手消息正等确认，且用户输入是"执行/确认"类 → 触发确认，而不是当成新提问
    if (CONFIRM_WORDS.includes(raw.toLowerCase())) {
      for (let k = messages.length - 1; k >= 0; k--) {
        const mm = messages[k];
        if (mm.role === "assistant" && mm.pendingConfirm && !mm.confirmHandled) {
          setQuestion("");
          runConfirm(mm.pendingConfirm, k);
          return;
        }
      }
    }

    setQuestion("");

    // Determine mode
    let mode: "fast" | "research" | "agent" | "spark" = "fast";
    let query = raw;
    let explicitCmd = false;
    const followUp = isFollowUpQuestion(raw, messages);
    if (raw.startsWith("/c ") || raw.startsWith("/create ")) {
      mode = "spark";
      explicitCmd = true;
      query = raw.replace(/^\/(c|create) /, "").trim();
    } else if (raw.startsWith("/a ") || raw.startsWith("/agent ")) {
      mode = "agent";
      explicitCmd = true;
      query = raw.replace(/^\/(a|agent) /, "").trim();
    } else if (raw.startsWith("/r ") || raw.startsWith("/research ")) {
      mode = "research";
      explicitCmd = true;
      query = raw.replace(/^\/(r|research) /, "").trim();
    } else if (isAgentQuery(raw)) {
      // 整理/连接/联网/记忆类指令 → 知识管理 Agent（优先于创作，"归类"等动作词为准）
      mode = "agent";
    } else if (isSparkQuery(raw)) {
      // 明确"写一篇/生成一篇" → 灵感创作（现写并存库）
      mode = "spark";
    } else if (isComplexQuery(raw)) {
      mode = "research";
    } else if (followUp) {
      // 短追问沿用 Agent 入口。尤其上一轮已联网/查库后，用户追问其中一个术语，
      // 不能掉回单点 RAG，否则会丢失上下文并错过 web_search。
      mode = "agent";
    }

    // 本文问答:未显式 /r /a /c 时,锁定当前文章走单点 RAG
    // (不被「梳理/对比」等复杂问题自动升级为全库研究)
    const useArticleScope = scope === "article" && !!articleId && !explicitCmd && !followUp;
    if (useArticleScope) mode = "fast";

    if (!query) {
      setMessages((prev) => [
        ...prev,
        { role: "user", content: raw },
        { role: "assistant", content: "⚠️ 命令后没写内容，例如 `/r 梳理我对 Agent 的看法`" },
      ]);
      return;
    }

    if (followUp && mode === "agent") {
      const excerpt = lastAssistantExcerpt(messages);
      if (excerpt) {
        query = `结合上文语境回答这个追问，必要时继续联网搜索或读取资料。\n\n上文摘录：\n${excerpt}\n\n用户追问：${raw}`;
      }
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
        // 知识管理 Agent：统一入口，带记忆（会话历史 + 长期画像），写操作需确认
        await streamResearch(query, "/api/agent/chat", assistantIdx, {
          session_id: agentSession,
          confirmed: false,
        });
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

  // 点"执行"：直接执行那一个已确定的写操作（不重跑 agent，避免重新搜索/空转）
  const runConfirm = async (
    pending: { name: string; args: any },
    confirmMsgIdx: number,
  ) => {
    if (loading) return;
    setLoading(true);
    setMessages((prev) => {
      const next = [...prev];
      if (next[confirmMsgIdx]) next[confirmMsgIdx].confirmHandled = true; // 隐藏按钮
      return next;
    });
    const idx = messages.length;
    const assistantMsg: AssistantMessage = { role: "assistant", mode: "agent" };
    setMessages((prev) => [...prev, assistantMsg]);
    try {
      const token = typeof window !== "undefined" ? localStorage.getItem("trove_token") : null;
      const res = await fetch("/api/agent/execute", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ name: pending.name, args: pending.args, session_id: agentSession }),
      });
      const d = await res.json();
      setMessages((prev) => {
        const next = [...prev];
        const m = next[idx];
        if (m) m.content = d.ok ? `✅ 已执行：${d.summary || "完成"}` : `⚠️ 执行失败：${d.error || d.summary || "未知错误"}`;
        return next;
      });
    } catch (err: any) {
      setMessages((prev) => {
        const next = [...prev];
        const m = next[idx];
        if (m) m.content = `⚠️ 执行失败：${err.message || err}`;
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

  const useSuggestedPrompt = (prompt: string) => {
    setQuestion(prompt);
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
              <div className="py-8">
                <Brain size={36} className="mx-auto mb-3 opacity-40" />
                <p className="text-sm text-center text-gray-500 dark:text-gray-400">今天想让知识库帮你做什么？</p>
                <div className="mt-5 grid grid-cols-1 sm:grid-cols-2 gap-2">
                  {SUGGESTED_PROMPTS.map((item) => (
                    <button
                      key={item.label}
                      onClick={() => useSuggestedPrompt(item.prompt)}
                      className="text-left rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800/70 px-3 py-2.5 hover:border-[var(--accent)] hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors"
                    >
                      <span className="flex items-center gap-1.5 text-xs font-medium text-gray-900 dark:text-gray-100">
                        <MessageSquare size={12} />
                        {item.label}
                      </span>
                      <span className="mt-1 block text-[11px] leading-relaxed text-gray-500 dark:text-gray-400">
                        {item.prompt}
                      </span>
                    </button>
                  ))}
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
                          {!msg.content && !msg.streamText && (
                            <div className="text-[11px] text-gray-400 flex items-center gap-1.5 pt-0.5">
                              <Loader2 size={10} className="animate-spin" /> 进行中…
                            </div>
                          )}
                        </div>
                      )}

                      {/* 流式正文：最终答案落定前，逐字显示 */}
                      {!msg.content && msg.streamText && (
                        <div className="prose prose-sm dark:prose-invert max-w-none prose-p:text-gray-700 dark:prose-p:text-gray-200 prose-strong:text-gray-900 dark:prose-strong:text-white">
                          <ReactMarkdown
                            remarkPlugins={[remarkGfm]}
                          >
                            {normalizeMarkdown(msg.streamText)}
                          </ReactMarkdown>
                          <span className="animate-pulse">▍</span>
                        </div>
                      )}

                      {/* 还没有任何输出时的"正在思考"指示，避免空泡干等 */}
                      {!msg.content && !msg.streamText && (!msg.progress || msg.progress.length === 0) && (
                        <div className="text-xs text-gray-400 flex items-center gap-1.5">
                          <Loader2 size={12} className="animate-spin" /> 正在思考…
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
                            {withMarkdownCitations(msg.content, msg.citations)}
                          </ReactMarkdown>
                        </div>
                      )}

                      {/* 写操作待确认 → "执行"按钮 */}
                      {msg.pendingConfirm && !msg.confirmHandled && (
                        <div className="mt-3 rounded-xl border border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/30 p-3">
                          {(() => {
                            const summary = getConfirmSummary(msg.pendingConfirm!);
                            return (
                              <>
                                <div className="flex items-start gap-2">
                                  <Wrench size={14} className="mt-0.5 text-amber-700 dark:text-amber-300 shrink-0" />
                                  <div className="min-w-0 flex-1">
                                    <div className="text-xs font-semibold text-amber-900 dark:text-amber-100">
                                      待确认：{summary.toolLabel}
                                    </div>
                                    {summary.details.length > 0 && (
                                      <div className="mt-1 flex flex-wrap gap-1">
                                        {summary.details.map((detail) => (
                                          <span
                                            key={detail}
                                            className="rounded-md bg-white/70 dark:bg-amber-900/40 px-1.5 py-0.5 text-[10px] text-amber-800 dark:text-amber-100"
                                          >
                                            {detail}
                                          </span>
                                        ))}
                                      </div>
                                    )}
                                  </div>
                                </div>
                                <div className="mt-3 flex items-center gap-2">
                                  <button
                                    onClick={() => runConfirm(msg.pendingConfirm!, i)}
                                    disabled={loading}
                                    className="px-3 py-1.5 rounded-lg bg-[var(--accent)] text-white text-xs font-medium hover:bg-[var(--accent-hover)] disabled:opacity-40 flex items-center gap-1.5"
                                  >
                                    <Wrench size={12} /> 确认执行
                                  </button>
                                  <span className="text-[11px] text-amber-700 dark:text-amber-200">确认后才会改动知识库</span>
                                </div>
                              </>
                            );
                          })()}
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

          {/* Scope toggle — only on an article detail page */}
          {articleId && (
            <div className="px-4 pt-2 flex items-center gap-2 shrink-0">
              <span className="text-[11px] text-gray-400 dark:text-gray-500">范围</span>
              <div className="inline-flex rounded-lg bg-gray-100 dark:bg-gray-800 p-0.5">
                <button
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
