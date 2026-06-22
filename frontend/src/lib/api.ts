// Trove AI API Client — direct backend (bypass Next.js proxy)
import type { Article, ArticleDetail, ArticleListResponse, Tag, TagWithCount, Folder, GraphData, GraphInsights, ConceptSummary, ConceptDetail, ConceptSuggestion, ConceptAnalyze, LearningPath, LearningPathDetail, Stats, SparkResponse, RelatedArticlesResponse, AskResponse, MindMapData, MindMapResponse, User } from './types';

const API_BASE = '';

function getToken(): string | null {
  if (typeof window === 'undefined') return null;
  return localStorage.getItem('trove_token');
}

class ApiClient {
  private baseUrl: string;

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl;
  }

  private async request<T>(path: string, options?: RequestInit): Promise<T> {
    const url = `${this.baseUrl}${path}`;
    const token = getToken();

    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      ...(options?.headers as Record<string, string>),
    };

    if (token) {
      headers['Authorization'] = `Bearer ${token}`;
    }

    const res = await fetch(url, {
      ...options,
      headers,
    });

    if (!res.ok) {
      const error = await res.json().catch(() => ({ detail: res.statusText }));
      // FastAPI validation errors return detail as an array of {loc,msg,type,...}
      // — pull the msg(s) out so the UI shows the real reason instead of "[object Object]".
      let msg: string;
      if (Array.isArray(error.detail)) {
        msg = error.detail
          .map((e: any) => (typeof e === 'string' ? e : e?.msg || JSON.stringify(e)))
          .join('；');
      } else if (typeof error.detail === 'string') {
        msg = error.detail;
      } else if (error.detail && typeof error.detail === 'object') {
        msg = error.detail.msg || JSON.stringify(error.detail);
      } else {
        msg = 'Request failed';
      }
      throw new Error(msg);
    }

    if (res.status === 204) return undefined as T;
    return res.json();
  }

  // Auth
  async login(username: string, password: string): Promise<{ access_token: string; token_type: string }> {
    return this.request<{ access_token: string; token_type: string }>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    });
  }

  async getMe(): Promise<User> {
    return this.request<User>('/api/auth/me');
  }

  async setKbPurpose(kbPurpose: string): Promise<{ kb_purpose: string }> {
    return this.request<{ kb_purpose: string }>('/api/auth/kb-purpose', {
      method: 'PUT',
      body: JSON.stringify({ kb_purpose: kbPurpose }),
    });
  }

  async getInsights(username?: string): Promise<GraphInsights> {
    const q = username ? `?username=${encodeURIComponent(username)}` : '';
    return this.request<GraphInsights>(`/api/knowledge/insights${q}`);
  }

  // ── Concept pages ──
  async listConcepts(): Promise<ConceptSummary[]> {
    return this.request<ConceptSummary[]>('/api/concepts');
  }
  async conceptSuggestions(): Promise<ConceptSuggestion[]> {
    return this.request<ConceptSuggestion[]>('/api/concepts/suggestions');
  }
  async analyzeConcept(name: string, tag?: string): Promise<ConceptAnalyze> {
    return this.request<ConceptAnalyze>('/api/concepts/analyze', {
      method: 'POST',
      body: JSON.stringify({ name, tag: tag ?? null }),
    });
  }
  async createConcept(body: { name: string; seed_type: string; seed_tag?: string | null; article_ids?: string[] | null }): Promise<ConceptDetail> {
    return this.request<ConceptDetail>('/api/concepts', { method: 'POST', body: JSON.stringify(body) });
  }
  async getConcept(id: string): Promise<ConceptDetail> {
    return this.request<ConceptDetail>(`/api/concepts/${id}`);
  }
  async regenerateConcept(id: string): Promise<ConceptDetail> {
    return this.request<ConceptDetail>(`/api/concepts/${id}/regenerate`, { method: 'POST' });
  }
  async setConceptAutoUpdate(id: string, enabled: boolean): Promise<ConceptDetail> {
    return this.request<ConceptDetail>(`/api/concepts/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ auto_update: enabled }),
    });
  }
  async deleteConcept(id: string): Promise<void> {
    return this.request<void>(`/api/concepts/${id}`, { method: 'DELETE' });
  }

  async changePassword(currentPassword: string, newPassword: string): Promise<{ message: string }> {
    return this.request<{ message: string }>('/api/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ old_password: currentPassword, new_password: newPassword }),
    });
  }

  // User management (superadmin only)
  async getUsers(page: number = 1, page_size: number = 50, search?: string): Promise<{ items: User[]; total: number }> {
    const params = new URLSearchParams();
    params.append('page', String(page));
    params.append('page_size', String(page_size));
    if (search) params.append('search', search);
    return this.request<{ items: User[]; total: number }>(`/api/users?${params}`);
  }

  async createUser(username: string, password: string): Promise<User> {
    return this.request<User>('/api/users', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    });
  }

  async updateUser(id: string, data: { username?: string; password?: string; is_active?: boolean }): Promise<User> {
    return this.request<User>(`/api/users/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    });
  }

  async deleteUser(id: string): Promise<void> {
    return this.request<void>(`/api/users/${id}`, { method: 'DELETE' });
  }

  // Articles
  async createArticle(url: string, folderId?: string): Promise<Article> {
    return this.request<Article>('/api/articles', {
      method: 'POST',
      body: JSON.stringify({ url, folder_id: folderId }),
    });
  }

  async createArticleManual(url: string, title: string, content: string, sourcePlatform?: string, folderId?: string): Promise<Article> {
    return this.request<Article>('/api/articles/manual', {
      method: 'POST',
      body: JSON.stringify({ 
        url, title, content, 
        source_platform: sourcePlatform || 'other',
        folder_id: folderId 
      }),
    });
  }

  async createNote(title: string, content: string = '', folderId?: string): Promise<Article> {
    return this.request<Article>('/api/articles/notes', {
      method: 'POST',
      body: JSON.stringify({ title, content, folder_id: folderId }),
    });
  }

  async updateArticleContent(id: string, content: string, title?: string): Promise<Article> {
    const body: any = { clean_content: content };
    if (title !== undefined) body.title = title;
    return this.request<Article>(`/api/articles/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    });
  }

  async batchCreateArticles(urls: string[]): Promise<{ articles: Article[]; errors: string[] }> {
    return this.request<{ articles: Article[]; errors: string[] }>('/api/articles/batch', {
      method: 'POST',
      body: JSON.stringify({ urls }),
    });
  }

  async getArticles(params?: {
    page?: number;
    page_size?: number;
    status?: string;
    folder_id?: string;
    tag?: string;
    search?: string;
    sort?: string;
    username?: string;
  }): Promise<ArticleListResponse> {
    const searchParams = new URLSearchParams();
    if (params) {
      Object.entries(params).forEach(([k, v]) => {
        if (v !== undefined && v !== '') searchParams.append(k, String(v));
      });
    }
    const qs = searchParams.toString();
    return this.request<ArticleListResponse>(`/api/articles${qs ? '?' + qs : ''}`);
  }

  async getArticle(id: string): Promise<ArticleDetail> {
    return this.request<ArticleDetail>(`/api/articles/${id}`);
  }

  async updateArticle(id: string, data: Partial<any>): Promise<Article> {
    return this.request<Article>(`/api/articles/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    });
  }

  async deleteArticle(id: string): Promise<void> {
    return this.request<void>(`/api/articles/${id}`, { method: 'DELETE' });
  }

  async updateArticleTags(id: string, tagIds: string[]): Promise<Article> {
    return this.request<Article>(`/api/articles/${id}/tags`, {
      method: 'PATCH',
      body: JSON.stringify({ tag_ids: tagIds }),
    });
  }

  async reprocessArticle(id: string): Promise<Article> {
    return this.request<Article>(`/api/articles/${id}/reprocess`, { method: 'POST' });
  }

  async batchMoveArticles(articleIds: string[], folderId: string): Promise<{ message: string }> {
    return this.request<{ message: string }>('/api/articles/batch-move', {
      method: 'PATCH',
      body: JSON.stringify({ article_ids: articleIds, folder_id: folderId || null }),
    });
  }

  // Tags
  async getTags(): Promise<TagWithCount[]> {
    return this.request<TagWithCount[]>('/api/knowledge/tags');
  }

  async createTag(name: string, color?: string): Promise<Tag> {
    const params = new URLSearchParams({ name });
    if (color) params.append('color', color);
    return this.request<Tag>(`/api/knowledge/tags?${params}`, { method: 'POST' });
  }

  async deleteTag(id: string): Promise<void> {
    return this.request<void>(`/api/knowledge/tags/${id}`, { method: 'DELETE' });
  }

  async updateTag(id: string, data: { name?: string; color?: string; description?: string }): Promise<Tag> {
    return this.request<Tag>(`/api/knowledge/tags/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    });
  }

  async getTagStats(): Promise<TagWithCount[]> {
    return this.request<TagWithCount[]>('/api/knowledge/tags/stats');
  }

  async mergeTags(sourceTagId: string, targetTagId: string): Promise<{ message: string }> {
    return this.request<{ message: string }>('/api/knowledge/tags/merge', {
      method: 'POST',
      body: JSON.stringify({ source_tag_id: sourceTagId, target_tag_id: targetTagId }),
    });
  }

  async batchDeleteTags(tagIds: string[]): Promise<{ message: string }> {
    return this.request<{ message: string }>('/api/knowledge/tags/batch-delete', {
      method: 'POST',
      body: JSON.stringify({ tag_ids: tagIds }),
    });
  }

  // Folders
  async getFolders(parentId?: string): Promise<Folder[]> {
    const params = parentId ? `?parent_id=${parentId}` : '';
    return this.request<Folder[]>(`/api/knowledge/folders${params}`);
  }

  async createFolder(name: string, parentId?: string, color?: string): Promise<Folder> {
    const params = new URLSearchParams({ name });
    if (parentId) params.append('parent_id', parentId);
    if (color) params.append('color', color);
    return this.request<Folder>(`/api/knowledge/folders?${params}`, { method: 'POST' });
  }

  async updateFolder(id: string, updates: { name?: string; color?: string; icon?: string }): Promise<Folder> {
    const params = new URLSearchParams();
    if (updates.name !== undefined) params.set('name', updates.name);
    if (updates.color !== undefined) params.set('color', updates.color);
    if (updates.icon !== undefined) params.set('icon', updates.icon);
    return this.request<Folder>(`/api/knowledge/folders/${id}?${params}`, { method: 'PATCH' });
  }

  async deleteFolder(id: string): Promise<void> {
    return this.request<void>(`/api/knowledge/folders/${id}`, { method: 'DELETE' });
  }

  // Knowledge Graph
  async getGraph(username?: string): Promise<GraphData> {
    const params = username ? `?username=${username}` : '';
    return this.request<GraphData>(`/api/knowledge/graph${params}`);
  }

  async regenerateGraph(username?: string): Promise<{ message: string; status: string }> {
    const params = username ? `?username=${username}` : '';
    return this.request<{ message: string; status: string }>(`/api/knowledge/graph/regenerate${params}`, { method: 'POST' });
  }

  // Learning Paths
  async getPaths(status?: string, username?: string): Promise<LearningPath[]> {
    const qs = new URLSearchParams();
    if (status) qs.append('status', status);
    if (username) qs.append('username', username);
    const q = qs.toString();
    return this.request<LearningPath[]>(`/api/paths${q ? '?' + q : ''}`);
  }

  async generatePath(topic: string, description?: string): Promise<LearningPathDetail> {
    return this.request<LearningPathDetail>('/api/paths/generate', {
      method: 'POST',
      body: JSON.stringify({ topic, description }),
    });
  }

  async getPath(id: string): Promise<LearningPathDetail> {
    return this.request<LearningPathDetail>(`/api/paths/${id}`);
  }

  async updatePath(id: string, data: Partial<any>): Promise<LearningPathDetail> {
    const params = new URLSearchParams();
    Object.entries(data).forEach(([k, v]) => {
      if (v !== undefined) params.append(k, String(v));
    });
    return this.request<LearningPathDetail>(`/api/paths/${id}?${params}`, { method: 'PATCH' });
  }

  async deletePath(id: string): Promise<void> {
    return this.request<void>(`/api/paths/${id}`, { method: 'DELETE' });
  }

  // Stats
  async getStats(username?: string): Promise<Stats> {
    const params = username ? `?username=${username}` : '';
    return this.request<Stats>(`/api/knowledge/stats${params}`);
  }

  // Spark (AI article generation)
  async sparkArticle(sentence: string): Promise<SparkResponse> {
    return this.request<SparkResponse>('/api/articles/spark', {
      method: 'POST',
      body: JSON.stringify({ sentence }),
    });
  }

  // Related articles
  async getRelatedArticles(articleId: string): Promise<RelatedArticlesResponse> {
    return this.request<RelatedArticlesResponse>(`/api/articles/${articleId}/related`);
  }

  // Mind map
  async getCachedMindmap(articleId: string): Promise<MindMapResponse> {
    return this.request<MindMapResponse>(`/api/knowledge/mindmap/${articleId}`);
  }
  async generateMindMap(articleId: string): Promise<MindMapResponse> {
    return this.request<MindMapResponse>(`/api/knowledge/mindmap/${articleId}`, {
      method: 'POST',
    });
  }
  async deleteMindmapCache(articleId: string): Promise<void> {
    return this.request<void>(`/api/knowledge/mindmap/${articleId}`, {
      method: 'DELETE',
    });
  }

  // AI Assistant
  async askAssistant(question: string, topK: number = 5): Promise<AskResponse> {
    return this.request<AskResponse>('/api/assistant/ask', {
      method: 'POST',
      body: JSON.stringify({ question, top_k: topK }),
    });
  }

  // File upload
  async uploadFile(file: File): Promise<Article> {
    const formData = new FormData();
    formData.append('file', file);

    const token = getToken();
    const headers: Record<string, string> = {};
    if (token) {
      headers['Authorization'] = `Bearer ${token}`;
    }

    const url = `${this.baseUrl}/api/articles/upload`;
    const res = await fetch(url, {
      method: 'POST',
      headers,
      body: formData,
    });

    if (!res.ok) {
      const error = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(error.detail || 'Upload failed');
    }

    if (res.status === 204) return undefined as any;
    return res.json();
  }

  // ── WeChat bot binding ───────────────────────────────────────
  async wechatBindStart(): Promise<{ session: string; qr_image_content: string }> {
    return this.request('/api/wechat/bind/start', { method: 'POST' });
  }

  async wechatBindStatus(session: string): Promise<{
    status: string;
    session?: string;
    display_name?: string;
    message?: string;
  }> {
    return this.request(`/api/wechat/bind/status?session=${encodeURIComponent(session)}`);
  }

  async wechatGetAccount(): Promise<null | {
    id: string;
    account_id: string;
    wechat_user_id?: string;
    display_name?: string;
    is_active: boolean;
    last_seen_at?: string;
    created_at?: string;
  }> {
    return this.request('/api/wechat/account');
  }

  async wechatUnbind(): Promise<{ ok: boolean; message: string }> {
    return this.request('/api/wechat/account', { method: 'DELETE' });
  }

  // ── Review schedule ─────────────────────────────────────────
  async getReviewSchedule(): Promise<{
    enabled: boolean;
    frequency_days: number;
    time_of_day: string;
    next_send_at?: string;
    last_sent_at?: string;
    has_wechat_binding: boolean;
  }> {
    return this.request('/api/review/schedule');
  }

  async updateReviewSchedule(body: {
    enabled: boolean;
    frequency_days: number;
    time_of_day: string;
  }) {
    return this.request<any>('/api/review/schedule', {
      method: 'PUT',
      body: JSON.stringify(body),
    });
  }

  async previewReview(): Promise<{
    text: string | null;
    article_count: number;
    citations: { idx: number; id: string; title: string }[];
    message?: string;
  }> {
    return this.request('/api/review/preview', { method: 'POST' });
  }
}

export const api = new ApiClient(API_BASE);
