"use client";

import { useEffect, useState, useSyncExternalStore } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  AlertTriangle,
  AudioWaveform,
  FileText,
  History,
  LayoutDashboard,
  LogOut,
  Menu,
  Mic,
  PhoneOutgoing,
  Settings,
  Users,
  Wallet,
  X,
} from "lucide-react";
import { api, demoUrl, fmtInr, getToken, setToken, type AccountsOverview } from "@/lib/api";
import { Spinner } from "@/components/ui";

const NAV = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard },
  { href: "/leads", label: "Leads", icon: Users },
  { href: "/scripts", label: "Scripts", icon: FileText },
  { href: "/test-call", label: "Test call", icon: PhoneOutgoing },
  { href: "/calls", label: "Call history", icon: History },
  { href: "/accounts", label: "Accounts & cost", icon: Wallet },
  { href: "/settings", label: "Settings", icon: Settings },
];

const noSubscribe = () => () => {};

export async function openDemo(params: Record<string, string> = {}) {
  window.open(await demoUrl(params), "_blank", "noopener");
}

export function ConsoleShell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  // null on the server and before hydration; the stored token afterwards.
  const token = useSyncExternalStore(noSubscribe, getToken, () => null);
  const ready = !!token;
  const [open, setOpen] = useState(false);
  const [accounts, setAccounts] = useState<AccountsOverview | null>(null);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    const load = () => api.accounts().then(setAccounts).catch(() => {});
    load();
    const t = setInterval(() => {
      if (document.visibilityState === "visible") load();
    }, 60_000);
    return () => clearInterval(t);
  }, [router]);

  if (!ready) {
    return (
      <div className="flex min-h-screen items-center justify-center text-haze">
        <Spinner className="h-6 w-6" />
      </div>
    );
  }

  const active = (href: string) => (href === "/" ? pathname === "/" : pathname.startsWith(href));
  const inUse = accounts?.accounts.filter((a) => a.in_use && !a.custom) ?? [];
  const warnings = [
    ...(accounts?.problems ?? []),
    ...inUse.filter((a) => a.linked && a.low).map((a) => `${a.name} balance is low — about ${a.calls_left} calls left.`),
  ];

  return (
    <div className="min-h-screen lg:pl-64">
      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-64 flex-col border-r border-ink-700 bg-white transition-transform lg:translate-x-0 ${
          open ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div className="flex items-center gap-3 px-6 py-6">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-brand">
            <AudioWaveform className="h-5 w-5 text-white" />
          </div>
          <div>
            <p className="font-display text-xl font-bold leading-none text-gradient">Dialwave</p>
            <p className="mt-1 text-[11px] uppercase tracking-[0.18em] text-haze">AI sales caller</p>
          </div>
          <button className="ml-auto text-haze lg:hidden" onClick={() => setOpen(false)} aria-label="Close menu">
            <X className="h-5 w-5" />
          </button>
        </div>
        <nav className="flex-1 space-y-1 overflow-y-auto px-3">
          {NAV.map(({ href, label, icon: Icon }) => (
            <Link
              key={href}
              href={href}
              onClick={() => setOpen(false)}
              className={`flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition ${
                active(href) ? "bg-ultra/10 text-ultra" : "text-haze hover:bg-ink-800 hover:text-ink"
              }`}
            >
              <Icon className="h-[18px] w-[18px]" />
              {label}
            </Link>
          ))}
          <button
            onClick={() => openDemo()}
            className="mt-4 flex w-full items-center gap-3 rounded-xl border border-cyanx/25 bg-cyanx/[0.06] px-3 py-2.5 text-left text-sm font-medium text-cyan-800 transition hover:bg-cyanx/15"
          >
            <Mic className="h-[18px] w-[18px] text-cyanx" />
            <span>
              Try the voice demo
              <span className="block text-[11px] font-normal text-haze">Talk to the agent in your browser</span>
            </span>
          </button>
        </nav>
        <div className="space-y-3 p-4">
          {accounts && (
            <Link href="/accounts" className="block rounded-xl border border-ink-700 bg-ink-950 p-3 text-xs hover:border-ultra/40">
              <p className="mb-2 font-semibold uppercase tracking-wider text-haze">Balances</p>
              <ul className="space-y-1.5">
                {inUse.map((a) => (
                  <li key={a.id} className="flex items-center justify-between gap-2">
                    <span className="flex items-center gap-1.5 text-haze">
                      <span className={`h-1.5 w-1.5 rounded-full ${!a.linked ? "bg-red-500" : a.low ? "bg-amber-500" : "bg-volt"}`} />
                      {a.name}
                    </span>
                    <span className={`font-display font-semibold tabular-nums ${a.low ? "text-amber-700" : ""}`}>
                      {!a.linked ? "Not linked" : a.balance ? `${a.balance_kind === "estimated" ? "~" : ""}${fmtInr(a.balance.inr, 0)}` : "Set balance"}
                    </span>
                  </li>
                ))}
              </ul>
              <p className="mt-2 border-t border-ink-700 pt-2 text-haze">
                About <span className="font-semibold text-ink">{fmtInr(accounts.per_call.total)}</span> per {Math.round(accounts.per_call.seconds / 60)}-min call
              </p>
            </Link>
          )}
          <button
            className="btn btn-ghost w-full"
            onClick={() => {
              setToken(null);
              router.replace("/login");
            }}
          >
            <LogOut className="h-4 w-4" /> Log out
          </button>
        </div>
      </aside>
      {open && <div className="fixed inset-0 z-30 bg-black/30 lg:hidden" onClick={() => setOpen(false)} />}

      <header className="sticky top-0 z-20 flex items-center gap-3 border-b border-ink-700 bg-white px-4 py-3 lg:hidden">
        <button onClick={() => setOpen(true)} aria-label="Open menu" className="text-haze">
          <Menu className="h-6 w-6" />
        </button>
        <span className="font-display text-lg font-bold text-gradient">Dialwave</span>
      </header>

      <main className="mx-auto max-w-7xl px-4 py-8 sm:px-8 sm:py-10">
        {warnings.length > 0 && !pathname.startsWith("/accounts") && (
          <Link
            href="/accounts"
            className="mb-6 flex items-start gap-2 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-800 hover:bg-amber-100"
          >
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>
              {warnings[0]}
              {warnings.length > 1 && ` (+${warnings.length - 1} more)`} <span className="font-semibold underline">Open Accounts</span>
            </span>
          </Link>
        )}
        {children}
      </main>
    </div>
  );
}
