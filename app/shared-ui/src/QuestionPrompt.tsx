import "./QuestionPrompt.css";
import type { MessageQuestionMeta } from "./types";

// The one-click answer panel under a §12 clarifying-question agent message,
// shared between the spoke SPA chat and the hub customer console. Pure
// presentational: the host renders the question text as the message body and
// owns posting the answer (an option's label, or free text through its own
// composer). While `active`, options are buttons; once answered they freeze,
// highlighting the picked one when the reply matched an option.
export function QuestionPrompt({
  question,
  active,
  selected = null,
  busy = false,
  onSelect,
  freeTextHint = "…or just type your own answer below.",
}: {
  question: MessageQuestionMeta;
  active: boolean;
  selected?: string | null;
  busy?: boolean;
  onSelect: (label: string) => void;
  freeTextHint?: string;
}) {
  return (
    <div className={`qp${active ? "" : " qp-done"}`}>
      <div className="qp-options" role={active ? "group" : undefined}>
        {question.options.map((o, i) => {
          const picked = selected != null && o.label === selected;
          return (
            <button
              key={`${i}-${o.label}`}
              type="button"
              className={`qp-option${picked ? " qp-picked" : ""}`}
              disabled={!active || busy}
              onClick={() => onSelect(o.label)}
            >
              <span className="qp-num">{i + 1}</span>
              <span className="qp-text">
                <strong className="qp-label">{o.label}</strong>
                {o.description && <span className="qp-desc">{o.description}</span>}
              </span>
            </button>
          );
        })}
      </div>
      {active && question.allow_free_text !== false && (
        <div className="qp-hint">{freeTextHint}</div>
      )}
    </div>
  );
}
