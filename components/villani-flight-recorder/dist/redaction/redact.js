export const emptyReport = () => ({
    secrets: 0,
    tokens: 0,
    envValues: 0,
    privateKeys: 0,
    connectionStrings: 0,
    highEntropyStrings: 0,
});
const rules = [
    [
        "privateKeys",
        /-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g,
        "[REDACTED_PRIVATE_KEY]",
    ],
    [
        "connectionStrings",
        /\b(?:postgres(?:ql)?|mysql|redis|mongodb):\/\/[^\s'"@]+:[^\s'"@]+@[^\s'"<>]+/gi,
        "[REDACTED_CONNECTION_STRING]",
    ],
    [
        "envValues",
        /\b(?:OPENAI_API_KEY|ANTHROPIC_API_KEY|GITHUB_TOKEN|GH_TOKEN|DATABASE_URL|POSTGRES_URL|POSTGRESQL_URL|MYSQL_URL|REDIS_URL|PASSWORD|PASS|PWD|SECRET|TOKEN|API_KEY)\s*=\s*[^\s'"`]+/gi,
        (m) => m.includes("[REDACTED_CONNECTION_STRING]")
            ? `${m.split("=")[0].trim()}=[REDACTED_CONNECTION_STRING]`
            : `${m.split("=")[0].trim()}=[REDACTED_ENV_VALUE]`,
    ],
    ["tokens", /Bearer\s+[A-Za-z0-9._~+/-]+=*/gi, "Bearer [REDACTED_TOKEN]"],
    [
        "tokens",
        /github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9_]{20,}|xox[bp]-[A-Za-z0-9-]{20,}|npm_[A-Za-z0-9_]{20,}/g,
        "[REDACTED_TOKEN]",
    ],
    [
        "secrets",
        /sk-proj-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{20,}/g,
        "[REDACTED_SECRET]",
    ],
    [
        "tokens",
        /eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/g,
        "[REDACTED_TOKEN]",
    ],
    [
        "highEntropyStrings",
        /\b(?=[A-Za-z0-9+/=_-]{48,}\b)(?=.*[A-Z])(?=.*[a-z])(?=.*\d)[A-Za-z0-9+/=_-]+\b/g,
        "[REDACTED_TOKEN]",
    ],
];
export function redactStringWithReport(s, report = emptyReport()) {
    let text = s;
    for (const [key, re, marker] of rules) {
        text = text.replace(re, (...args) => {
            report[key]++;
            return typeof marker === "function" ? marker(args[0]) : marker;
        });
    }
    return { text, report };
}
export function redactString(s) {
    return redactStringWithReport(s).text;
}
export function redactDeep(v, report = emptyReport()) {
    const walk = (x) => {
        if (typeof x === "string")
            return redactStringWithReport(x, report).text;
        if (Array.isArray(x))
            return x.map(walk);
        if (x && typeof x === "object")
            return Object.fromEntries(Object.entries(x).map(([k, val]) => [k, walk(val)]));
        return x;
    };
    const out = walk(v);
    if (out && typeof out === "object")
        out.redactionReport = report;
    return out;
}
