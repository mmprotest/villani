import { useEffect, useMemo, useState } from "react";
import type { ConsoleModel } from "@villani/run-model";
import {
  ErrorState,
  PageIntro,
  Panel,
  PrimaryAction,
  ProgressStages,
  SecondaryAction,
  Select,
} from "@villani/ui/react";
import { ConsoleClient, type ConsoleRunOptions } from "./consoleApi";
import { useConsoleEnvironment } from "./consoleContext";
import { ProductShell } from "./ProductShell";

const stages = [
  { id: "repository", label: "Repository" },
  { id: "agent", label: "Agent connection" },
  { id: "verification", label: "Verification" },
  { id: "ready", label: "Ready" },
];

type SavedSetup = { stage?: number; repository?: string; backend?: string };

function savedSetup(): SavedSetup {
  try {
    return JSON.parse(
      localStorage.getItem("villani.onboarding.v1") ?? "{}",
    ) as SavedSetup;
  } catch {
    return {};
  }
}

export function OnboardingPage({ client }: { client: ConsoleClient }) {
  const environment = useConsoleEnvironment();
  const saved = useMemo(savedSetup, []);
  const [options, setOptions] = useState<ConsoleRunOptions | null>(null);
  const [models, setModels] = useState<ConsoleModel[]>(environment.models);
  const [stage, setStage] = useState(environment.setup.valid ? 3 : (saved.stage ?? 0));
  const [repository, setRepository] = useState(saved.repository ?? "");
  const [backend, setBackend] = useState(saved.backend ?? "");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    void client
      .runOptions(controller.signal)
      .then((value) => {
        setOptions(value);
        setRepository((current) => current || value.default_repository || "");
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted)
          setError(reason instanceof Error ? reason.message : String(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [client]);

  useEffect(() => {
    const preferred =
      models.find((model) => model.bootstrap_default) ??
      models.find((model) => model.configured) ??
      models[0];
    if (preferred)
      setBackend((current) => current || preferred.backend_name || preferred.id);
  }, [models]);

  useEffect(() => {
    try {
      localStorage.setItem(
        "villani.onboarding.v1",
        JSON.stringify({ stage, repository, backend }),
      );
    } catch {
      // Setup still works when storage is blocked; only cross-reload resumption is lost.
    }
  }, [backend, repository, stage]);

  const selectedModel = models.find(
    (model) => (model.backend_name || model.id) === backend,
  );
  const selectedRepository = options?.repositories.find(
    (item) => item.path === repository,
  );

  const detectAgents = async () => {
    setBusy(true);
    setError(null);
    try {
      const inventory = await client.detectModels();
      setModels(inventory.models);
      if (!inventory.models.length)
        setError(
          "No configured agent connection was detected. Add one in Advanced agent settings.",
        );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  const verifyAgent = async () => {
    if (!selectedModel) return;
    setBusy(true);
    setError(null);
    try {
      const result = await client.testModels(selectedModel.backend_name ?? undefined);
      const tested = result.results[0];
      if (!tested || tested.availability !== "available") {
        setError(
          tested?.diagnostic ?? "Villani could not verify this agent connection.",
        );
        return;
      }
      setModels((current) =>
        current.map((model) =>
          model.id === selectedModel.id
            ? {
                ...model,
                availability: tested.availability,
                available: true,
                last_tested_at: tested.tested_at,
                last_test_diagnostic: tested.diagnostic,
              }
            : model,
        ),
      );
      setStage(3);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  const taskUrl = `/console?repository=${encodeURIComponent(repository)}`;
  return (
    <ProductShell surface="onboarding" title="Setup">
      <div className="console-stack onboarding-flow" data-testid="onboarding-flow">
        <PageIntro title="Set up Villani">
          Four short decisions, saved as you go. Technical connection details stay
          available under Advanced.
        </PageIntro>
        <ProgressStages stages={stages} current={stage} aria-label="Setup progress" />
        {error && <ErrorState title="Setup needs attention" detail={error} />}
        {loading && !error && (
          <p className="v-muted">Looking for safe local defaults…</p>
        )}
        {!loading && stage === 0 && (
          <Panel className="onboarding-decision">
            <div className="v-panel__body console-stack">
              <div>
                <span className="onboarding-step-label">1 of 4</span>
                <h2>Which repository should Villani work in?</h2>
                <p className="v-secondary">
                  A clean detected Git repository is preselected when available.
                </p>
              </div>
              <Select
                label="Repository"
                value={repository}
                onChange={(event) => setRepository(event.target.value)}
                error={
                  selectedRepository?.dirty
                    ? "Commit or stash existing changes before continuing."
                    : selectedRepository && !selectedRepository.valid
                      ? "Choose a valid Git repository."
                      : undefined
                }
                options={(options?.repositories ?? []).map((item) => ({
                  value: item.path,
                  label: item.name,
                  disabled: !item.valid || item.dirty === true,
                }))}
              />
              <PrimaryAction
                disabled={
                  !selectedRepository?.valid || selectedRepository.dirty === true
                }
                onClick={() => setStage(1)}
              >
                Use this repository
              </PrimaryAction>
            </div>
          </Panel>
        )}
        {!loading && stage === 1 && (
          <Panel className="onboarding-decision">
            <div className="v-panel__body console-stack">
              <div>
                <span className="onboarding-step-label">2 of 4</span>
                <h2>Which agent system should Villani use?</h2>
                <p className="v-secondary">
                  Villani preselects the configured default and does not infer
                  capability or pricing.
                </p>
              </div>
              {models.length ? (
                <Select
                  label="Agent system"
                  value={backend}
                  onChange={(event) => setBackend(event.target.value)}
                  options={models.map((model) => ({
                    value: model.backend_name || model.id,
                    label: model.display_name || model.model,
                    disabled: !model.configured,
                  }))}
                />
              ) : (
                <p>No configured agent connection has been found yet.</p>
              )}
              <div className="v-cluster">
                <SecondaryAction onClick={() => setStage(0)}>Back</SecondaryAction>
                {models.length ? (
                  <PrimaryAction disabled={!selectedModel} onClick={() => setStage(2)}>
                    Use this agent
                  </PrimaryAction>
                ) : (
                  <PrimaryAction disabled={busy} onClick={() => void detectAgents()}>
                    {busy ? "Detecting…" : "Detect agents"}
                  </PrimaryAction>
                )}
              </div>
              <details className="run-advanced">
                <summary>Advanced connection details</summary>
                <dl className="run-result-list">
                  <div>
                    <dt>Endpoint</dt>
                    <dd>{selectedModel?.endpoint ?? "Not recorded"}</dd>
                  </div>
                  <div>
                    <dt>Pricing</dt>
                    <dd>{selectedModel?.pricing_status ?? "unknown"}</dd>
                  </div>
                  <div>
                    <dt>Capability</dt>
                    <dd>{selectedModel?.capability_status ?? "UNRATED"}</dd>
                  </div>
                </dl>
                <a href="/console/models">Open Advanced agent settings</a>
              </details>
            </div>
          </Panel>
        )}
        {!loading && stage === 2 && (
          <Panel className="onboarding-decision">
            <div className="v-panel__body console-stack">
              <div>
                <span className="onboarding-step-label">3 of 4</span>
                <h2>Verify the agent connection</h2>
                <p className="v-secondary">
                  This checks availability without spending model tokens or changing a
                  repository.
                </p>
              </div>
              <div className="onboarding-agent-summary">
                <strong>{selectedModel?.display_name ?? "Agent connection"}</strong>
                <span>{selectedModel?.provider ?? "Provider not recorded"}</span>
              </div>
              <div className="v-cluster">
                <SecondaryAction onClick={() => setStage(1)}>Back</SecondaryAction>
                <PrimaryAction
                  disabled={!selectedModel || busy}
                  onClick={() => void verifyAgent()}
                >
                  {busy ? "Verifying…" : "Verify connection"}
                </PrimaryAction>
              </div>
              <details className="run-advanced">
                <summary>Advanced verification details</summary>
                <p>
                  Service: {environment.service.status}. Last result:{" "}
                  {selectedModel?.last_test_diagnostic ?? "Not tested"}.
                </p>
              </details>
            </div>
          </Panel>
        )}
        {!loading && stage === 3 && (
          <Panel className="onboarding-decision" data-testid="setup-complete">
            <div className="v-panel__body console-stack">
              <div>
                <span className="onboarding-step-label">4 of 4</span>
                <h2>Villani is ready for your task</h2>
                <p className="v-secondary">
                  Your repository is selected. Describe a real task now, or try the
                  disposable sample later.
                </p>
              </div>
              <a className="v-button onboarding-primary-link" href={taskUrl}>
                Open New task
              </a>
              <a href={`${taskUrl}&sample=offered`}>View the optional sample</a>
            </div>
          </Panel>
        )}
      </div>
    </ProductShell>
  );
}
