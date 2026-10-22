"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowRight, AudioWaveform, Lock, Mic, User } from "lucide-react";
import { DEMO_URL, api, setToken } from "@/lib/api";
import { ErrorNote, Spinner } from "@/components/ui";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { token } = await api.login(username, password);
      setToken(token);
      router.replace("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-md">
        <div className="mb-10 text-center">
          <div className="mx-auto mb-5 flex h-16 w-16 items-center justify-center rounded-2xl bg-brand shadow-[0_10px_40px_-8px_rgb(255_47_168/0.8)]">
            <AudioWaveform className="h-8 w-8 text-white" />
          </div>
          <h1 className="font-display text-4xl font-bold tracking-tight">
            <span className="text-gradient">Dialwave</span>
          </h1>
          <p className="mt-2 text-haze">Your AI caller. Upload leads, it does the talking.</p>
        </div>
        <form onSubmit={submit} className="card card-glow space-y-5 p-7">
          <div>
            <label className="label" htmlFor="username">Username</label>
            <div className="relative">
              <User className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-haze" />
              <input
                id="username"
                autoFocus
                autoComplete="username"
                className="input pl-10"
                placeholder="admin"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
            </div>
          </div>
          <div>
            <label className="label" htmlFor="password">Password</label>
            <div className="relative">
              <Lock className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-haze" />
              <input
                id="password"
                type="password"
                autoComplete="current-password"
                className="input pl-10"
                placeholder="••••••••"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
          </div>
          <ErrorNote message={error} />
          <button className="btn btn-primary w-full py-3 text-base" disabled={busy || !username || !password}>
            {busy ? <Spinner /> : <>Enter console <ArrowRight className="h-4 w-4" /></>}
          </button>
        </form>
        <a href={DEMO_URL} target="_blank" rel="noreferrer" className="mt-6 flex items-center justify-center gap-2 text-sm text-haze hover:text-white">
          <Mic className="h-4 w-4 text-cyanx" /> Want to test it first? <span className="font-semibold text-cyanx underline-offset-4 hover:underline">Try the voice demo</span>
        </a>
      </div>
    </main>
  );
}
