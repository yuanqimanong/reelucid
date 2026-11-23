import { useEffect, useRef, useState } from "react";
import { api } from "../../api";
import { terminal } from "../../format";
import type { Job, Progress, Result, Transcript } from "../../types";

const snapshotEvents = [
  "snapshot",
  "stage.started",
  "artifact.ready",
  "job.succeeded",
  "job.partially_succeeded",
  "job.failed",
  "job.cancelled",
  "reset",
];
type AssetInfo = { name: string; media: { has_video?: boolean } };

export function useJobDetail(id: string) {
  const [job, setJob] = useState<Job | null>(null);
  const [asset, setAsset] = useState<AssetInfo | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const [loadedTranscript, setLoadedTranscript] = useState<{
    value: Transcript;
    artifactId?: string;
  } | null>(null);
  const [error, setError] = useState("");
  const [attempt, watchAgain] = useState(0);
  const requestRefresh = useRef<(() => void) | null>(null);

  useEffect(() => {
    setJob(null);
    setAsset(null);
    setResult(null);
    setLoadedTranscript(null);
    setError("");
    const controller = new AbortController();
    const { signal } = controller;
    const stream = new EventSource(`/api/v1/jobs/${id}/events`);
    let loading = false;
    let dirty = false;
    let finished = false;
    let transcriptArtifact: string | undefined;

    async function readSnapshot() {
      const value = await api<Job>(`/jobs/${id}`, { signal });
      if (signal.aborted) return;
      setJob(value);
      if (value.asset_id) {
        const info = await api<AssetInfo>(`/assets/${value.asset_id}`, {
          signal,
        });
        if (signal.aborted) return;
        setAsset(info);
      }
      if (terminal(value.status)) {
        const done = await api<Result>(
          `/jobs/${id}/result?include=transcript`,
          { signal },
        );
        if (signal.aborted) return;
        setResult(done);
        if (done.transcript) {
          const raw = done.artifacts?.find((a) => a.kind === "transcript");
          setLoadedTranscript({
            value: done.transcript,
            artifactId: raw?.artifact_id,
          });
        }
        finished = true;
        stream.close();
        clearInterval(poll);
      } else {
        const raw = value.artifacts?.find((a) => a.kind === "transcript");
        if (raw && transcriptArtifact !== raw.artifact_id) {
          const partial = await api<Transcript>(
            `/artifacts/${raw.artifact_id}/content`,
            { signal },
          );
          if (signal.aborted) return;
          transcriptArtifact = raw.artifact_id;
          setLoadedTranscript({ value: partial, artifactId: raw.artifact_id });
        }
      }
    }

    // If an event arrives during a refresh, fetch another snapshot after it finishes.
    async function refresh() {
      if (finished || signal.aborted) return;
      if (loading) {
        dirty = true;
        return;
      }
      loading = true;
      try {
        do {
          dirty = false;
          await readSnapshot();
        } while (dirty && !finished && !signal.aborted);
      } catch (e) {
        if (!signal.aborted) setError((e as Error).message);
      } finally {
        loading = false;
      }
    }
    requestRefresh.current = refresh;
    for (const event of snapshotEvents) stream.addEventListener(event, refresh);
    stream.addEventListener("stage.progress", (event) => {
      const progress = JSON.parse((event as MessageEvent).data) as Progress;
      setJob((current) => (current ? { ...current, progress } : current));
    });
    const poll = window.setInterval(refresh, 3000);
    void refresh();
    return () => {
      controller.abort();
      stream.close();
      clearInterval(poll);
      requestRefresh.current = null;
    };
  }, [id, attempt]);

  async function act(action: "cancel" | "retry") {
    try {
      await api(`/jobs/${id}/${action}`, { method: "POST" });
      // Reopen the stream after retrying a terminal job.
      if (action === "retry") watchAgain((old) => old + 1);
      else requestRefresh.current?.();
    } catch (e) {
      setError((e as Error).message);
    }
  }
  return {
    job,
    asset,
    result,
    transcript: loadedTranscript?.value ?? null,
    transcriptArtifactId: loadedTranscript?.artifactId,
    error,
    setError,
    act,
  };
}
