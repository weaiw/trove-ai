from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text
from app.config import get_settings
import re

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_size=20,
    max_overflow=10,
)

async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

class Base(DeclarativeBase):
    pass

async def get_db() -> AsyncSession:
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

async def init_db():
    import os, glob, re
    async with engine.begin() as conn:
        # Multiple Uvicorn workers start together. Serialize schema work so they
        # do not deadlock while running the same DDL concurrently.
        await conn.exec_driver_sql("SELECT pg_advisory_xact_lock(824312657)")
        await conn.run_sync(Base.metadata.create_all)
        # Run migration SQL files from migrations directory
        migrations_dir = os.path.join(os.path.dirname(__file__), 'migrations')
        if os.path.isdir(migrations_dir):
            for sql_file in sorted(glob.glob(os.path.join(migrations_dir, '*.sql'))):
                with open(sql_file, 'r') as f:
                    sql_content = f.read()
                # Split into statements, preserving PL/pgSQL DO $$...$$ blocks
                statements = _split_sql(sql_content)
                for statement in statements:
                    statement = statement.strip()
                    if statement:
                        try:
                            # Keep one bad/obsolete statement from aborting every later
                            # migration. Driver SQL also avoids treating colons in SQL
                            # comments as SQLAlchemy bind parameters.
                            async with conn.begin_nested():
                                await conn.exec_driver_sql(statement)
                        except Exception as e:
                            print(f"⚠️  Migration warning ({os.path.basename(sql_file)}): {e}")


def _split_sql(sql: str) -> list[str]:
    """Split SQL into statements, keeping DO $$...$$ blocks intact."""
    statements = []
    # A semicolon in a line comment is not a statement boundary. Removing line
    # comments also keeps prose containing ':' from reaching the SQL driver.
    sql = re.sub(r'--.*$', '', sql, flags=re.MULTILINE)
    # Replace $$ delimited blocks with placeholders to avoid splitting inside them
    dollar_blocks = []
    
    def replace_dollar(m):
        dollar_blocks.append(m.group(0))
        return f'__DOLLAR_BLOCK_{len(dollar_blocks) - 1}__'
    
    # Match $$...$$ blocks (PL/pgSQL function bodies, DO blocks, etc.)
    sql_escaped = re.sub(r'\$\$.*?\$\$', replace_dollar, sql, flags=re.DOTALL)
    
    # Split by semicolon
    parts = sql_escaped.split(';')
    
    # Restore dollar blocks
    for part in parts:
        part = part.strip()
        if not part:
            continue
        for i, block in enumerate(dollar_blocks):
            part = part.replace(f'__DOLLAR_BLOCK_{i}__', block)
        statements.append(part)
    
    return statements
