import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type FormEvent,
} from "react";
import type { ProductRun, ProductRunAction } from "@villani/run-model";
import {
  CostDisplay,
  DurationDisplay,
  ErrorState,
  EvidenceDisclosure,
  FormField,
  KeyValueGrid,
  LoadingState,
  PageIntro,
  Panel,
  PanelHeader,
  PrimaryAction,
  ProgressStages,
  ResultVerdict,
  SecondaryAction,
  TaskComposerShell,
} from "@villani/ui/react";
import {
  ConsoleClient,
  type ConsoleRunOptions,
  type ConsoleValidationDiscovery,
  type PolicyPreview,
  type RunFailure,
} from "./consoleApi";
import { ProductShell } from "./ProductShell";
import { useConsoleEnvironment } from "./consoleContext";

const DRAFT_KEY = "villani.new-task.draft.v1";
const SUBMISSION_KEY = "villani.new-task.submission.v1";
const STAGES = ["Understanding", "Working", "Checking", "Ready"] as const;

interface TaskDraft {
  repository: string;
  task: string;
  successCriteria: string;
  referenceText: string;
  manualValidation: string;
}

const emptyDraft: TaskDraft = {
  repository: "",
  task: "",
  successCriteria: "",
  referenceText: "",
  manualValidation: "",
};

function restoreDraft(): TaskDraft {
  try {
    const value = JSON.parse(
      localStorage.getItem(DRAFT_KEY) ?? "null",
    ) as Partial<TaskDraft>;
    if (!value || typeof value !== "object") return emptyDraft;
    return {
      repository: typeof value.repository === "string" ? value.repository : "",
      task: typeof value.task === "string" ? value.task : "",
      successCriteria:
        typeof value.successCriteria === "string" ? value.successCriteria : "",
      referenceText: typeof value.referenceText === "string" ? value.referenceText : "",
      manualValidation:
        typeof value.manualValidation === "string" ? value.manualValidation : "",
    };
  } catch {
    return emptyDraft;
  }
}

const safeSessionGet = (key: string) => {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
};

function restoreSubmission(value: string | null) {
  if (!value) return null;
  try {
    const parsed = JSON.parse(value) as { fingerprint?: unknown; id?: unknown };
    return {
      fingerprint:
        typeof parsed.fingerprint === "string" ? parsed.fingerprint : undefined,
      id: typeof parsed.id === "string" ? parsed.id : undefined,
    };
  } catch {
    return null;
  }
}

function FailureDetails({ failure }: { failure: RunFailure }) {
  return (
    <div className="console-stack" role="alert">
      <strong>{failure.what_failed}</strong>
      <p>{failure.patch_status}</p>
      <p>
        <strong>Next:</strong> {failure.next_action}
      </p>
    </div>
  );
}

async function readAttachments(files: File[]) {
  let combined = 0;
  const values = [];
  for (const file of files) {
    if (file.size > 100_000)
      throw new Error(`${file.name} is larger than the 100 KB text attachment limit.`);
    const content = await file.text();
    combined += content.length;
    if (combined > 100_000)
      throw new Error("Text attachments exceed the combined 100 KB limit.");
    values.push({ name: file.name, content, media_type: file.type || "text/plain" });
  }
  return values;
}

function ProductResult({
  value,
  client,
  onUpdate,
}: {
  value: ProductRun;
  client: ConsoleClient;
  onUpdate: (value: ProductRun) => void;
}) {
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const stageIndex = Math.max(STAGES.indexOf(value.current_stage), 0);
  const terminal = value.final_verdict !== null;

  const runAction = async (action: ProductRunAction) => {
    if (action.method === "GET") {
      location.assign(action.href);
      return;
    }
    setPendingAction(action.id);
    setActionError(null);
    try {
      if (action.id === "cancel")
        onUpdate(await client.cancelRun(value.run_identity.run_id));
      else
        onUpdate(
          await client.approvalAction(value.run_identity.run_id, {
            action: "approve",
            reason: `${action.label} selected from the product result.`,
          }),
        );
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setPendingAction(null);
    }
  };

  if (!terminal) {
    const cancel = value.available_actions.find((action) => action.id === "cancel");
    return (
      <section
        className="console-stack run-result"
        aria-live="polite"
        data-testid="run-presentation"
      >
        <ProgressStages
          stages={STAGES.map((label) => ({ id: label, label }))}
          current={stageIndex}
          aria-label="Task progress"
        />
        <Panel>
          <PanelHeader title={value.current_stage.toUpperCase()} />
          <div className="v-panel__body console-stack">
            <p className="run-current-sentence">{value.stage_sentence}</p>
            <KeyValueGrid
              items={[
                ["Elapsed", <DurationDisplay milliseconds={value.duration.value_ms} />],
                [
                  "Agent system",
                  [
                    value.agent_system.name,
                    value.agent_system.backend,
                    value.agent_system.model,
                  ]
                    .filter(Boolean)
                    .join(" · "),
                ],
                [
                  "Running cost",
                  <CostDisplay
                    value={value.cost.value}
                    currency={value.cost.currency}
                    accountingStatus={value.cost.accounting_status}
                  />,
                ],
              ]}
            />
            {cancel && (
              <SecondaryAction
                type="button"
                disabled={pendingAction !== null}
                onClick={() => void runAction(cancel)}
              >
                {pendingAction === "cancel" ? "Cancelling…" : cancel.label}
              </SecondaryAction>
            )}
            {actionError && (
              <p className="v-danger" role="alert">
                {actionError}
              </p>
            )}
          </div>
        </Panel>
        <EvidenceDisclosure summary="Evidence">
          <p>Raw controller events remain in the recorded evidence for this run.</p>
          {value.evidence_links.map((link) => (
            <a key={link.href} href={link.href}>
              {link.label}
            </a>
          ))}
        </EvidenceDisclosure>
      </section>
    );
  }

  const primary = value.available_actions.find(
    (action) => action.id !== "review_evidence",
  );
  return (
    <section className="console-stack run-result" data-testid="run-presentation">
      <ResultVerdict
        status={value.final_verdict ?? "unknown"}
        label={value.final_verdict}
        detail={value.verdict_reason}
      />
      <p className="v-muted">{value.target_repository.statement}</p>
      <Panel>
        <PanelHeader title="WHAT CHANGED" />
        <div className="v-panel__body">
          <p>{value.change_summary}</p>
        </div>
      </Panel>
      <Panel>
        <PanelHeader title="FILES CHANGED" meta={value.changed_files.length} />
        <div className="v-panel__body">
          {value.changed_files.length ? (
            <ul className="console-list">
              {value.changed_files.map((file) => (
                <li key={file}>{file}</li>
              ))}
            </ul>
          ) : (
            <p>No file changes were recorded.</p>
          )}
        </div>
      </Panel>
      <Panel>
        <PanelHeader
          title="CHECKS AND TESTS"
          meta={value.checks_summary.accounting_status}
        />
        <KeyValueGrid
          items={[
            ["Passed", value.checks_summary.passed ?? "Unknown"],
            ["Failed", value.checks_summary.failed ?? "Unknown"],
            ["Not run", value.checks_summary.not_run ?? "Unknown"],
            ["Unavailable", value.checks_summary.unavailable ?? "Unknown"],
          ]}
        />
      </Panel>
      <Panel>
        <PanelHeader
          title="REQUIREMENT COVERAGE"
          meta={value.requirement_summary.accounting_status}
        />
        <KeyValueGrid
          items={[
            ["Proved", value.requirement_summary.proved ?? "Unknown"],
            ["Not proved", value.requirement_summary.not_proved ?? "Unknown"],
          ]}
        />
      </Panel>
      <Panel>
        <PanelHeader title="KNOWN COST" meta={value.cost.accounting_status} />
        <div className="v-panel__body">
          <CostDisplay
            value={value.cost.value}
            currency={value.cost.currency}
            accountingStatus={value.cost.accounting_status}
          />
        </div>
      </Panel>
      <Panel>
        <PanelHeader title="ELAPSED TIME" meta={value.duration.accounting_status} />
        <div className="v-panel__body">
          <DurationDisplay milliseconds={value.duration.value_ms} />
        </div>
      </Panel>
      {primary && (
        <PrimaryAction
          type="button"
          disabled={pendingAction !== null}
          onClick={() => void runAction(primary)}
        >
          {pendingAction === primary.id ? "Working…" : primary.label}
        </PrimaryAction>
      )}
      {actionError && (
        <p className="v-danger" role="alert">
          {actionError}
        </p>
      )}
      <EvidenceDisclosure summary="Evidence">
        <ul className="console-list">
          {value.evidence_links.map((link) => (
            <li key={link.href}>
              <a href={link.href}>{link.label}</a>
            </li>
          ))}
        </ul>
        {!!value.technical_detail_references.length && (
          <>
            <strong>Technical details</strong>
            <ul className="console-list">
              {value.technical_detail_references.map((reference) => (
                <li key={reference}>
                  <code>{reference}</code>
                </li>
              ))}
            </ul>
          </>
        )}
      </EvidenceDisclosure>
    </section>
  );
}

export function SingleTaskPage({ client }: { client: ConsoleClient }) {
  const environment = useConsoleEnvironment();
  const initialDraft = useMemo(restoreDraft, []);
  const [options, setOptions] = useState<ConsoleRunOptions | null>(null);
  const [optionsError, setOptionsError] = useState<string | null>(null);
  const [repository, setRepository] = useState(initialDraft.repository);
  const [task, setTask] = useState(initialDraft.task);
  const [successCriteria, setSuccessCriteria] = useState(initialDraft.successCriteria);
  const [referenceText, setReferenceText] = useState(initialDraft.referenceText);
  const [manualValidation, setManualValidation] = useState(
    initialDraft.manualValidation,
  );
  const [attachments, setAttachments] = useState<File[]>([]);
  const [discovery, setDiscovery] = useState<ConsoleValidationDiscovery | null>(null);
  const [discoveryError, setDiscoveryError] = useState<string | null>(null);
  const [selectedSuggestion, setSelectedSuggestion] = useState("");
  const [validationConfirmed, setValidationConfirmed] = useState(false);
  const [deliveryMode, setDeliveryMode] = useState("approve");
  const [policyPreset, setPolicyPreset] = useState("performance");
  const [policySelection, setPolicySelection] = useState("configured");
  const [routingMode, setRoutingMode] = useState("observe");
  const [maxCost, setMaxCost] = useState("");
  const [maxWallTime, setMaxWallTime] = useState("");
  const [maxAttempts, setMaxAttempts] = useState("3");
  const [requiresFileChanges, setRequiresFileChanges] = useState(true);
  const [preview, setPreview] = useState<PolicyPreview | null>(null);
  const [previewPending, setPreviewPending] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submissionError, setSubmissionError] = useState<string | null>(null);
  const [submissionFailure, setSubmissionFailure] = useState<RunFailure | null>(null);
  const [runId, setRunId] = useState(
    () => new URLSearchParams(location.search).get("run") ?? "",
  );
  const [product, setProduct] = useState<ProductRun | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const draftFingerprint = JSON.stringify({
    repository,
    task,
    successCriteria,
    referenceText,
    manualValidation,
  });
  const previousFingerprint = useRef(draftFingerprint);

  useEffect(() => {
    const controller = new AbortController();
    void client
      .runOptions(controller.signal)
      .then((value) => {
        setOptions(value);
        const requested = new URLSearchParams(location.search).get("repository");
        setRepository(
          (current) => current || requested || value.default_repository || "",
        );
        setMaxCost(
          value.defaults.max_cost === null ? "" : String(value.defaults.max_cost),
        );
        setMaxAttempts(String(value.defaults.max_attempts));
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted)
          setOptionsError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => controller.abort();
  }, [client]);

  useEffect(() => {
    try {
      localStorage.setItem(DRAFT_KEY, draftFingerprint);
    } catch {
      /* storage can be unavailable */
    }
    if (previousFingerprint.current !== draftFingerprint && !submitting) {
      try {
        sessionStorage.removeItem(SUBMISSION_KEY);
      } catch {
        /* storage can be unavailable */
      }
    }
    previousFingerprint.current = draftFingerprint;
  }, [draftFingerprint, submitting]);

  useEffect(() => {
    if (!repository) {
      setDiscovery(null);
      return;
    }
    const controller = new AbortController();
    setDiscoveryError(null);
    void client
      .discoverValidation(repository, controller.signal)
      .then((value) => {
        setDiscovery(value);
        setSelectedSuggestion(value.selected_suggestion_id ?? "");
        setValidationConfirmed(false);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted)
          setDiscoveryError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => controller.abort();
  }, [client, repository]);

  useEffect(() => {
    if (!runId) return;
    const controller = new AbortController();
    const observe = async () => {
      let sequence = 0;
      try {
        let current = await client.runStatus(runId, controller.signal);
        while (!controller.signal.aborted) {
          setProduct(current);
          setStatusError(null);
          sequence = current.last_event_sequence;
          if (current.final_verdict !== null) return;
          current = await client.runEvents(runId, sequence, controller.signal);
        }
      } catch (reason) {
        if (!controller.signal.aborted)
          setStatusError(reason instanceof Error ? reason.message : String(reason));
      }
    };
    void observe();
    return () => controller.abort();
  }, [client, runId]);

  const repositoryStatus =
    options?.repositories.find((item) => item.path === repository) ??
    discovery?.repository;
  const suggestion = discovery?.suggestions.find(
    (item) => item.suggestion_id === selectedSuggestion,
  );
  const lowConfidence = !manualValidation && suggestion?.requires_confirmation === true;
  const formReady =
    environment.setup.valid &&
    !!repository &&
    !!task.trim() &&
    repositoryStatus?.dirty !== true &&
    repositoryStatus?.valid !== false &&
    (!lowConfidence || validationConfirmed) &&
    !submitting;

  const chooseAttachments = (event: ChangeEvent<HTMLInputElement>) => {
    setAttachments(Array.from(event.target.files ?? []));
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!formReady) return;
    setSubmitting(true);
    setSubmissionError(null);
    setSubmissionFailure(null);
    try {
      const fingerprint = draftFingerprint;
      const previous = restoreSubmission(safeSessionGet(SUBMISSION_KEY));
      const submissionId =
        previous?.fingerprint === fingerprint && previous.id
          ? previous.id
          : crypto.randomUUID();
      sessionStorage.setItem(
        SUBMISSION_KEY,
        JSON.stringify({ fingerprint, id: submissionId }),
      );
      const values = await readAttachments(attachments);
      const result = await client.startRun({
        submission_id: submissionId,
        repository,
        task,
        success_criteria: successCriteria || undefined,
        reference_text: referenceText || undefined,
        attachments: values,
        validation_command: manualValidation || undefined,
        validation_argv: manualValidation ? undefined : suggestion?.argv,
        validation_confirmed: validationConfirmed,
        verification_required: true,
        delivery_mode: deliveryMode,
        policy_preset: policyPreset,
        policy_selection: policySelection,
        routing_mode: routingMode,
        max_cost: maxCost || undefined,
        max_wall_time: maxWallTime || undefined,
        max_attempts: maxAttempts,
        requires_file_changes: requiresFileChanges,
      });
      if (result.failure) setSubmissionFailure(result.failure);
      else if (result.run_id) {
        setRunId(result.run_id);
        history.replaceState(
          null,
          "",
          `/console?run=${encodeURIComponent(result.run_id)}`,
        );
        sessionStorage.removeItem(SUBMISSION_KEY);
        localStorage.removeItem(DRAFT_KEY);
      }
    } catch (reason) {
      setSubmissionError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSubmitting(false);
    }
  };

  const previewTask = async () => {
    if (!repository || !task.trim() || previewPending) return;
    setPreviewPending(true);
    try {
      setPreview(
        await client.previewPolicy({
          repository,
          task,
          success_criteria: successCriteria || task,
          preset: policyPreset,
        }),
      );
    } catch (reason) {
      setSubmissionError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setPreviewPending(false);
    }
  };

  if (runId) {
    return (
      <ProductShell
        surface="new-task"
        title={product?.final_verdict ? "Result" : "Task in progress"}
      >
        <div className="console-stack">
          {statusError && (
            <ErrorState
              title="Live status is temporarily unavailable"
              detail={`${statusError}. The server-side run continues; refresh to reconnect.`}
            />
          )}
          {!product && !statusError && (
            <LoadingState
              title="Reconnecting to the task"
              detail="The server-side run continues while this page reconnects."
            />
          )}
          {product && (
            <ProductResult value={product} client={client} onUpdate={setProduct} />
          )}
        </div>
      </ProductShell>
    );
  }

  return (
    <ProductShell
      surface="new-task"
      title="New task"
      status={optionsError ? "failed" : "running"}
      statusText={optionsError ? "Villani service is unavailable" : undefined}
    >
      <div className="console-stack">
        <PageIntro title="What would you like Villani to change?">
          Your repository stays unchanged while Villani works in isolation. A proved
          result waits for your delivery decision.
        </PageIntro>
        {optionsError && (
          <>
            <ErrorState title="Villani Service is unavailable" detail={optionsError} />
            <Panel>
              <PanelHeader title="RECOVERY" />
              <div className="v-panel__body console-stack">
                <p>No run was started. The target repository was not modified.</p>
                <p>
                  <strong>Next:</strong> Run <code>villani service start</code>, then
                  retry.
                </p>
              </div>
            </Panel>
          </>
        )}
        <TaskComposerShell
          title="New task"
          meta="Verification required · no default time limit"
        >
          <form className="run-form" onSubmit={submit} noValidate>
            <FormField
              className="run-form--wide"
              id="task-repository"
              label="Repository"
              required
              error={
                repositoryStatus?.dirty
                  ? "Commit or stash existing changes before starting a task."
                  : repository && repositoryStatus?.valid === false
                    ? "Choose a valid Git repository."
                    : undefined
              }
            >
              <input
                className="v-input"
                list="villani-repositories"
                value={repository}
                onChange={(event) => setRepository(event.target.value)}
                placeholder="Select a Git repository"
                required
              />
            </FormField>
            <datalist id="villani-repositories">
              {options?.repositories.map((item) => (
                <option value={item.path} key={item.path}>
                  {item.name}
                </option>
              ))}
            </datalist>
            <FormField
              className="run-form--wide"
              id="task-instruction"
              label="Task"
              required
            >
              <textarea
                className="v-textarea run-textarea"
                value={task}
                onChange={(event) => setTask(event.target.value)}
                placeholder="What should Villani change?"
                required
              />
            </FormField>
            <details className="run-advanced run-form--wide task-settings">
              <summary>Details (optional)</summary>
              <div className="run-form run-form--nested">
                <FormField
                  className="run-form--wide"
                  id="task-success-criteria"
                  label="Success criteria (optional)"
                >
                  <textarea
                    className="v-textarea"
                    value={successCriteria}
                    onChange={(event) => setSuccessCriteria(event.target.value)}
                  />
                </FormField>
                <FormField
                  className="run-form--wide"
                  id="task-reference"
                  label="Issue or reference text"
                >
                  <textarea
                    className="v-textarea"
                    value={referenceText}
                    onChange={(event) => setReferenceText(event.target.value)}
                  />
                </FormField>
                <FormField
                  className="run-form--wide"
                  id="task-attachments"
                  label="Text attachments"
                  help="Up to 100 KB combined. Contents become local task context and recorded evidence."
                >
                  <input
                    className="v-input"
                    type="file"
                    multiple
                    accept="text/*,.md,.json,.yaml,.yml,.toml,.xml,.csv"
                    onChange={chooseAttachments}
                  />
                </FormField>
                <fieldset className="run-validation run-form--wide">
                  <legend>Verification</legend>
                  {discoveryError && (
                    <p className="v-danger" role="alert">
                      {discoveryError}
                    </p>
                  )}
                  {suggestion ? (
                    <p>
                      Detected check: <code>{suggestion.display_command}</code>
                    </p>
                  ) : (
                    <p>
                      No repository check was detected. The task may run, but the result
                      cannot be ready to apply without sufficient alternative evidence.
                    </p>
                  )}
                  {discovery?.failure && <FailureDetails failure={discovery.failure} />}
                  {lowConfidence && (
                    <label className="run-checkbox">
                      <input
                        type="checkbox"
                        checked={validationConfirmed}
                        onChange={(event) =>
                          setValidationConfirmed(event.target.checked)
                        }
                      />
                      Villani is not sure this is the right check. Use{" "}
                      <code>{suggestion?.display_command}</code>?
                    </label>
                  )}
                  <FormField id="manual-validation" label="Manual check override">
                    <input
                      className="v-input"
                      value={manualValidation}
                      onChange={(event) => setManualValidation(event.target.value)}
                    />
                  </FormField>
                </fieldset>
                <label className="v-field">
                  <span className="v-field__label">Delivery mode</span>
                  <select
                    className="v-select"
                    value={deliveryMode}
                    onChange={(event) => setDeliveryMode(event.target.value)}
                  >
                    {options?.delivery_modes.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="v-field">
                  <span className="v-field__label">Agent mode</span>
                  <select
                    className="v-select"
                    value={policyPreset}
                    onChange={(event) => setPolicyPreset(event.target.value)}
                  >
                    {options?.policy_presets.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="v-field">
                  <span className="v-field__label">Cost limit (optional, USD)</span>
                  <input
                    className="v-input"
                    type="number"
                    min="0"
                    step="0.01"
                    value={maxCost}
                    onChange={(event) => setMaxCost(event.target.value)}
                  />
                </label>
                <label className="v-field">
                  <span className="v-field__label">Time limit (optional, seconds)</span>
                  <input
                    className="v-input"
                    type="number"
                    min="0"
                    step="1"
                    value={maxWallTime}
                    onChange={(event) => setMaxWallTime(event.target.value)}
                  />
                </label>
                <label className="v-field">
                  <span className="v-field__label">Maximum attempts</span>
                  <input
                    className="v-input"
                    type="number"
                    min="1"
                    step="1"
                    value={maxAttempts}
                    onChange={(event) => setMaxAttempts(event.target.value)}
                  />
                </label>
                <label className="v-field">
                  <span className="v-field__label">Advanced policy source</span>
                  <select
                    className="v-select"
                    value={policySelection}
                    onChange={(event) => setPolicySelection(event.target.value)}
                  >
                    {options?.advanced_policies.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="v-field">
                  <span className="v-field__label">Advanced routing mode</span>
                  <select
                    className="v-select"
                    value={routingMode}
                    onChange={(event) => setRoutingMode(event.target.value)}
                  >
                    {options?.routing_modes.map((item) => (
                      <option key={item} value={item}>
                        {item}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="run-checkbox">
                  <input
                    type="checkbox"
                    checked={requiresFileChanges}
                    onChange={(event) => setRequiresFileChanges(event.target.checked)}
                  />
                  Require a file change
                </label>
                <SecondaryAction
                  type="button"
                  disabled={!repository || !task.trim() || previewPending}
                  onClick={() => void previewTask()}
                >
                  {previewPending ? "Assessing…" : "Preview task assessment"}
                </SecondaryAction>
                {preview && (
                  <EvidenceDisclosure
                    className="run-form--wide"
                    summary="Advanced task assessment"
                  >
                    <section aria-label="Task assessment">
                      <KeyValueGrid
                        items={[
                          [
                            "Selected agent system",
                            `${preview.selected_coding_route.backend ?? "None"} / ${preview.selected_coding_route.model ?? "None"}`,
                          ],
                          [
                            "Verification",
                            preview.selected_verifier_route.selected
                              ? "Available"
                              : "Unavailable",
                          ],
                          [
                            "Estimated cost",
                            preview.estimated_cost.value === null
                              ? `Unknown (${preview.estimated_cost.status})`
                              : String(preview.estimated_cost.value),
                          ],
                        ]}
                      />
                    </section>
                  </EvidenceDisclosure>
                )}
              </div>
            </details>
            <PrimaryAction className="run-submit" type="submit" disabled={!formReady}>
              {submitting ? "Starting…" : "Run safely"}
            </PrimaryAction>
          </form>
        </TaskComposerShell>
        {submissionError && (
          <ErrorState title="Task could not be started" detail={submissionError} />
        )}
        {submissionFailure && (
          <Panel>
            <PanelHeader title="TASK COULD NOT START" />
            <div className="v-panel__body">
              <FailureDetails failure={submissionFailure} />
            </div>
          </Panel>
        )}
      </div>
    </ProductShell>
  );
}
