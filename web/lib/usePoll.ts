"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// Fetches now, then every `intervalMs` while the tab is visible. `refresh()` refetches
// immediately (e.g. right after an action).
export function usePoll<T>(fetcher: () => Promise<T>, intervalMs = 3000) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fetcherRef = useRef(fetcher);
  useEffect(() => {
    fetcherRef.current = fetcher;
  });

  const refresh = useCallback(async () => {
    try {
      setData(await fetcherRef.current());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed");
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(() => {
      if (document.visibilityState === "visible") refresh();
    }, intervalMs);
    return () => clearInterval(t);
  }, [refresh, intervalMs]);

  return { data, error, refresh };
}
