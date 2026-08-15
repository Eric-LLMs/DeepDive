// Poll an async enrichment job to completion.
//
// Enrichment endpoints (TTS / image fetch / explain / definition / syntax / indexing) now
// return a job_id immediately and do the work in the worker. `useJob.run(enqueue)` enqueues
// the job, then polls GET /jobs/{id} every second until the worker marks it succeeded/failed,
// resolving the returned promise with the job result (or rejecting with the error).
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { JobStatus } from "./types";

const POLL_MS = 1000;

interface JobState<T> {
  status: JobStatus | "idle";
  result: T | null;
  error: string | null;
  busy: boolean;
}

export function useJob<T = unknown>() {
  const [state, setState] = useState<JobState<T>>({
    status: "idle",
    result: null,
    error: null,
    busy: false,
  });
  const timers = useRef<Set<number>>(new Set());

  const clearTimers = useCallback(() => {
    timers.current.forEach((t) => window.clearTimeout(t));
    timers.current.clear();
  }, []);

  useEffect(() => clearTimers, [clearTimers]);

  const run = useCallback(
    (enqueue: () => Promise<{ job_id: string }>): Promise<T> =>
      new Promise<T>((resolve, reject) => {
        setState({ status: "queued", result: null, error: null, busy: true });
        enqueue()
          .then(({ job_id }) => {
            const poll = () => {
              api
                .getJob(job_id)
                .then((info) => {
                  if (info.status === "succeeded") {
                    setState({ status: "succeeded", result: info.result as T, error: null, busy: false });
                    resolve(info.result as T);
                  } else if (info.status === "failed" || info.status === "unknown") {
                    const err = info.error || "Job failed";
                    setState({ status: "failed", result: null, error: err, busy: false });
                    reject(new Error(err));
                  } else {
                    const t = window.setTimeout(poll, POLL_MS);
                    timers.current.add(t);
                  }
                })
                .catch((e) => {
                  setState({ status: "failed", result: null, error: String(e), busy: false });
                  reject(e);
                });
            };
            poll();
          })
          .catch((e) => {
            setState({ status: "failed", result: null, error: String(e), busy: false });
            reject(e);
          });
      }),
    []
  );

  return { ...state, run };
}
