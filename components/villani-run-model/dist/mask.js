const SENSITIVE = /(^|_)(secret|token|password|credential|authorization|api_key|private_key)($|_)/i;
export function maskSensitive(value, reveal = false) {
    if (reveal || value === null || value === undefined)
        return value;
    if (Array.isArray(value))
        return value.map((item) => maskSensitive(item));
    if (typeof value !== "object")
        return value;
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [
        key,
        SENSITIVE.test(key) ? "••••••••" : maskSensitive(item),
    ]));
}
export const artifactMayRender = (sensitivity) => sensitivity !== "secret";
