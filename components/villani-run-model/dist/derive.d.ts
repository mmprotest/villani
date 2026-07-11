import type { DerivedRun, RunDetail, RunEvent, RunStatusSummary } from "./types.js";
export declare function deriveRunStatus(events: RunEvent[], controllerState?: string): RunStatusSummary;
export declare function deriveRun(detail: RunDetail, events: RunEvent[]): DerivedRun;
