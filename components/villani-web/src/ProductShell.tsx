import type { ReactNode } from "react";
import {
  AppShell,
  Sidebar,
  SidebarItem,
  SidebarSection,
  StatusBadge,
  StatusStrip,
  TopHeader,
} from "@villani/ui/react";
import { useConsoleEnvironment } from "./consoleContext";

export type Surface =
  | "home"
  | "run"
  | "history"
  | "replay"
  | "models"
  | "policies"
  | "settings"
  | "fleet"
  | "tasks"
  | "costs"
  | "alerts"
  | "audit"
  | "ask";

const localItems: { id: Surface; label: string; href: string; glyph: string }[] = [
  { id: "home", label: "Home", href: "/console", glyph: "H" },
  { id: "run", label: "Run", href: "/console/run", glyph: ">" },
  { id: "history", label: "History", href: "/console/history", glyph: "=" },
  { id: "replay", label: "Replay", href: "/console/replay", glyph: "@" },
  { id: "models", label: "Models", href: "/console/models", glyph: "M" },
  { id: "policies", label: "Policies", href: "/console/policies", glyph: "P" },
  { id: "settings", label: "Settings", href: "/console/settings", glyph: "S" },
];

const teamItems: { id: Surface; label: string; href: string; glyph: string }[] = [
  { id: "fleet", label: "Fleet", href: "/console/fleet", glyph: "F" },
  { id: "tasks", label: "Tasks", href: "/console/tasks", glyph: "T" },
  { id: "costs", label: "Costs", href: "/console/costs", glyph: "$" },
  { id: "alerts", label: "Alerts", href: "/console/alerts", glyph: "!" },
  { id: "audit", label: "Audit", href: "/console/audit", glyph: "A" },
];

export function ProductShell({
  surface,
  title,
  detail,
  status = "running",
  statusText,
  children,
}: {
  surface: Surface;
  title: string;
  detail?: ReactNode;
  status?: string;
  statusText?: string;
  children: ReactNode;
}) {
  const environment = useConsoleEnvironment();
  const connected = environment.workspace.connected;
  const activeSurface = surface === "ask" ? "audit" : surface;
  const sidebar = (
    <Sidebar
      brand={
        <>
          <span aria-hidden="true">[V]</span> VILLANI
        </>
      }
      data-testid="shared-sidebar"
    >
      <SidebarSection title="LOCAL">
        {localItems.map((item) => (
          <SidebarItem
            key={item.id}
            href={item.href}
            glyph={item.glyph}
            active={activeSurface === item.id}
          >
            {item.label}
          </SidebarItem>
        ))}
      </SidebarSection>
      {connected && (
        <SidebarSection title="TEAM" data-testid="team-navigation">
          {teamItems.map((item) => (
            <SidebarItem
              key={item.id}
              href={item.href}
              glyph={item.glyph}
              active={activeSurface === item.id}
            >
              {item.label}
            </SidebarItem>
          ))}
        </SidebarSection>
      )}
      <div className="web-shell-version">
        VILLANI CONSOLE
        <br />
        {environment.version}
      </div>
    </Sidebar>
  );
  const mode = connected ? "CONNECTED" : "LOCAL";
  return (
    <>
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>
      <AppShell
        data-testid="shared-app-shell"
        sidebar={sidebar}
        header={
          <TopHeader
            data-testid="shared-header"
            title={title}
            detail={detail}
            actions={<span className="v-muted">{mode}</span>}
          />
        }
        statusStrip={
          <StatusStrip>
            <StatusBadge
              status={status}
              label={
                statusText ?? `SERVICE / ${environment.service.status.toUpperCase()}`
              }
            />
            <span>MODE / {mode}</span>
            <span>SYNC / {environment.synchronization.pending} PENDING</span>
            {environment.synchronization.dead_letters > 0 && (
              <span>FAILED / {environment.synchronization.dead_letters}</span>
            )}
          </StatusStrip>
        }
      >
        {children}
      </AppShell>
    </>
  );
}
