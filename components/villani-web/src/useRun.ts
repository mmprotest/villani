import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  deriveRun,
  type ArtifactDescriptor,
  type RunDetail,
  type RunEvent,
  type RunSpan,
} from "@villani/run-model";
import { ApiError, RunClient } from "./api";
import { deriveVillaniWebRunModel } from "./connectedRunModel";

export function useRun(runId: string, client: RunClient) {
  const [detail, setDetail] = useState<RunDetail>();
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [spans, setSpans] = useState<RunSpan[]>([]);
  const [artifacts, setArtifacts] = useState<ArtifactDescriptor[]>([]);
  const [spanCursor, setSpanCursor] = useState<string | null>(null);
  const [artifactCursor, setArtifactCursor] = useState<string | null>(null);
  const [connection, setConnection] = useState<
    "connecting" | "live" | "reconnecting" | "offline"
  >("connecting");
  const [error, setError] = useState<string>();
  const cursor = useRef<string | null>(null);
  const ids = useRef(new Set<string>());
  const merge = useCallback((incoming: RunEvent[]) => {
    const fresh = incoming.filter((event) => {
      const id = event.event_id ?? event.idempotency_key ?? event.id;
      if (ids.current.has(id)) return false;
      ids.current.add(id);
      return true;
    });
    if (fresh.length)
      setEvents((current) =>
        [...current, ...fresh].sort((a, b) => (a.sequence ?? 0) - (b.sequence ?? 0)),
      );
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    async function start() {
      try {
        const [run, eventPage, spanPage, artifactPage] = await Promise.all([
          client.detail(runId, controller.signal),
          client.events(runId, null, controller.signal),
          client.spans(runId, null, controller.signal),
          client.artifacts(runId, null, controller.signal),
        ]);
        setDetail(run);
        merge(eventPage.events);
        cursor.current = eventPage.cursor ?? null;
        setSpans(spanPage.values);
        setSpanCursor(spanPage.nextCursor);
        setArtifacts(artifactPage.values);
        setArtifactCursor(artifactPage.nextCursor);
        void client.subscribe(
          runId,
          cursor.current,
          {
            onEvent: (event) => merge([event]),
            onCatchUp: (incoming, next) => {
              merge(incoming);
              cursor.current = next ?? cursor.current;
            },
            onState: setConnection,
          },
          controller.signal,
        );
      } catch (reason) {
        if (!controller.signal.aborted) {
          setConnection("offline");
          setError(
            reason instanceof ApiError ? reason.message : "Unable to load this run.",
          );
        }
      }
    }
    void start();
    return () => controller.abort();
  }, [client, merge, runId]);
  const loadMoreSpans = useCallback(async () => {
    if (!spanCursor) return;
    const page = await client.spans(runId, spanCursor);
    setSpans((current) => [...current, ...page.values]);
    setSpanCursor(page.nextCursor);
  }, [client, runId, spanCursor]);
  const loadMoreArtifacts = useCallback(async () => {
    if (!artifactCursor) return;
    const page = await client.artifacts(runId, artifactCursor);
    setArtifacts((current) => [...current, ...page.values]);
    setArtifactCursor(page.nextCursor);
  }, [artifactCursor, client, runId]);
  const derived = useMemo(
    () => (detail ? deriveRun(detail, events) : undefined),
    [detail, events],
  );
  const canonical = useMemo(
    () => (detail ? deriveVillaniWebRunModel(detail) : undefined),
    [detail],
  );
  return {
    detail,
    events,
    spans,
    artifacts,
    derived,
    canonical,
    connection,
    error,
    spanCursor,
    artifactCursor,
    loadMoreSpans,
    loadMoreArtifacts,
  };
}
