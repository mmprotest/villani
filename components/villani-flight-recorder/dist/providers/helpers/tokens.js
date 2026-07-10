import { obj } from "../../normalize/events.js";
const usagePaths = [
    ["message", "usage"],
    ["usage"],
    ["response", "usage"],
    ["result", "usage"],
    ["token_usage"],
    ["metrics", "usage"],
];
const fieldMap = {
    inputTokens: ["input_tokens", "inputTokens", "prompt_tokens", "promptTokens"],
    outputTokens: [
        "output_tokens",
        "outputTokens",
        "completion_tokens",
        "completionTokens",
    ],
    totalTokens: ["total_tokens", "totalTokens"],
    cacheCreationTokens: [
        "cache_creation_input_tokens",
        "cacheCreationInputTokens",
    ],
    cacheReadTokens: ["cache_read_input_tokens", "cacheReadInputTokens"],
    cachedTokens: ["cached_tokens", "cachedTokens"],
    reasoningTokens: ["reasoning_tokens", "reasoningTokens"],
};
function numberOf(v) {
    if (typeof v === "number" && Number.isFinite(v))
        return v;
    if (typeof v === "string" && /^\d+(?:\.\d+)?$/.test(v.trim())) {
        const n = Number(v);
        if (Number.isFinite(n))
            return n;
    }
    return undefined;
}
function atPath(record, path) {
    return path.reduce((cur, key) => obj(cur)[key], record);
}
function normalizeUsage(raw, source) {
    const o = obj(raw);
    if (!Object.keys(o).length)
        return undefined;
    const usage = { source };
    for (const [target, keys] of Object.entries(fieldMap)) {
        const n = keys.map((k) => numberOf(o[k])).find((v) => v !== undefined);
        if (n !== undefined)
            usage[target] = n;
    }
    if (usage.totalTokens === undefined) {
        const total = [
            usage.inputTokens,
            usage.outputTokens,
            usage.cacheCreationTokens,
            usage.cacheReadTokens,
            usage.cachedTokens,
            usage.reasoningTokens,
        ].reduce((a, b) => a + (b ?? 0), 0);
        if (total > 0)
            usage.totalTokens = total;
    }
    return Object.keys(usage).some((k) => k !== "source") ? usage : undefined;
}
export function extractTokenUsage(record) {
    for (const p of usagePaths) {
        const found = normalizeUsage(atPath(record, p), p.join("."));
        if (found)
            return found;
    }
    return undefined;
}
export function sumTokenUsage(events) {
    const total = { source: "sum" };
    let any = false;
    for (const e of events) {
        const u = e.tokenUsage;
        if (!u)
            continue;
        any = true;
        for (const k of Object.keys(fieldMap)) {
            const v = u[k];
            if (typeof v === "number" && Number.isFinite(v))
                total[k] = (total[k] ?? 0) + v;
        }
    }
    if (any && total.totalTokens === undefined) {
        const n = [
            total.inputTokens,
            total.outputTokens,
            total.cacheCreationTokens,
            total.cacheReadTokens,
            total.cachedTokens,
            total.reasoningTokens,
        ].reduce((a, b) => a + (b ?? 0), 0);
        if (n > 0)
            total.totalTokens = n;
    }
    return any ? total : undefined;
}
export function formatTokenCount(n) {
    if (typeof n !== "number" || !Number.isFinite(n))
        return "—";
    if (Math.abs(n) >= 1_000_000)
        return `${(n / 1_000_000).toFixed(n % 1_000_000 ? 1 : 0)}m`;
    if (Math.abs(n) >= 1_000)
        return `${(n / 1_000).toFixed(n % 1_000 ? 1 : 0)}k`;
    return String(n);
}
export function formatTokenUsage(usage) {
    if (!usage)
        return "Not captured";
    const parts = [
        ["input", usage.inputTokens],
        ["output", usage.outputTokens],
        [
            "cache",
            (usage.cacheCreationTokens ?? 0) +
                (usage.cacheReadTokens ?? 0) +
                (usage.cachedTokens ?? 0) || undefined,
        ],
        ["reasoning", usage.reasoningTokens],
        ["total", usage.totalTokens],
    ].filter((x) => typeof x[1] === "number");
    return parts.length
        ? parts.map(([k, v]) => `${k} ${formatTokenCount(v)}`).join(" · ")
        : "Not captured";
}
