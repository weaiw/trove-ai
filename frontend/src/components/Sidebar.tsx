'use client';

import React, { useState, useEffect } from 'react';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import {
  LayoutDashboard, BookOpen, GitGraph, Route,
  Search, Settings, X, Sun, Moon, Menu,
  Users, LogOut, Library
} from 'lucide-react';
import clsx from 'clsx';
import { useTheme } from '@/components/ThemeProvider';
import { useAuth } from '@/contexts/AuthContext';

const commonNavItems = [
  { href: '/', label: '仪表盘', icon: LayoutDashboard },
  { href: '/library', label: '知识库', icon: BookOpen },
  { href: '/graph', label: '知识图谱', icon: GitGraph },
  { href: '/concepts', label: '概念词条', icon: Library },
  { href: '/paths', label: '学习路线', icon: Route },
  { href: '/my', label: '个人设置', icon: Settings },
];

const adminNavItems = [
  { href: '/settings', label: '系统管理', icon: Settings },
  { href: '/users', label: '用户管理', icon: Users },
];

export default function Sidebar() {
  const pathname = usePathname();
  const router = useRouter();
  const [collapsed, setCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const { theme, toggleTheme } = useTheme();
  const { isSuperAdmin, logout } = useAuth();

  const handleLogout = () => {
    logout();
    router.push('/login');
  };

  // Build nav items based on role
  const navItems = isSuperAdmin
    ? [...commonNavItems, ...adminNavItems]
    : commonNavItems;

  // Close mobile sidebar on route change
  useEffect(() => {
    setMobileOpen(false);
  }, [pathname]);

  // Close mobile sidebar on outside click
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (mobileOpen) {
        const target = e.target as HTMLElement;
        if (!target.closest('#mobile-sidebar') && !target.closest('#mobile-hamburger')) {
          setMobileOpen(false);
        }
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [mobileOpen]);

  const sidebarContent = (
    <>
      {/* Logo */}
      <div className="h-16 flex items-center px-4 border-b border-[var(--border-color)]">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-[#007aff] to-[#5856d6] flex items-center justify-center flex-shrink-0">
            <BookOpen size={18} color="white" />
          </div>
          {!collapsed && (
            <span className="font-bold text-lg text-[var(--text-primary)] whitespace-nowrap">Trove AI</span>
          )}
        </div>
        {/* Close button - mobile only */}
        <button
          onClick={() => setMobileOpen(false)}
          className="md:hidden ml-auto p-2 rounded-lg hover:bg-[var(--bg-tertiary)] text-[var(--text-tertiary)]"
        >
          <X size={18} />
        </button>
      </div>

      {/* Navigation */}
      <nav className="flex-1 py-4 px-2 space-y-1 overflow-y-auto">
        {navItems.map((item) => {
          const Icon = item.icon;
          const active = pathname === item.href;
          return (
            <Link
              key={item.href}
              href={item.href}
              className={clsx(
                'flex items-center gap-3 px-3 py-2.5 rounded-lg transition-all duration-200 group',
                active
                  ? 'bg-[var(--accent-light)] text-[var(--accent)] font-medium'
                  : 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)]'
              )}
            >
              <Icon size={20} />
              {!collapsed && <span className="text-sm">{item.label}</span>}
            </Link>
          );
        })}
      </nav>

      {/* Bottom section */}
      <div className="border-t border-[var(--border-color)]">
        {/* Dark mode toggle */}
        <button
          onClick={toggleTheme}
          className="w-full flex items-center gap-3 px-4 py-2.5 hover:bg-[var(--bg-tertiary)] text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors"
          title={theme === 'dark' ? '切换到亮色模式' : '切换到暗色模式'}
        >
          {theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}
          {!collapsed && <span className="text-sm">{theme === 'dark' ? '亮色模式' : '暗色模式'}</span>}
        </button>

        {/* Logout button */}
        <button
          onClick={handleLogout}
          className="w-full flex items-center gap-3 px-4 py-2.5 hover:bg-[var(--bg-tertiary)] text-[var(--text-secondary)] hover:text-red-500 transition-colors"
          title="退出登录"
        >
          <LogOut size={18} />
          {!collapsed && <span className="text-sm">退出登录</span>}
        </button>

        {/* Collapse toggle - desktop only */}
        <button
          onClick={() => setCollapsed(!collapsed)}
          className="hidden md:flex w-full items-center justify-center p-2 rounded-lg hover:bg-[var(--bg-tertiary)] text-[var(--text-tertiary)] transition-colors"
        >
          {collapsed ? '→' : '←'}
        </button>
      </div>
    </>
  );

  return (
    <>
      {/* Mobile Hamburger Bar */}
      <div className="md:hidden fixed top-0 left-0 right-0 z-40 h-14 bg-[var(--bg-primary)] border-b border-[var(--border-color)] flex items-center px-4">
        <button
          id="mobile-hamburger"
          onClick={() => setMobileOpen(!mobileOpen)}
          className="p-2 -ml-2 rounded-lg hover:bg-[var(--bg-tertiary)] text-[var(--text-primary)]"
          aria-label="Toggle menu"
        >
          <Menu size={22} />
        </button>
        <div className="flex items-center gap-2 ml-2">
          <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-[#007aff] to-[#5856d6] flex items-center justify-center">
            <BookOpen size={14} color="white" />
          </div>
          <span className="font-bold text-base text-[var(--text-primary)]">Trove AI</span>
        </div>
        <div className="flex-1" />
        <button
          onClick={toggleTheme}
          className="p-2 rounded-lg hover:bg-[var(--bg-tertiary)] text-[var(--text-secondary)]"
          aria-label="Toggle theme"
        >
          {theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}
        </button>
      </div>

      {/* Desktop Sidebar */}
      <aside
        className={clsx(
          'hidden md:flex fixed left-0 top-0 h-full bg-[var(--bg-primary)] border-r border-[var(--border-color)] flex-col transition-all duration-300 z-50',
          collapsed ? 'w-16' : 'w-60'
        )}
      >
        {sidebarContent}
      </aside>

      {/* Mobile Overlay */}
      {mobileOpen && (
        <div
          className="md:hidden fixed inset-0 bg-black/50 z-40 transition-opacity"
          onClick={() => setMobileOpen(false)}
        />
      )}

      {/* Mobile Sidebar (overlay) */}
      <aside
        id="mobile-sidebar"
        className={clsx(
          'md:hidden fixed left-0 top-0 h-full w-60 bg-[var(--bg-primary)] border-r border-[var(--border-color)] flex-col z-50 transition-transform duration-300 shadow-xl',
          mobileOpen ? 'translate-x-0' : '-translate-x-full'
        )}
      >
        {sidebarContent}
      </aside>
    </>
  );
}
