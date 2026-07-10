export function pluralize(count, singular, plural = `${singular}s`) {
    return `${count} ${count === 1 ? singular : plural}`;
}
export function formatTimelineTime(timestamp) {
    if (!timestamp)
        return "—";
    const d = new Date(timestamp);
    if (Number.isNaN(d.getTime()))
        return timestamp;
    return d.toISOString().slice(11, 19);
}
