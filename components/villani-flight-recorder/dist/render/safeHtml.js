export const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
export const safeJsonForScript = (value) => JSON.stringify(value)
    .replace(/</g, "\\u003c")
    .replace(/>/g, "\\u003e")
    .replace(/&/g, "\\u0026")
    .replace(/\u2028/g, "\\u2028")
    .replace(/\u2029/g, "\\u2029");
export const truncateText = (value, maxChars = 20_000) => {
    const text = String(value ?? "");
    return text.length > maxChars
        ? `${text.slice(0, maxChars)}\n\nOutput truncated to ${maxChars.toLocaleString()} characters. Full content was larger.`
        : text;
};
