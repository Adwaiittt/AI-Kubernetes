"use client";

import { useEffect, useRef, useState } from "react";

import { API_URL, type TraceEvent } from "./api/client";
import { mergeEvents } from "./timeline";

export type StreamState = {
  events: TraceEvent[];
  connected: boolean;
  /** Final status from the server's `end` event, once the investigation is done. */
  endedWith: string | null;
};

/**
 * Follow an incident's live agent trace over SSE. The server replays persisted events first,
 * and EventSource reconnects with Last-Event-ID, so merging by seq makes this exactly-once.
 */
export function useIncidentStream(
  incidentId: string,
  enabled: boolean,
  onEnd?: (status: string) => void,
): StreamState {
  const [state, setState] = useState<StreamState>({
    events: [],
    connected: false,
    endedWith: null,
  });
  // Latest callback without re-opening the stream when it changes identity.
  const onEndRef = useRef(onEnd);
  useEffect(() => {
    onEndRef.current = onEnd;
  }, [onEnd]);

  useEffect(() => {
    if (!enabled) return;
    const source = new EventSource(`${API_URL}/incidents/${encodeURIComponent(incidentId)}/stream`);

    source.onopen = () => setState((s) => ({ ...s, connected: true }));
    source.addEventListener("trace", (msg) => {
      const event = JSON.parse((msg as MessageEvent<string>).data) as TraceEvent;
      setState((s) => ({ ...s, events: mergeEvents(s.events, [event]) }));
    });
    source.addEventListener("end", (msg) => {
      const { status } = JSON.parse((msg as MessageEvent<string>).data) as { status: string };
      setState((s) => ({ ...s, connected: false, endedWith: status }));
      source.close();
      onEndRef.current?.(status);
    });
    source.onerror = () => setState((s) => ({ ...s, connected: false }));

    return () => source.close();
  }, [incidentId, enabled]);

  return state;
}
