import { useState } from "react";
import { projectsApi } from "../lib/endpoints";
import { useAuth } from "../lib/auth";
import { Alert, Spinner, formatCredits } from "./ui";
import type { Evaluation, FeasibilityVerdict } from "../types";

const VERDICT_COPY: Record<FeasibilityVerdict, { kind: "success" | "warn" | "info" | "error"; title: string }> = {
  pass: { kind: "success", title: "Feasible - ready to submit" },
  review_required: { kind: "warn", title: "Manual review required" },
  needs_info: { kind: "info", title: "More information needed" },
  reject: { kind: "error", title: "Not feasible" },
};

export function EvaluationScreen({
  evaluation,
  error,
  projectId,
  onRevise,
  onRetry,
  onSubmitted,
}: {
  evaluation: Evaluation | null;
  error: string | null;
  projectId: string | null;
  onRevise: () => void;
  onRetry: () => void;
  onSubmitted: (id: string) => void;
}) {
  const { settings } = useAuth();
  const consultant = settings?.consultant_first_name ?? "Consultant";
  const [submitting, setSubmitting] = useState(false);
  const [submitErr, setSubmitErr] = useState<string | null>(null);

  if (error) {
    return (
      <div className="card">
        <Alert kind="error">{error}</Alert>
        <button className="btn" onClick={onRetry}>
          Retry
        </button>
      </div>
    );
  }

  if (!evaluation || evaluation.state === "pending") {
    return (
      <div className="card">
        <div className="loading-center">
          <Spinner /> Moderating, checking feasibility, and estimating cost…
        </div>
        <p className="muted small center">This usually takes a moment.</p>
      </div>
    );
  }

  if (evaluation.state === "failed") {
    return (
      <div className="card">
        <Alert kind="error">Evaluation failed. Please try again.</Alert>
        <button className="btn" onClick={onRetry}>
          Retry evaluation
        </button>
      </div>
    );
  }

  const feasibility = evaluation.feasibility;
  const verdict = feasibility?.verdict;
  const meta = verdict ? VERDICT_COPY[verdict] : null;
  const canSubmit = verdict === "pass" || verdict === "review_required";

  async function submit() {
    if (!projectId) return;
    setSubmitting(true);
    setSubmitErr(null);
    try {
      await projectsApi.submit(projectId);
      onSubmitted(projectId);
    } catch (err) {
      setSubmitErr(err instanceof Error ? err.message : "Submit failed.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div>
      {/* Moderation */}
      {evaluation.moderation && (
        <div className="card">
          <div className="section-title">Moderation</div>
          {evaluation.moderation.verdict && (
            <p style={{ margin: 0 }} className="small">
              Verdict: <strong>{evaluation.moderation.verdict}</strong>
            </p>
          )}
          {evaluation.moderation.reasons && evaluation.moderation.reasons.length > 0 && (
            <ul className="muted small">
              {evaluation.moderation.reasons.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* Feasibility */}
      {feasibility && meta && (
        <div className="card">
          <div className="section-title">Feasibility</div>
          <Alert kind={meta.kind}>{meta.title}</Alert>
          {feasibility.reasons.length > 0 && (
            <ul className="muted small">
              {feasibility.reasons.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          )}
          {verdict === "review_required" && (
            <p className="tiny faint">
              Automatic development stays blocked until an admin authorizes this project.
            </p>
          )}
        </div>
      )}

      {/* Estimate */}
      {evaluation.estimate && evaluation.estimate.credits != null && (
        <div className="card">
          <div className="section-title">Cost estimate</div>
          <div className="between">
            <div>
              <div style={{ fontSize: "1.8rem" }} className="grad-text">
                {formatCredits(evaluation.estimate.credits)} credits
              </div>
              <div className="tiny faint">Prepaid, consumed as work happens.</div>
            </div>
            {evaluation.estimate.tokens != null && (
              <div className="tiny faint" style={{ textAlign: "right" }}>
                ~{evaluation.estimate.tokens.toLocaleString()} tokens
                <br />@ {evaluation.estimate.cost_per_token} credits/token
              </div>
            )}
          </div>
          {evaluation.estimate.explanation && (
            <p className="muted small mt">{evaluation.estimate.explanation}</p>
          )}
        </div>
      )}

      {/* Direct-quote: no automated estimate, the consultant prices it (no charge) */}
      {evaluation.estimate && evaluation.estimate.credits == null && (
        <div className="card">
          <div className="section-title">Custom quote</div>
          <Alert kind="info">
            No charge to submit. {consultant} will review your project and send a tailored quote.
          </Alert>
          {evaluation.estimate.explanation && (
            <p className="muted small">{evaluation.estimate.explanation}</p>
          )}
        </div>
      )}

      <div className="card">
        <Alert kind="info">
          After submission you may be asked for extra details or credentials about your
          infrastructure or remote platforms. {consultant} curates every project before any build starts.
        </Alert>
        {submitErr && <Alert kind="error">{submitErr}</Alert>}
        <div className="wizard-actions" style={{ marginTop: 0 }}>
          <button className="btn" onClick={onRevise} disabled={submitting}>
            {verdict === "reject" ? "Revise project" : "Go back & edit"}
          </button>
          {canSubmit && (
            <button className="btn btn-primary" onClick={submit} disabled={submitting}>
              {submitting ? <Spinner /> : "Submit for review"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
