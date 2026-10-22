"use client";

import { useEffect, useState, useSyncExternalStore } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  AudioWaveform,
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
import { DEMO_URL, api, getToken, setToken, type SystemStatus } from "@/lib/api";
import { Spinner } from "@/components/ui";

const NAV = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard },
  { href: "/leads", label: "Leads", icon: Users },
  { href: "/test-call", label: "Test call", icon: PhoneOutgoing },
  { href: "/calls", label: "Call history", icon: History },
  { href: "/settings", label: "Settings", icon: Settings },
];

const noSubscribe = () => () => {};

export function ConsoleShell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  // null on the server and before hydration; the stored token afterwards.
  const token = useSyncExternalStore(noSubscribe, getToken, () => null);
  const ready = !!token;
  const [open, setOpen] = useState(false);
  const [system, setSystem] = useState<SystemStatus | null>(null);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    api.system().then(setSystem).catch(() => {});
    const t = setInterval(() => api.system().then(setSystem).catch(() => {}), 60_000);
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

  return (
    <div className="min-h-screen lg:pl-64">
      {/* Sidebar */}
      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-64 flex-col border-r border-white/5 bg-ink-900/85 backdrop-blur-xl transition-transform lg:translate-x-0 ${
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
        <nav className="flex-1 space-y-1 px-3">
          {NAV.map(({ href, label, icon: Icon }) => (
            <Link
              key={href}
              href={href}
              onClick={() => setOpen(false)}
              className={`group flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition ${
                active(href)
                  ? "bg-gradient-to-r from-ultra/30 via-hotpink/20 to-transparent text-white shadow-[inset_2px_0_0_#ff2fa8]"
                  : "text-haze hover:bg-white/5 hover:text-white"
              }`}
            >
              <Icon className={`h-[18px] w-[18px] ${active(href) ? "text-hotpink" : ""}`} />
              {label}
            </Link>
          ))}
          <a
            href={DEMO_URL}
            target="_blank"
            rel="noreferrer"
            className="mt-4 flex items-center gap-3 rounded-xl border border-cyanx/25 bg-cyanx/[0.07] px-3 py-2.5 text-sm font-medium text-cyan-100 transition hover:bg-cyanx/15"
          >
            <Mic className="h-[18px] w-[18px] text-cyanx" />
            <span>
              Try the voice demo
              <span className="block text-[11px] font-normal text-haze">Talk to the agent in your browser</span>
            </span>
          </a>
        </nav>
        <div className="space-y-3 p-4">
          {system && (
            <div className="rounded-xl border border-white/8 bg-white/[0.03] p-3 text-xs">
              <div className="flex items-center justify-between text-haze">
                <span className="flex items-center gap-1.5"><Wallet className="h-3.5 w-3.5" /> Call balance</span>
                <span className="font-display text-sm font-semibold text-volt">
                  {system.balance == null ? "—" : `₹${system.balance.toFixed(2)}`}
                </span>
              </div>
              <div className="mt-2 flex items-center gap-1.5 text-haze">
                <span className={`h-1.5 w-1.5 rounded-full ${system.public_url_ok ? "bg-volt" : "bg-amber-400"}`} />
                {system.public_url_ok ? "Ready to dial" : "Public URL not set"}
              </div>
            </div>
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
      {open && <div className="fixed inset-0 z-30 bg-black/60 lg:hidden" onClick={() => setOpen(false)} />}

      {/* Mobile top bar */}
      <header className="sticky top-0 z-20 flex items-center gap-3 border-b border-white/5 bg-ink-950/80 px-4 py-3 backdrop-blur lg:hidden">
        <button onClick={() => setOpen(true)} aria-label="Open menu" className="text-haze">
          <Menu className="h-6 w-6" />
        </button>
        <span className="font-display text-lg font-bold text-gradient">Dialwave</span>
      </header>

      <main className="mx-auto max-w-7xl px-4 py-8 sm:px-8 sm:py-10">{children}</main>
    </div>
  );
}
