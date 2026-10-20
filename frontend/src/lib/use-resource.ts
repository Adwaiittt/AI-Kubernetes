"use client";

import { useCallback, useEffect, useState } from "react";

export type Resource<T> = {
  data: T | undefined;
  error: unknown;
  reload: () => void;
};

/**
 * Fetch `fetcher()` on mount, whenever it changes, on `reload()`, and every `intervalMs`
 * (null disables polling). Out-of-order responses from superseded requests are ignored.
 */
export function useResource<T>(fetcher: () => Promise<T>, intervalMs: number | null): Resource<T> {
  const [state, setState] = useState<{ data?: T; error?: unknown }>({});
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let current = true;
    fetcher().then(
      (data) => current && setState({ data }),
      (error: unknown) => current && setState((s) => ({ ...s, error })),
    );
    return () => {
      current = false;
    };
  }, [fetcher, tick]);

  useEffect(() => {
    if (intervalMs === null) return;
    const timer = setInterval(() => setTick((t) => t + 1), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data: state.data, error: state.error, reload };
}

export function messageOf(error: unknown): string | null {
  if (error === undefined || error === null) return null;
  return error instanceof Error ? error.message : String(error);
}
