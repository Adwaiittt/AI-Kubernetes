"use client";

import { useSyncExternalStore } from "react";

/**
 * The approver's API key lives in sessionStorage: it survives reloads within the tab but is
 * gone when the tab closes, and it is never sent anywhere except the backend's X-API-Key header.
 */
const STORAGE_KEY = "k8s-ai-monitor.approver-key";
const listeners = new Set<() => void>();

function read(): string | null {
  try {
    return window.sessionStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setApproverKey(value: string | null): void {
  try {
    if (value) window.sessionStorage.setItem(STORAGE_KEY, value);
    else window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // storage unavailable (private mode): the key just won't persist
  }
  listeners.forEach((l) => l());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useApproverKey(): string | null {
  return useSyncExternalStore(subscribe, read, () => null);
}
