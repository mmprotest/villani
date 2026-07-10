import { obj, textOf } from "../../normalize/events.js";
export function contentText(content) {
    if (typeof content === "string")
        return content;
    if (Array.isArray(content)) {
        return (content
            .filter((b) => obj(b).type !== "tool_use" && obj(b).type !== "tool_result")
            .map((b) => textOf(b))
            .filter(Boolean)
            .join("\n") || undefined);
    }
    return textOf(content);
}
export function blocks(content) {
    return Array.isArray(content)
        ? content.map(obj).filter((o) => Object.keys(o).length)
        : [];
}
