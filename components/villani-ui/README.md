# Villani UI

`@villani/ui` is the shared monochrome terminal-control-plane design system for Villani Web,
Flight Recorder, and static replay output. `theme.css` and the generated `theme-source.js` are the
single visual source of truth: near-black surfaces, monospaced typography, square borders, visible
focus, reduced-motion behavior, shared shell dimensions, and text-plus-glyph status indicators.

The React entry point (`@villani/ui/react`) exports `AppShell`, `Sidebar`, `SidebarSection`,
`SidebarItem`, `TopHeader`, `StatusStrip`, `Panel`, `PanelHeader`, `MetricCard`, `DataTable`,
`EventTable`, `StatusBadge`, `Button`, `IconButton`, `TextInput`, `Select`, `Tabs`, `Tooltip`,
`Dialog`, `Drawer`, `EmptyState`, `ErrorState`, `LoadingState`, `Timeline`, `TimelineNode`,
`KeyValueGrid`, `AsciiFrame`, `AsciiCorners`, and `Sparkline`. The base entry point exports the
shared tokens, chart tokens, class names, status descriptors, and serialized theme CSS.

Run the package checks with Node 20 or newer:

```console
npm test
npm run build
npm pack --dry-run
```

Theme regression tests in the package, final-foundation suite, and connected Playwright gate check
the exported primitives as well as rendered computed styles. A light root/panel, green or blue
primary accent, divergent shell dimensions, missing shared shell, or legacy cream surface fails the
release gate.
