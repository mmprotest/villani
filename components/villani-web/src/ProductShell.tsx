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

type Surface = "fleet" | "run" | "ask";

export function ProductShell({
  surface,
  title,
  detail,
  status = "running",
  statusText = "CONTROL PLANE",
  children,
}: {
  surface: Surface;
  title: string;
  detail?: ReactNode;
  status?: string;
  statusText?: string;
  children: ReactNode;
}) {
  const sidebar = (
    <Sidebar
      brand={
        <>
          <span aria-hidden="true">[V]</span> VILLANI
        </>
      }
      data-testid="shared-sidebar"
    >
      <SidebarSection title="CONTROL">
        <SidebarItem href="/fleet" glyph="▦" active={surface === "fleet"}>
          Fleet
        </SidebarItem>
        <SidebarItem href="/fleet#runs" glyph="≡">
          Runs
        </SidebarItem>
        <SidebarItem href="/ask" glyph="?" active={surface === "ask"}>
          Query
        </SidebarItem>
      </SidebarSection>
      <SidebarSection title="OBSERVE">
        <SidebarItem
          href={surface === "run" ? location.pathname : "/fleet#runs"}
          glyph="◎"
          active={surface === "run"}
        >
          Run detail
        </SidebarItem>
        <SidebarItem href="/fleet#comparisons" glyph="⇄">
          Candidates
        </SidebarItem>
        <SidebarItem href="/fleet#alerts" glyph="!">
          Monitoring
        </SidebarItem>
      </SidebarSection>
      <div className="web-shell-version">
        CONTROL PLANE / WEB
        <br />
        MONOCHROME SYSTEM
      </div>
    </Sidebar>
  );
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
            actions={<span className="v-muted">LOCAL-FIRST</span>}
          />
        }
        statusStrip={
          <StatusStrip>
            <StatusBadge status={status} label={statusText} />
            <span>API / CONNECTED</span>
            <span>SCHEMA / V4</span>
            <span className="web-status-clock">
              {new Date().toISOString().slice(0, 19)}Z
            </span>
          </StatusStrip>
        }
      >
        {children}
      </AppShell>
    </>
  );
}
