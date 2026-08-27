import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { projectsApi } from "../lib/endpoints";
import { loadQuestions, loadSpecialities } from "../lib/meta";
import { useAuth } from "../lib/auth";
import { useToast } from "../lib/toast";
import { Alert, Loading, Spinner, Toggle } from "../components/ui";
import QuestionStepper, {
  missingRequired,
  visibleQuestions,
  type AnswerMap,
} from "../components/QuestionList";
import { EvaluationScreen } from "../components/EvaluationScreen";
import ResendVerify from "../components/ResendVerify";
import { ApiError } from "../lib/api";
import type { Answer, Evaluation, Question, Speciality, WizardProjectKind } from "../types";

const DESC_LIMIT = 40000;

// The wizard is a list of named steps that depends on the engagement kind:
// the curated-AI kinds pick a speciality (it drives the questions, the
// sovereign default and the harness) on a step of its own; a direct quote has
// none; chat is just the kind and the opening message.
type StepId =
  | "engagement" | "speciality" | "description" | "sources" | "questions" | "sovereignty" | "review";
const STEP_LABELS: Record<StepId, string> = {
  engagement: "Engagement",
  speciality: "Speciality",
  description: "Description",
  sources: "Sources",
  questions: "Questions",
  sovereignty: "Sovereignty",
  review: "Review",
};
const FULL_STEPS: StepId[] = [
  "engagement", "speciality", "description", "sources", "questions", "sovereignty", "review",
];

function stepsFor(kind: WizardProjectKind | null): StepId[] {
  if (kind === "chat") return ["engagement", "description"];
  if (kind === "ai" || kind === "auto_dev") return FULL_STEPS;
  return FULL_STEPS.filter((s) => s !== "speciality");
}

export default function NewProject() {
  const navigate = useNavigate();
  const toast = useToast();
  const { config, isAdmin, settings } = useAuth();
  const consultant = settings?.consultant_first_name ?? "Consultant";
  const [searchParams] = useSearchParams();
  const kindParam = searchParams.get("kind");

  const [specialities, setSpecialities] = useState<Speciality[]>([]);
  const [questions, setQuestions] = useState<Question[]>([]);
  const [loadErr, setLoadErr] = useState<string | null>(null);

  const [step, setStep] = useState(0);

  // form state
  const [kind, setKind] = useState<WizardProjectKind | null>(
    kindParam === "ai" || kindParam === "direct_quote" || kindParam === "auto_dev" ||
    kindParam === "chat"
      ? kindParam
      : null,
  );
  const [specialityId, setSpecialityId] = useState<string>("");
  const [description, setDescription] = useState("");
  const [fromScratch, setFromScratch] = useState(true);
  const [repos, setRepos] = useState<string[]>([""]);
  const [answers, setAnswers] = useState<AnswerMap>({});
  // Which question the Questions step is showing (one at a time).
  const [qIdx, setQIdx] = useState(0);
  const [watchLabels, setWatchLabels] = useState("");
  const [watchAssignees, setWatchAssignees] = useState("");
  const [watchAuthors, setWatchAuthors] = useState("");
  const [sovereign, setSovereign] = useState(false);
  const [sovereignComment, setSovereignComment] = useState("");

  // creation / evaluation state
  const [projectId, setProjectId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [evaluation, setEvaluation] = useState<Evaluation | null>(null);
  const [evalError, setEvalError] = useState<string | null>(null);
  const [needsVerify, setNeedsVerify] = useState(false);

  const speciality = useMemo(
    () => specialities.find((s) => s.id === specialityId) ?? null,
    [specialities, specialityId],
  );

  useEffect(() => {
    Promise.all([loadSpecialities(), loadQuestions()])
      .then(([s, q]) => {
        setSpecialities(s.filter((x) => x.enabled));
        setQuestions(q.questions);
      })
      .catch((err) => setLoadErr(err instanceof Error ? err.message : "Failed to load onboarding."));
  }, []);

  // When speciality changes, pre-set sovereign default and repo mode.
  function chooseSpeciality(s: Speciality) {
    setSpecialityId(s.id);
    setSovereign(s.sovereign_default);
    if (s.requires_existing_repo) {
      setFromScratch(false);
    }
  }

  const cleanRepos = repos.map((r) => r.trim()).filter(Boolean);
  const missing = missingRequired(questions, specialityId, answers);

  const isDirect = kind === "direct_quote";
  const isAuto = kind === "auto_dev";
  const isChat = kind === "chat";
  const csv = (v: string) => v.split(",").map((x) => x.trim()).filter(Boolean);
  const chatFee = config?.chat_upfront_credits ?? 10;

  // Admin can pause new deposits per kind (Admin settings). Admins are exempt.
  const pauseAi = !isAdmin && !!config?.pause_ai_deposits;
  const pauseDirect = !isAdmin && !!config?.pause_direct_deposits;
  const pauseAutoDev = !isAdmin && !!config?.pause_auto_dev_deposits;
  const pauseChat = !isAdmin && (config?.pause_chat_deposits ?? true);
  const bothPaused = pauseAi && pauseDirect;
  const kindPaused = (k: WizardProjectKind | null) =>
    (k === "ai" && pauseAi) || (k === "direct_quote" && pauseDirect) ||
    (k === "auto_dev" && pauseAutoDev) || (k === "chat" && pauseChat);
  const PAUSED_MSG = "We're not accepting this kind of project right now.";

  const steps = stepsFor(kind);
  const stepId: StepId = steps[Math.min(step, steps.length - 1)];
  const stepLabel = (id: StepId) =>
    id === "description" && isChat ? "Message" : STEP_LABELS[id];
  // The last input before the project is created: sovereignty on the build
  // paths, the opening message on chat.
  const isFinalInput = isChat ? stepId === "description" : stepId === "sovereignty";

  // Clear a deep-linked (?kind=) preselection that is currently paused so the
  // customer can't advance past step 1 with it.
  useEffect(() => {
    if (isAdmin || !config || stepId !== "engagement") return;
    if (kindPaused(kind)) setKind(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config, isAdmin, stepId, kind]);

  // Questions are shown one by one; the wizard's Back/Continue walk them.
  const visibleQs = visibleQuestions(questions, specialityId, answers);
  const qCount = visibleQs.length;
  const qIdxClamped = Math.min(qIdx, Math.max(0, qCount - 1));
  const onLastQuestion = qIdxClamped >= qCount - 1;

  function canAdvance(): string | null {
    if (stepId === "engagement") {
      if (!kind) return "Choose how you'd like to work with me.";
      if (kindPaused(kind)) return PAUSED_MSG;
    }
    if (stepId === "speciality" && !specialityId) return "Select a speciality.";
    if (stepId === "description") {
      if (!description.trim())
        return isAuto ? "Describe how you want me to develop."
          : isChat ? "Write your opening message."
            : "Describe what you want to build.";
      if (description.length > DESC_LIMIT) return "Description exceeds the character limit.";
    }
    if (stepId === "sources" && !isDirect) {
      if (isAuto) {
        if (cleanRepos.length === 0) return "Add the repository whose issues I should watch.";
        if (csv(watchLabels).length === 0 && csv(watchAssignees).length === 0)
          return "Set at least one label or assignee to watch.";
      } else if (!fromScratch && cleanRepos.length === 0) {
        return "Add at least one repository SSH URI, or choose from scratch.";
      }
    }
    if (stepId === "questions") {
      if (missing.length > 0) return "Answer the required questions.";
    }
    return null;
  }

  const advanceBlock = canAdvance();

  function next() {
    // Inside the Questions step, Continue moves one question at a time; only
    // the current question is validated (later ones haven't been seen yet).
    if (stepId === "questions" && !onLastQuestion) {
      const q = visibleQs[qIdxClamped];
      if (q?.required && (answers[q.id]?.option_ids.length ?? 0) === 0) {
        toast.push("Answer this question to continue.", "err");
        return;
      }
      setQIdx(qIdxClamped + 1);
      return;
    }
    if (advanceBlock) {
      toast.push(advanceBlock, "err");
      return;
    }
    if (isFinalInput) {
      void createAndEvaluate();
      return;
    }
    setStep((s) => s + 1);
  }

  function back() {
    if (stepId === "questions" && qIdxClamped > 0) {
      setQIdx(qIdxClamped - 1);
      return;
    }
    setStep((s) => Math.max(0, s - 1));
  }

  function buildAnswerPayload(): Answer[] {
    const visible = visibleQuestions(questions, specialityId, answers);
    const out: Answer[] = [];
    for (const q of visible) {
      const a = answers[q.id];
      if (!a) continue;
      if (a.option_ids.length === 0 && !a.comment.trim()) continue;
      const answer: Answer = { question_id: q.id, option_ids: a.option_ids };
      if (a.comment.trim()) answer.comment = a.comment.trim();
      out.push(answer);
    }
    return out;
  }

  async function createAndEvaluate() {
    if (kindPaused(kind)) {
      // defense in depth - the backend also 403s "deposits_paused"
      toast.push(PAUSED_MSG, "err");
      return;
    }
    setBusy(true);
    setEvalError(null);
    setNeedsVerify(false);
    try {
      let id = projectId;
      if (!id) {
        const project = await projectsApi.create({
          kind: kind ?? "ai",
          speciality: kind === "ai" || isAuto ? specialityId : specialityId || null,
          description: description.trim(),
          from_scratch: isDirect ? true : isAuto ? false : fromScratch,
          sovereign,
          sovereign_comment: sovereignComment.trim() || undefined,
          repos:
            isDirect || (fromScratch && !isAuto)
              ? undefined
              : cleanRepos.map((ssh_uri) => ({ ssh_uri })),
          issue_watch: isAuto
            ? { labels: csv(watchLabels), assignees: csv(watchAssignees), authors: csv(watchAuthors) }
            : undefined,
        });
        id = project.id;
        setProjectId(id);
      }
      if (isChat) {
        // chat skips evaluation entirely - the opening message is already posted
        // and the first answer is on its way.
        toast.push("Chat opened - the first answer is on its way.", "ok");
        navigate(`/projects/${id}`);
        return;
      }
      await projectsApi.saveAnswers(id, buildAnswerPayload());
      if (isAuto) {
        // auto_dev skips evaluation entirely - the sentinel is born watching.
        toast.push("Auto-developer created - watching for matching issues.", "ok");
        navigate(`/projects/${id}`);
        return;
      }
      await projectsApi.evaluate(id);
      setStep(steps.indexOf("review"));
      setEvaluation({ state: "pending" });
    } catch (err) {
      if (err instanceof ApiError && err.status === 403 && err.detail === "email_not_verified") {
        setNeedsVerify(true);
      } else if (err instanceof ApiError && err.status === 403 && err.detail === "deposits_paused") {
        if (isChat) toast.push(PAUSED_MSG, "err");
        else setEvalError(PAUSED_MSG);
      } else if (isChat) {
        // no EvaluationScreen on the chat path - errors (e.g. the 402 low-balance
        // refusal) surface as a toast and the wizard stays on the message step.
        toast.push(err instanceof Error ? err.message : "Could not open the chat.", "err");
      } else {
        setEvalError(err instanceof Error ? err.message : "Could not create the project.");
      }
    } finally {
      setBusy(false);
    }
  }

  // Poll evaluation until it resolves.
  useEffect(() => {
    if (stepId !== "review" || !projectId || !evaluation || evaluation.state !== "pending") return;
    let stop = false;
    const t = setInterval(async () => {
      try {
        const ev = await projectsApi.evaluation(projectId);
        if (stop) return;
        setEvaluation(ev);
      } catch (err) {
        if (stop) return;
        setEvalError(err instanceof Error ? err.message : "Evaluation failed.");
        clearInterval(t);
      }
    }, 2500);
    return () => {
      stop = true;
      clearInterval(t);
    };
  }, [stepId, projectId, evaluation]);

  if (loadErr) return <Alert kind="error">{loadErr}</Alert>;
  if (specialities.length === 0) return <Loading label="Loading onboarding…" />;

  if (needsVerify) {
    return (
      <div className="wizard">
        <h1>Verify your email first</h1>
        <Alert kind="warn">
          You need a verified email address before creating a project. Check your inbox for the
          verification link.
        </Alert>
        <ResendVerify />
      </div>
    );
  }

  return (
    <div className="wizard">
      <h1>New project</h1>
      <div className="steps">
        {steps.map((id, i) => (
          <div
            key={id}
            className={`step-pill ${i < step ? "done" : i === step ? "current" : ""}`}
            title={stepLabel(id)}
          />
        ))}
      </div>
      <p className="muted small" style={{ marginTop: "-0.75rem", marginBottom: "1.25rem" }}>
        Step {Math.min(step + 1, steps.length)} of {steps.length} - {stepLabel(stepId)}
      </p>

      {/* Engagement kind - every way to work, side by side */}
      {stepId === "engagement" && (
        <div className="card">
          <label>How would you like to work with me?</label>
          {(pauseAi || pauseDirect) && (
            <Alert kind="info">
              {bothPaused
                ? "We're not accepting any new projects for now. Please check back later."
                : pauseAi
                  ? "We're not accepting new AI-curated MVP projects for now - the direct-contact quote is still open."
                  : "We're not accepting new direct-contact quotes for now - the AI-curated MVP is still open."}
            </Alert>
          )}
          <div className="kind-grid">
            <button
              type="button"
              className={`select-card${kind === "ai" ? " selected" : ""}`}
              disabled={pauseAi}
              onClick={() => setKind("ai")}
            >
              <div className="between">
                <h3>Curated AI MVP</h3>
                <span className="chip chip-ai">Curated AI</span>
              </div>
              <div className="muted small">
                A senior-curated AI agent builds a working MVP fast - plugged into {consultant}'s
                experience knowledge base, senior structures (OCPA), CVE-aware, with a live demo on
                a subdomain. Pay-as-you-go credits.
              </div>
              {pauseAi && <div className="tiny faint mt">Paused - not accepting these right now.</div>}
            </button>
            <button
              type="button"
              className={`select-card${kind === "direct_quote" ? " selected" : ""}`}
              disabled={pauseDirect}
              onClick={() => {
                setKind("direct_quote");
                setSpecialityId("");
              }}
            >
              <div className="between">
                <h3>Direct contact quote</h3>
                <span className="chip chip-direct">Direct quote</span>
              </div>
              <div className="muted small">
                A custom-made engagement quoted by {consultant}. Takes a bit more time; best for heavier
                or more demanding work - air-gapped systems, infrastructure refactoring/migration,
                defence/regulated programs. No charge to submit; you get a tailored quote.
              </div>
              {pauseDirect && (
                <div className="tiny faint mt">Paused - not accepting these right now.</div>
              )}
            </button>
            <button
              type="button"
              className={`select-card${kind === "auto_dev" ? " selected" : ""}`}
              disabled={pauseAutoDev}
              onClick={() => setKind("auto_dev")}
            >
              <div className="between">
                <h3>Curated AI auto-developer</h3>
                <span className="chip chip-ai">Curated AI</span>
              </div>
              <div className="muted small">
                A sentinel on one of your GitHub/GitLab repositories: assign an issue or add a
                label and the {consultant}-curated agent builds it and opens a pull request,
                guided by your standing development policy. Usage-based credits per build - no
                upfront estimate.
              </div>
              {pauseAutoDev && (
                <div className="tiny faint mt">Paused - not accepting these right now.</div>
              )}
            </button>
            <button
              type="button"
              className={`select-card${kind === "chat" ? " selected" : ""}`}
              disabled={pauseChat}
              onClick={() => {
                setKind("chat");
                setSpecialityId("");
              }}
            >
              <div className="between">
                <h3>Just chat with me</h3>
                <span className="chip chip-ai">Curated AI</span>
              </div>
              <div className="muted small">
                A conversation, not a build: ask anything and get answers grounded in{" "}
                {consultant}'s experience knowledge base, with a human hand-off whenever you
                want it. {chatFee.toLocaleString()} credits to open, then usage-billed answers.
              </div>
              {pauseChat && (
                <div className="tiny faint mt">Paused - not accepting these right now.</div>
              )}
            </button>
          </div>
        </div>
      )}

      {/* Speciality (curated AI kinds) */}
      {stepId === "speciality" && (
        <div className="card">
          <label>Which speciality fits this project?</label>
          <p className="muted small mb">
            The speciality picks the harness and the knowledge the agent builds with, the
            questions that follow and the sovereignty default. Change it later from the project
            page if the scope moves.
          </p>
          <div className="stack">
            {specialities.map((s) => (
              <button
                key={s.id}
                type="button"
                className={`select-card${specialityId === s.id ? " selected" : ""}`}
                onClick={() => chooseSpeciality(s)}
              >
                <div className="between">
                  <h3>{s.label}</h3>
                  <span className="badge">{s.complexity_baseline}</span>
                </div>
                <div className="muted small">{s.description}</div>
                {s.requires_existing_repo && (
                  <div className="tiny faint mt">Acts on your existing repository.</div>
                )}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Description (the opening message on chat) */}
      {stepId === "description" && (
        <div className="card">
          <label htmlFor="desc">
            {isAuto
              ? "How do you want me to develop? Conventions, stack constraints, review expectations - this standing policy guides every build and stays editable anytime."
              : isChat
                ? "What would you like to talk about? This opens the conversation - you keep chatting from the project page."
                : "What do you want in the end? Add as much context as you can - more detail means a better estimate and build."}
          </label>
          <textarea
            id="desc"
            value={description}
            maxLength={DESC_LIMIT + 500}
            style={{ minHeight: 220 }}
            onChange={(e) => setDescription(e.target.value)}
          />
          <div className={`char-counter${description.length > DESC_LIMIT ? " over" : ""}`}>
            {description.length.toLocaleString()} / {DESC_LIMIT.toLocaleString()}
          </div>
          <p className="muted small" style={{ marginTop: "0.5rem" }}>
            {isAuto
              ? "A project title is generated from this policy - you can rename it later from the project page."
              : isChat
                ? `Opening the chat debits the ${chatFee.toLocaleString()}-credit fee and posts this as your first message.`
                : "A project title is generated from this description - you can rename it later from the project page."}
          </p>
        </div>
      )}

      {/* Sources - from scratch vs existing repos (AI only) */}
      {stepId === "sources" && isDirect && (
        <div className="card">
          <label>Existing systems</label>
          <Alert kind="info">
            No repository access is needed to request a quote. Describe any existing systems or
            repositories in the project description - {consultant} will discuss access and scope with you
            during the quote.
          </Alert>
        </div>
      )}
      {stepId === "sources" && !isDirect && (
        <div className="card">
          {!isAuto && <label>How should we start?</label>}
          {!isAuto && (
          <div className="stack">
            <button
              type="button"
              className={`select-card${fromScratch ? " selected" : ""}`}
              disabled={speciality?.requires_existing_repo}
              onClick={() => setFromScratch(true)}
            >
              <h3>From scratch</h3>
              <div className="muted small">We scaffold a fresh project following OCPA.</div>
            </button>
            <button
              type="button"
              className={`select-card${!fromScratch ? " selected" : ""}`}
              onClick={() => setFromScratch(false)}
            >
              <h3>Act on existing repository(ies)</h3>
              <div className="muted small">
                We work against repositories you already have.
              </div>
            </button>
          </div>
          )}

          {(isAuto || !fromScratch) && (
            <div className="mt-2">
              <Alert kind="info">
                A project SSH public key is generated when the project is created. You'll add it as a
                deploy key to your GitHub/GitLab so we can access these repositories (read-only).
              </Alert>
              <label>Repository SSH URIs</label>
              {repos.map((r, i) => (
                <div key={i} className="row" style={{ marginBottom: "0.5rem" }}>
                  <input
                    type="text"
                    placeholder="git@github.com:org/repo.git"
                    value={r}
                    onChange={(e) =>
                      setRepos((rs) => rs.map((x, j) => (j === i ? e.target.value : x)))
                    }
                  />
                  {repos.length > 1 && (
                    <button
                      type="button"
                      className="btn btn-sm btn-ghost"
                      onClick={() => setRepos((rs) => rs.filter((_, j) => j !== i))}
                    >
                      ✕
                    </button>
                  )}
                </div>
              ))}
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => setRepos((rs) => [...rs, ""])}
              >
                + Add another repository
              </button>
            </div>
          )}

          {isAuto && (
            <div className="mt-2">
              <label>What should trigger a build?</label>
              <p className="muted small">
                I watch the first repository's open issues (GitHub or GitLab). An issue matching any
                of these labels and/or assignees becomes a build - assigning and labeling need triage
                rights on your repo, so only your team can trigger me.
              </p>
              <input
                type="text"
                placeholder="Labels, comma-separated (e.g. ai, auto-dev)"
                value={watchLabels}
                onChange={(e) => setWatchLabels(e.target.value)}
                style={{ marginBottom: "0.5rem" }}
              />
              <input
                type="text"
                placeholder="Assignees, comma-separated usernames"
                value={watchAssignees}
                onChange={(e) => setWatchAssignees(e.target.value)}
                style={{ marginBottom: "0.5rem" }}
              />
              <input
                type="text"
                placeholder="Optional: only act on issues authored by (comma-separated usernames)"
                value={watchAuthors}
                onChange={(e) => setWatchAuthors(e.target.value)}
              />
            </div>
          )}
        </div>
      )}

      {/* Questions - MCQ, one question at a time */}
      {stepId === "questions" && (
        <div className="card">
          <p className="muted small mb">
            These answers scope the MVP and improve the estimate.
          </p>
          <QuestionStepper
            questions={questions}
            speciality={specialityId}
            answers={answers}
            onChange={setAnswers}
            index={qIdxClamped}
            onAdvance={() => setQIdx((i) => Math.min(i + 1, Math.max(0, qCount - 1)))}
          />
        </div>
      )}

      {/* Sovereignty toggle */}
      {stepId === "sovereignty" && (
        <div className="card">
          <div className="between">
            <div>
              <h3 style={{ margin: 0 }}>Sovereign technologies</h3>
              <p className="muted small" style={{ margin: "0.25rem 0 0" }}>
                Prefer sovereign, EU-based, government-certified platforms and open components.
                {speciality && (
                  <>
                    {" "}
                    Default for <strong>{speciality.short_label}</strong>:{" "}
                    {speciality.sovereign_default ? "on" : "off"}.
                  </>
                )}
              </p>
            </div>
            <Toggle checked={sovereign} onChange={setSovereign} />
          </div>
          <div className="field mt">
            <label htmlFor="sov-comment">Comment (optional)</label>
            <textarea
              id="sov-comment"
              value={sovereignComment}
              placeholder="Any constraints on hosting, residency, or components."
              onChange={(e) => setSovereignComment(e.target.value)}
            />
          </div>
        </div>
      )}

      {/* Review - creation + evaluation */}
      {stepId === "review" && (
        <EvaluationScreen
          evaluation={evaluation}
          error={evalError}
          projectId={projectId}
          onRevise={() => {
            setEvaluation(null);
            setEvalError(null);
            setStep(steps.indexOf("description"));
          }}
          onRetry={() => void createAndEvaluate()}
          onSubmitted={(id) => navigate(`/projects/${id}`)}
        />
      )}

      {stepId !== "review" && (
        <div className="wizard-actions">
          <button
            type="button"
            className="btn"
            disabled={step === 0 || busy}
            onClick={back}
          >
            Back
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={next}
            disabled={busy || (stepId === "engagement" && bothPaused)}
          >
            {busy ? <Spinner />
              : isChat && stepId === "description" ? `Open chat (${chatFee.toLocaleString()} credits)`
                : stepId === "sovereignty" ? "Create & evaluate" : "Continue"}
          </button>
        </div>
      )}
    </div>
  );
}
