"""Authentication router — login, current user, change password."""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.dependencies import (
    get_current_user,
    hash_password,
    verify_password,
    create_access_token,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ── Schemas ─────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


class UserInfo(BaseModel):
    id: str
    username: str
    is_super_admin: bool
    is_active: bool
    created_at: str
    kb_purpose: Optional[str] = None

    class Config:
        from_attributes = True


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=6, max_length=100)


class KbPurposeRequest(BaseModel):
    kb_purpose: str = Field(default="", max_length=2000)


# ── Routes ──────────────────────────────────────────────────
@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    """Login with username and password, return JWT token."""
    from sqlalchemy import select

    result = await db.execute(
        select(User).where(User.username == body.username)
    )
    user = result.scalar_one_or_none()

    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="账号或密码错误",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账号已被停用，请联系管理员",
        )

    token = create_access_token(user.id, user.username, user.is_super_admin)

    return LoginResponse(
        access_token=token,
        user={
            "id": str(user.id),
            "username": user.username,
            "is_super_admin": user.is_super_admin,
            "is_active": user.is_active,
        },
    )


@router.get("/me", response_model=UserInfo)
async def get_me(current_user: User = Depends(get_current_user)):
    """Get current logged-in user info."""
    return UserInfo(
        id=str(current_user.id),
        username=current_user.username,
        is_super_admin=current_user.is_super_admin,
        is_active=current_user.is_active,
        created_at=str(current_user.created_at) if current_user.created_at else "",
        kb_purpose=current_user.kb_purpose,
    )


@router.put("/kb-purpose")
async def set_kb_purpose(
    body: KbPurposeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """设置当前用户的知识库定位/用途(注入到 RAG 问答与深度研究)。空串=清除。"""
    current_user.kb_purpose = body.kb_purpose.strip() or None
    await db.commit()
    return {"kb_purpose": current_user.kb_purpose or ""}


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change current user's password (requires old password)."""
    if not verify_password(body.old_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="原密码错误",
        )

    current_user.password_hash = hash_password(body.new_password)
    await db.commit()

    return {"message": "密码修改成功"}
