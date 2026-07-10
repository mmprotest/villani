export type IconName =
  | "play"
  | "external"
  | "task"
  | "model"
  | "runner"
  | "tokens"
  | "cost"
  | "clock"
  | "run"
  | "filter"
  | "fullscreen"
  | "discover"
  | "parse"
  | "normalize"
  | "correlate"
  | "terminal"
  | "branch"
  | "edit"
  | "warn"
  | "check"
  | "x"
  | "shield"
  | "review"
  | "flag"
  | "dot"
  | "minus"
  | "plus"
  | "ring"
  | "skip";
export function icon(name: IconName | string) {
  const p: Record<string, string> = {
    play: '<path d="M8 5v14l11-7z"/>',
    external:
      '<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M11 5H5v14h14v-6"/>',
    task: '<path d="M7 3h7l4 4v14H7z"/><path d="M14 3v5h5"/>',
    model:
      '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M12 12l8-4.5M12 12v9M12 12L4 7.5"/>',
    runner:
      '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M2 12h3M19 12h3"/>',
    tokens: '<path d="M4 17c3-7 6 3 9-4s5 1 7-5"/>',
    cost: '<circle cx="12" cy="12" r="9"/><path d="M12 6v12M15 9c-2-1-6-.8-6 1.3 0 2.7 6 1.3 6 4.1 0 2-4 2.3-6 1"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    run: '<path d="M7 7a5 5 0 0 1 10 0c0 5-10 5-10 10a5 5 0 0 0 10 0"/>',
    filter: '<path d="M4 5h16l-6 7v5l-4 2v-7z"/>',
    fullscreen: '<path d="M8 3H3v5M16 3h5v5M8 21H3v-5M21 16v5h-5"/>',
    discover: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="M16 16l4 4"/>',
    parse: '<path d="M7 3h7l4 4v14H7z"/><path d="M9 13h6M9 17h4"/>',
    normalize: '<path d="M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z"/>',
    correlate:
      '<path d="M8 12h8"/><circle cx="5" cy="12" r="3"/><circle cx="19" cy="12" r="3"/>',
    terminal: '<path d="M4 7l5 5-5 5M11 17h9"/>',
    branch:
      '<circle cx="6" cy="6" r="2"/><circle cx="18" cy="6" r="2"/><circle cx="12" cy="18" r="2"/><path d="M8 6h8M6 8v4a6 6 0 0 0 6 6M18 8v4a6 6 0 0 1-6 6"/>',
    edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13 7l4 4"/>',
    warn: '<path d="M12 3l10 18H2z"/><path d="M12 9v5M12 18h.01"/>',
    check: '<circle cx="12" cy="12" r="9"/><path d="M8 12l2.5 2.5L16 9"/>',
    x: '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
    shield:
      '<path d="M12 3l8 4v5c0 5-3.4 8-8 9-4.6-1-8-4-8-9V7z"/><path d="M9 12l2 2 4-4"/>',
    review: '<path d="M5 5h14v12H7l-2 2z"/><path d="M8 9h8M8 13h5"/>',
    flag: '<path d="M5 21V4h12l-2 4 2 4H5"/>',
    dot: '<circle cx="12" cy="12" r="4"/>',
    minus: '<path d="M5 12h14"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    ring: '<circle cx="12" cy="12" r="8"/>',
    skip: '<path d="M7 12h10"/>',
  };
  return `<svg viewBox="0 0 24 24" aria-hidden="true">${p[name] ?? p.dot}</svg>`;
}
