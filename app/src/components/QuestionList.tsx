import { useEffect, useRef } from "react";
import type { Question, ShowIf } from "../types";

export interface AnswerState {
  option_ids: string[];
  comment: string;
}

export type AnswerMap = Record<string, AnswerState>;

// Evaluate a show_if predicate against the chosen speciality and prior answers.
export function isVisible(q: Question, speciality: string, answers: AnswerMap): boolean {
  const cond: ShowIf = q.show_if;
  if (!cond) return true;
  if ("speciality" in cond) return cond.speciality === speciality;
  if ("answer" in cond) {
    const dep = answers[cond.answer.question];
    if (!dep) return false;
    return dep.option_ids.some((id) => cond.answer.any_of.includes(id));
  }
  return true;
}

export function visibleQuestions(
  questions: Question[],
  speciality: string,
  answers: AnswerMap,
): Question[] {
  return questions.filter((q) => isVisible(q, speciality, answers));
}

// Which required + visible questions still have no selection.
export function missingRequired(
  questions: Question[],
  speciality: string,
  answers: AnswerMap,
): Question[] {
  return visibleQuestions(questions, speciality, answers).filter(
    (q) => q.required && (answers[q.id]?.option_ids.length ?? 0) === 0,
  );
}

// One question at a time with a progress bar. Selecting a single-choice
// answer auto-advances (after a beat, so the selection is seen to register);
// multi-choice and skippable questions advance with the wizard's Continue.
// The parent owns the index so the wizard's Back/Continue can navigate too.
export default function QuestionStepper({
  questions,
  speciality,
  answers,
  onChange,
  index,
  onAdvance,
}: {
  questions: Question[];
  speciality: string;
  answers: AnswerMap;
  onChange: (next: AnswerMap) => void;
  index: number;
  onAdvance: () => void;
}) {
  const visible = visibleQuestions(questions, speciality, answers);
  const count = visible.length;
  const idx = Math.min(index, Math.max(0, count - 1));
  const q = visible[idx];
  const advanceTimer = useRef<number | null>(null);

  // A pending auto-advance must not survive a manual move (Previous during
  // the delay) or unmount.
  useEffect(() => {
    return () => {
      if (advanceTimer.current !== null) window.clearTimeout(advanceTimer.current);
    };
  }, [idx]);

  if (!q) return null;

  function toggle(question: Question, optionId: string) {
    const current = answers[question.id] ?? { option_ids: [], comment: "" };
    let option_ids: string[];
    if (question.type === "single") {
      option_ids = [optionId];
    } else {
      option_ids = current.option_ids.includes(optionId)
        ? current.option_ids.filter((id) => id !== optionId)
        : [...current.option_ids, optionId];
    }
    onChange({ ...answers, [question.id]: { ...current, option_ids } });
    if (question.type === "single" && idx < count - 1) {
      if (advanceTimer.current !== null) window.clearTimeout(advanceTimer.current);
      advanceTimer.current = window.setTimeout(onAdvance, 300);
    }
  }

  function setComment(question: Question, comment: string) {
    const current = answers[question.id] ?? { option_ids: [], comment: "" };
    onChange({ ...answers, [question.id]: { ...current, comment } });
  }

  const state = answers[q.id] ?? { option_ids: [], comment: "" };

  return (
    <div>
      <div className="q-progress">
        <div className="q-progress-track">
          <div
            className="q-progress-fill"
            style={{ width: `${((idx + 1) / count) * 100}%` }}
          />
        </div>
        <span className="q-progress-label">
          Question {idx + 1} of {count}
        </span>
      </div>
      <div className="question-block" key={q.id}>
        <div className="q-prompt">
          {q.prompt}
          {q.required ? (
            <span className="req-star">*</span>
          ) : (
            <span className="tiny faint" style={{ marginLeft: "0.4rem" }}>
              optional
            </span>
          )}
        </div>
        {q.options.map((opt) => {
          const selected = state.option_ids.includes(opt.id);
          return (
            <label key={opt.id} className={`option${selected ? " selected" : ""}`}>
              <input
                type={q.type === "single" ? "radio" : "checkbox"}
                name={q.id}
                checked={selected}
                onChange={() => toggle(q, opt.id)}
              />
              <span>{opt.label}</span>
            </label>
          );
        })}
        {q.allow_comment && (
          <textarea
            placeholder="Add context (optional)"
            value={state.comment}
            onChange={(e) => setComment(q, e.target.value)}
            style={{ minHeight: 60, marginTop: "0.4rem" }}
          />
        )}
        {(q.type === "multi" || !q.required) && (
          <p className="tiny faint" style={{ margin: "0.4rem 0 0" }}>
            {q.type === "multi"
              ? "Select all that apply, then Continue."
              : "Continue to skip this question."}
          </p>
        )}
      </div>
    </div>
  );
}
