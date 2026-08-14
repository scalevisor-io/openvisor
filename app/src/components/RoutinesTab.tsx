import { useEffect, useState } from "react";
import { Alert, Loading, Spinner, Toggle, relTime } from "./ui";
import { routinesApi } from "../lib/endpoints";
import { useToast } from "../lib/toast";
import type { Routine } from "../types";

// Cadences worth offering. The platform floor is hourly (a routine starts a real
// build), so nothing faster is listed - the field still accepts any valid cron.
const CRON_PRESETS: { label: string; cron: string }[] = [
  { label: "Every hour", cron: "0 * * * *" },
  { label: "Every day at 07:00", cron: "0 7 * * *" },
  { label: "Every Monday at 07:00", cron: "0 7 * * 1" },
  { label: "Monthly (1st, 07:00)", cron: "0 7 1 * *" },
];

type Draft = { title: string; prompt: string; schedule_cron: string };

const BLANK: Draft = { title: "", prompt: "", schedule_cron: "" };

// One form for both creating and editing, so the two can never drift into
// offering different fields - editing a routine is exactly re-stating it.
function RoutineForm({
  draft,
  setDraft,
  onSubmit,
  onCancel,
  submitting,
  submitLabel,
}: {
  draft: Draft;
  setDraft: (d: Draft) => void;
  onSubmit: () => void;
  onCancel: () => void;
  submitting: boolean;
  submitLabel: string;
}) {
  return (
    <div className="stack" style={{ gap: "0.75rem" }}>
      <div className="field">
        <label>Name</label>
        <input
          type="text"
          value={draft.title}
          placeholder="Weekly dependency audit"
          onChange={(e) => setDraft({ ...draft, title: e.target.value })}
        />
      </div>
      <div className="field">
        <label>Prompt - what the agent should do each time</label>
        <textarea
          rows={4}
          value={draft.prompt}
          placeholder="Check for outdated dependencies, upgrade the safe ones and open a pull request."
          onChange={(e) => setDraft({ ...draft, prompt: e.target.value })}
        />
      </div>
      <div className="field">
        <label>Schedule - leave empty to run it only when you ask</label>
        <div className="row gap-sm">
          <select
            value=""
            onChange={(e) => e.target.value && setDraft({ ...draft, schedule_cron: e.target.value })}
          >
            <option value="">Preset…</option>
            {CRON_PRESETS.map((p) => (
              <option key={p.cron} value={p.cron}>
                {p.label}
              </option>
            ))}
          </select>
          <input
            type="text"
            value={draft.schedule_cron}
            placeholder="0 7 * * 1"
            onChange={(e) => setDraft({ ...draft, schedule_cron: e.target.value })}
          />
        </div>
      </div>
      <div className="row gap-sm">
        <button className="btn btn-primary btn-sm" disabled={submitting} onClick={onSubmit}>
          {submitting ? <Spinner /> : submitLabel}
        </button>
        <button className="btn btn-sm" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}

// §routines: saved prompts on a project, optionally scheduled. Each firing
// creates an ordinary request - which is why a routine shows its last request's
// status rather than a run state of its own.
export default function RoutinesTab({
  projectId,
  readOnly,
}: {
  projectId: string;
  readOnly: boolean;
}) {
  const toast = useToast();
  const [routines, setRoutines] = useState<Routine[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>(BLANK);
  const [adding, setAdding] = useState(false);

  function load() {
    routinesApi
      .list(projectId)
      .then(setRoutines)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load."));
  }
  useEffect(load, [projectId]);

  async function act<T>(key: string, fn: () => Promise<T>, okMsg: string) {
    setBusy(key);
    try {
      await fn();
      toast.push(okMsg, "ok");
      load();
      return true;
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Action failed", "err");
      return false;
    } finally {
      setBusy(null);
    }
  }

  async function create() {
    if (!draft.title.trim() || !draft.prompt.trim()) {
      toast.push("A routine needs a name and a prompt", "err");
      return;
    }
    const ok = await act("new", () => routinesApi.create(projectId, draft), "Routine created");
    if (ok) {
      setDraft(BLANK);
      setAdding(false);
    }
  }

  if (error) return <Alert kind="error">{error}</Alert>;
  if (routines === null) return <Loading />;

  return (
    <div className="stack" style={{ gap: "1rem" }}>
      <div className="card">
        <div className="between mb">
          <div>
            <h3 style={{ margin: 0 }}>Routines</h3>
            <p className="tiny faint" style={{ margin: "0.25rem 0 0" }}>
              A saved prompt you can run on demand, or on a schedule. Each run becomes a
              request in the Requests tab and builds like any other.
            </p>
          </div>
          {!readOnly && !adding && (
            <button className="btn btn-primary btn-sm" onClick={() => setAdding(true)}>
              New routine
            </button>
          )}
        </div>

        {adding && (
          <RoutineForm
            draft={draft}
            setDraft={setDraft}
            onSubmit={create}
            onCancel={() => {
              setAdding(false);
              setDraft(BLANK);
            }}
            submitting={busy === "new"}
            submitLabel="Create routine"
          />
        )}

        {routines.length === 0 && !adding && (
          <p className="faint" style={{ margin: 0 }}>
            No routines yet.
          </p>
        )}
      </div>

      {routines.map((r) => (
        <RoutineCard
          key={r.id}
          routine={r}
          projectId={projectId}
          readOnly={readOnly}
          busy={busy}
          act={act}
        />
      ))}
    </div>
  );
}

function RoutineCard({
  routine,
  projectId,
  readOnly,
  busy,
  act,
}: {
  routine: Routine;
  projectId: string;
  readOnly: boolean;
  busy: string | null;
  act: <T>(key: string, fn: () => Promise<T>, okMsg: string) => Promise<boolean>;
}) {
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Draft>({
    title: routine.title,
    prompt: routine.prompt,
    schedule_cron: routine.schedule_cron,
  });

  // The previous request being open is exactly what stops the next firing, so
  // say so rather than leaving the customer wondering why nothing ran.
  const openRun =
    routine.last_request_status != null &&
    ["open", "quoted", "in_progress"].includes(routine.last_request_status);

  function startEditing() {
    // Re-seed from the row every time, so cancelling an edit and reopening it
    // never resurrects the abandoned draft.
    setDraft({
      title: routine.title,
      prompt: routine.prompt,
      schedule_cron: routine.schedule_cron,
    });
    setEditing(true);
  }

  async function save() {
    if (!draft.title.trim() || !draft.prompt.trim()) {
      toast.push("A routine needs a name and a prompt", "err");
      return;
    }
    const ok = await act(
      routine.id,
      () => routinesApi.update(projectId, routine.id, draft),
      "Routine saved",
    );
    if (ok) setEditing(false);
  }

  return (
    <div className="card">
      <div className="between mb">
        <div>
          <h4 style={{ margin: 0 }}>{routine.title}</h4>
          <p className="tiny faint" style={{ margin: "0.25rem 0 0" }}>
            {routine.schedule_cron ? (
              <>
                <code>{routine.schedule_cron}</code>
                {routine.next_run_at && routine.enabled && <> · next {relTime(routine.next_run_at)}</>}
              </>
            ) : (
              "Runs only when you ask"
            )}
            {routine.last_run_at && <> · last run {relTime(routine.last_run_at)}</>}
          </p>
        </div>
        {!readOnly && !editing && (
          <div className="row gap-sm" style={{ alignItems: "center" }}>
            <Toggle
              checked={routine.enabled}
              onChange={(v) =>
                act(routine.id, () => routinesApi.update(projectId, routine.id, { enabled: v }),
                  v ? "Routine resumed" : "Routine paused")
              }
            />
            <button className="btn btn-sm" disabled={busy === routine.id} onClick={startEditing}>
              Edit
            </button>
            <button
              className="btn btn-sm"
              disabled={busy === routine.id}
              onClick={() =>
                act(routine.id, () => routinesApi.run(projectId, routine.id), "Routine started")
              }
            >
              {busy === routine.id ? <Spinner /> : "Run now"}
            </button>
            <button
              className="btn btn-sm btn-danger"
              disabled={busy === routine.id}
              onClick={() =>
                act(routine.id, () => routinesApi.remove(projectId, routine.id), "Routine deleted")
              }
            >
              Delete
            </button>
          </div>
        )}
      </div>

      {editing ? (
        <RoutineForm
          draft={draft}
          setDraft={setDraft}
          onSubmit={save}
          onCancel={() => setEditing(false)}
          submitting={busy === routine.id}
          submitLabel="Save changes"
        />
      ) : (
        <>
          <p className="tiny" style={{ whiteSpace: "pre-wrap", margin: 0 }}>
            {routine.prompt}
          </p>
          {openRun && (
            <p className="tiny faint" style={{ margin: "0.5rem 0 0" }}>
              Its last request is still open - the next scheduled run waits for it to close.
            </p>
          )}
          {routine.last_skip_reason && !openRun && (
            <p className="tiny faint" style={{ margin: "0.5rem 0 0" }}>
              Last skipped: {routine.last_skip_reason}
            </p>
          )}
        </>
      )}
    </div>
  );
}
