from sqlalchemy import Column, String, Text, Integer, DateTime, Boolean, Float, ForeignKey, Table, Enum as SAEnum, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, ARRAY, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from pgvector.sqlalchemy import Vector
import uuid
import enum
from app.database import Base

# Association table for article-tags (many-to-many)
article_tags = Table(
    'article_tags',
    Base.metadata,
    Column('article_id', UUID(as_uuid=True), ForeignKey('articles.id', ondelete='CASCADE'), primary_key=True),
    Column('tag_id', UUID(as_uuid=True), ForeignKey('tags.id', ondelete='CASCADE'), primary_key=True),
)

class ArticleStatus(str, enum.Enum):
    UNREAD = "unread"
    READING = "reading"
    COMPLETED = "completed"
    ARCHIVED = "archived"

class Article(Base):
    __tablename__ = 'articles'
    __table_args__ = (
        UniqueConstraint('user_id', 'url', name='uq_articles_user_url'),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(500), nullable=False)
    url = Column(String(2048), nullable=True)  # NULL for notes
    content_type = Column(String(20), nullable=False, default='article')  # 'article' | 'note'
    source_platform = Column(String(100))  # wechat, toutiao, douyin, bilibili, xhs, etc.
    author = Column(String(255))
    published_at = Column(DateTime(timezone=True))
    
    raw_content = Column(Text)  # original HTML
    content_hash = Column(String(64), index=True)  # SHA256 of fetched raw_content; dedup same-content-different-URL
    clean_content = Column(Text)  # cleaned markdown
    plain_text = Column(Text)  # plain text for search
    embedding = Column(Vector(1024))  # SiliconFlow BAAI/bge-m3 (1024-dim) for semantic search
    
    mindmap_data = Column(JSONB)  # Cached mind map structure {name, children: [...]}
    
    summary = Column(Text)  # AI-generated summary
    key_points = Column(JSONB)  # ["point1", "point2", ...]
    
    reading_time = Column(Integer, default=0)  # estimated minutes
    word_count = Column(Integer, default=0)
    
    cover_image = Column(String(2048))
    
    status = Column(String(20), default='unread')
    fetch_status = Column(String(20), default='completed')  # completed | pending_agent | failed
    is_favorited = Column(Boolean, default=False)
    
    folder_id = Column(UUID(as_uuid=True), ForeignKey('folders.id', ondelete='SET NULL'), nullable=True)
    user_id = Column(UUID(as_uuid=True), nullable=True)  # Set by migration, then NOT NULL
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    
    # Relationships
    tags = relationship('Tag', secondary=article_tags, back_populates='articles', lazy='selectin')
    folder = relationship('Folder', back_populates='articles', lazy='selectin')

class Tag(Base):
    __tablename__ = 'tags'
    __table_args__ = (
        # 标签按用户隔离:唯一性是 (user_id, name),不再全局唯一。
        UniqueConstraint('user_id', 'name', name='uq_tags_user_name'),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    color = Column(String(7), default='#007aff')  # hex color
    is_ai_generated = Column(Boolean, default=True)
    description = Column(String(500))
    user_id = Column(UUID(as_uuid=True), nullable=True)  # Set by migration, then NOT NULL
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    articles = relationship('Article', secondary=article_tags, back_populates='tags', lazy='selectin')

class Folder(Base):
    __tablename__ = 'folders'
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    parent_id = Column(UUID(as_uuid=True), ForeignKey('folders.id', ondelete='CASCADE'), nullable=True)
    color = Column(String(7), default='#007aff')
    icon = Column(String(50), default='folder')
    user_id = Column(UUID(as_uuid=True), nullable=True)  # Set by migration, then NOT NULL
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    
    # Self-referential relationship for tree structure
    children = relationship('Folder', backref='parent', remote_side=[id], lazy='selectin')
    articles = relationship('Article', back_populates='folder', lazy='selectin')

class KnowledgeEdge(Base):
    __tablename__ = 'knowledge_edges'
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_article_id = Column(UUID(as_uuid=True), ForeignKey('articles.id', ondelete='CASCADE'), nullable=False)
    target_article_id = Column(UUID(as_uuid=True), ForeignKey('articles.id', ondelete='CASCADE'), nullable=False)
    relation_type = Column(String(50), default='related')  # related, prerequisite, extends, contradicts
    relation_desc = Column(String(500))
    weight = Column(Float, default=0.5)
    user_id = Column(UUID(as_uuid=True), nullable=True)  # Set by migration, then NOT NULL
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    source = relationship('Article', foreign_keys=[source_article_id], lazy='selectin')
    target = relationship('Article', foreign_keys=[target_article_id], lazy='selectin')

class ConceptPage(Base):
    """概念合成页:把同一概念跨多篇文章合成一页带溯源的"活百科"。
    sources(articles,不可变)→ wiki(本表,LLM 生成)。来源=语义聚合后的连贯文章集。"""
    __tablename__ = 'concept_pages'
    __table_args__ = (
        UniqueConstraint('user_id', 'name', name='uq_concept_user_name'),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    seed_type = Column(String(20), default='topic')         # 'tag' | 'topic'
    seed_tag = Column(String(100))
    content = Column(Text)
    source_article_ids = Column(JSONB, default=list)
    centroid = Column(Vector(1024))                         # 来源 embedding 质心(语义 stale 判定)
    stale = Column(Boolean, default=False)
    new_source_count = Column(Integer, default=0)
    auto_update = Column(Boolean, default=False)            # 命中新来源时后台自动重合成

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class LearningPath(Base):
    __tablename__ = 'learning_paths'
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(500), nullable=False)
    description = Column(Text)
    topic = Column(String(255))
    articles_order = Column(JSONB, default=[])  # ordered list of article IDs
    progress = Column(Float, default=0.0)  # 0-100
    status = Column(String(20), default='active')  # active, completed, paused
    user_id = Column(UUID(as_uuid=True), nullable=True)  # Set by migration, then NOT NULL
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
