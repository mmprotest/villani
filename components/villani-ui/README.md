# Villani UI

`@villani/ui` is the shared monochrome product design system for Villani Web, onboarding, Flight
Recorder, and static replay output. `theme.css` and `theme-source.js` are the single visual source
of truth: white and light-grey surfaces, system typography with monospace reserved for evidence,
consistent borders and radii, visible focus, reduced-motion behavior, shared shell dimensions, and
text-plus-glyph status indicators.

The React entry point (`@villani/ui/react`) exports `AppShell`, `Sidebar`, `SidebarSection`,
`SidebarItem`, `PrimaryNavigation`, `TopHeader`, `StatusStrip`, `ActionableSystemNotice`, `PageIntro`,
`TaskComposerShell`, `ProgressStages`, `ResultVerdict`, `EvidenceDisclosure`, `Panel`, `PanelHeader`,
`MetricCard`, `DataTable`, `EventTable`, `StatusBadge`, accessible form fields, primary and secondary
actions, `CostDisplay`, `DurationDisplay`, overlays, states, timelines, evidence grids, and charts.
The base entry point exports the shared tokens, chart tokens, class names, status descriptors, and
serialized theme CSS.

Run the package checks with Node 20 or newer:

```console
npm test
npm run build
npm pack --dry-run
```

Theme regression tests in the package and the Playwright gates check the exported primitives and
rendered computed styles. A standalone onboarding palette, green or blue primary accent, divergent
shell, missing focus behavior, or legacy dark control-plane surface fails PT1 verification.
