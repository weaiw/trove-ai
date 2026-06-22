'use client';

import React from 'react';
import WechatBinding from '@/components/WechatBinding';
import ReviewSettings from '@/components/ReviewSettings';
import ObsidianBackup from '@/components/ObsidianBackup';
import KbPurpose from '@/components/KbPurpose';

export default function MyPage() {
  return (
    <div className="p-6 md:p-8 max-w-4xl mx-auto space-y-8">
      <div>
        <h1 className="text-2xl font-bold text-[var(--foreground)]">个人设置</h1>
        <p className="text-sm text-[var(--muted-foreground)] mt-1">绑定外部服务、管理你的账号偏好</p>
      </div>

      <KbPurpose />
      <WechatBinding />
      <ReviewSettings />
      <ObsidianBackup />
    </div>
  );
}
