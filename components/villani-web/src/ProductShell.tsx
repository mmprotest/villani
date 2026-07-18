import type { ReactNode } from "react";
import {
  ActionableSystemNotice,
  AppShell,
  PrimaryNavigation,
  Sidebar,
  StatusStrip,
  TopHeader,
} from "@villani/ui/react";
import { useConsoleEnvironment } from "./consoleContext";

export type Surface =
  | "new-task"
  | "activity"
  | "agents"
  | "settings"
  | "onboarding"
  | "replay"
  | "models"
  | "policies"
  | "fleet"
  | "tasks"
  | "costs"
  | "alerts"
  | "audit"
  | "ask";

const primaryItems = [
  { id: "new-task", label: "New task", href: "/console", glyph: "+" },
  { id: "activity", label: "Activity", href: "/console/activity", glyph: "=" },
];

const secondaryItems = [
  { id: "agents", label: "Agents", href: "/console/agents", glyph: "A" },
  { id: "settings", label: "Settings", href: "/console/settings", glyph: "S" },
];

type Notice = {
  title: string;
  detail: string;
  href: string;
  label: string;
  kind: "warning" | "error";
};

function systemNotice(
  environment: ReturnType<typeof useConsoleEnvironment>,
  status: string,
  statusText?: string,
): Notice | null {
  if (status === "failed" || status === "error")
    return {
      title: statusText ?? "This page needs attention",
      detail: "Open diagnostics for the recorded error and recovery steps.",
      href: "/console/settings#diagnostics",
      label: "Open diagnostics",
      kind: "error",
    };
  if (environment.service.status.toLowerCase() === "loading") return null;
  if (!environment.setup.configured || !environment.setup.valid)
    return {
      title: "Finish setup",
      detail:
        environment.setup.issues[0] ??
        "Choose a repository and confirm an agent connection before starting a task.",
      href: "/console/onboarding",
      label: "Continue setup",
      kind: "warning",
    };
  if (!["running", "connected"].includes(environment.service.status.toLowerCase()))
    return {
      title: "Villani service is unavailable",
      detail:
        environment.service.last_error ??
        "Start the local service to submit tasks and load recorded activity.",
      href: "/console/settings#service",
      label: "View recovery",
      kind: "error",
    };
  if (environment.data_source === "local-service" && !environment.storage.writable)
    return {
      title: "Local storage needs attention",
      detail:
        "Villani cannot safely record new run evidence until storage is writable.",
      href: "/console/settings#diagnostics",
      label: "Open diagnostics",
      kind: "error",
    };
  if (environment.synchronization.dead_letters > 0)
    return {
      title: "Some activity could not synchronize",
      detail: `${environment.synchronization.dead_letters} item${environment.synchronization.dead_letters === 1 ? "" : "s"} need attention.`,
      href: "/console/settings#diagnostics",
      label: "Review sync",
      kind: "warning",
    };
  return null;
}

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
  const activeSurface = ["new-task", "activity", "agents"].includes(surface)
    ? surface
    : "settings";
  const notice = systemNotice(environment, status, statusText);
  const sidebar = (
    <Sidebar
      brand={
        <>
          <span aria-hidden="true">V</span> VILLANI
        </>
      }
      data-testid="shared-sidebar"
    >
      <PrimaryNavigation
        data-testid="primary-navigation"
        primary={primaryItems}
        secondary={secondaryItems}
        activeId={activeSurface}
      />
      <div className="web-shell-version" aria-hidden="true">
        VILLANI
        <br />
        {environment.version}
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
        header={<TopHeader data-testid="shared-header" title={title} detail={detail} />}
        statusStrip={
          notice ? (
            <StatusStrip data-testid="actionable-system-notice">
              <ActionableSystemNotice
                title={notice.title}
                detail={notice.detail}
                actionHref={notice.href}
                actionLabel={notice.label}
                kind={notice.kind}
              />
            </StatusStrip>
          ) : undefined
        }
      >
        {children}
      </AppShell>
    </>
  );
}
