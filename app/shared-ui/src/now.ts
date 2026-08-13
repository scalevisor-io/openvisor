// The project page's spine (§project-page-now): one pure mapping from the
// project's machine state to the customer's three answers - what's happening,
// who holds the ball, what to do next. Both consoles render the result with
// NowPanel; every guard stays server-side (a host only wires the actions its
// API allows, and the panel renders only actions with a handler).
import type { ProjectStatus } from "./types";

export type SharedProjectKind = "ai" | "direct_quote" | "auto_dev" | "chat";

export type NowOwner = "you" | "consultant" | "agent" | "done" | "none";

export type NowActionId =
  | "evaluate"
  | "submit"
  | "fund"
  | "resume"
  | "stop"
  | "open-pr"
  | "approve"
  | "open-demo"
  | "require-review";

export interface NowAction {
  id: NowActionId;
  label: string;
}

export interface ProjectNow {
  headline: string;
  body?: string;
  owner: NowOwner;
  primary?: NowAction;
  secondary: NowAction[];
}

export interface ProjectNowInput {
  status: ProjectStatus | string;
  kind?: SharedProjectKind | string;
  devRunState?: string | null; // idle|running|deploying|awaiting_merge|merged|failed|done
  demoUrl?: string | null;
  evaluationState?: "pending" | "done" | "failed" | null;
  evaluationVerdict?: string | null; // feasibility verdict when done
  estimateCredits?: number | null;
  prNumber?: number | null;
  /** Consultant display name, e.g. "Consultant"; defaults to a neutral label. */
  consultant?: string;
  /**
   * §parallel-builds MR4 (additive): how many sibling runs are in flight -
   * `building` counts queued/running/deploying rows, `merging` the
   * awaiting_merge ones. With two or more in total the development headline
   * aggregates ("2 builds running · 1 pull request waiting"); absent or a
   * single run keeps every singular copy below untouched.
   */
  runCounts?: { building: number; merging: number } | null;
}

// The aggregate headline, or null when the singular copy should stand.
function aggregate(i: ProjectNowInput): ProjectNow | null {
  const c = i.runCounts;
  if (!c || c.building + c.merging < 2) return null;
  const parts: string[] = [];
  if (c.building > 0)
    parts.push(c.building === 1 ? "1 build running" : `${c.building} builds running`);
  if (c.merging > 0)
    parts.push(
      c.merging === 1 ? "1 pull request waiting" : `${c.merging} pull requests waiting`,
    );
  return {
    headline: `${parts.join(" · ")}.`,
    body:
      c.building > 0
        ? "Each build has its own console below - stop or follow them one by one."
        : "Merge the open pull requests on your repository and the demo redeploys by itself.",
    owner: c.building > 0 ? "agent" : "you",
    secondary: [],
  };
}

const A = (id: NowActionId, label: string): NowAction => ({ id, label });

const fmt = (n: number) =>
  n.toLocaleString("en-US", { maximumFractionDigits: 2 });

// The escalation is available in every state where the customer holds the ball
// or the machine is working; where the consultant already has it, it's noise.
const ESCALATABLE: ReadonlySet<string> = new Set([
  "draft",
  "payment_due",
  "development",
  "awaiting_customer",
]);

export function projectNow(i: ProjectNowInput): ProjectNow {
  const kind = (i.kind ?? "ai") as SharedProjectKind;
  const consultant = i.consultant || "your consultant";
  const dev = i.devRunState ?? "idle";
  const escalate = A("require-review", `Ask for ${consultant}'s review`);
  const withEscalation = (now: ProjectNow): ProjectNow =>
    kind !== "direct_quote" && ESCALATABLE.has(String(i.status))
      ? { ...now, secondary: [...now.secondary, escalate] }
      : now;

  if (i.status === "canceled") {
    return { headline: "This project is canceled.", owner: "none", secondary: [] };
  }
  if (i.status === "finished") {
    return {
      headline: kind === "chat" ? "This conversation is closed." : "Delivered and signed.",
      body:
        kind === "chat"
          ? undefined
          : `${consultant} signed this delivery - thank you for building here.`,
      owner: "done",
      secondary: i.demoUrl ? [A("open-demo", "Open the demo")] : [],
    };
  }

  if (kind === "chat") {
    if (i.status === "development") {
      return {
        headline: "Ask anything.",
        body: "Answers draw on the consultant's private knowledge and this project's Memory, billed per answer.",
        owner: "agent",
        secondary: [A("require-review", `Hand the thread to ${consultant}`)],
      };
    }
    return {
      headline: `${consultant} has the thread.`,
      body: "A human is reading - replies land in the chat.",
      owner: "consultant",
      secondary: [],
    };
  }

  if (kind === "direct_quote") {
    switch (i.status) {
      case "draft":
        return {
          headline: "Submit when ready.",
          body: `${consultant} scopes and prices this kind of engagement by hand - no automated build.`,
          owner: "you",
          primary: A("submit", "Submit for review"),
          secondary: [],
        };
      case "awaiting_review":
      case "awaiting_admin":
        return { headline: `${consultant} is preparing your quote.`, owner: "consultant", secondary: [] };
      case "payment_due":
        return {
          headline:
            i.estimateCredits != null
              ? `Quote ready - ${fmt(i.estimateCredits)} credits.`
              : "Quote ready.",
          body: "Fund the wallet to accept it and the work starts.",
          owner: "you",
          primary: A("fund", "Add credits"),
          secondary: [],
        };
      case "development":
        return { headline: `${consultant} is working on your project.`, owner: "consultant", secondary: [] };
      case "awaiting_customer":
        return {
          headline: "Your input is needed.",
          body: "See the latest message in the chat.",
          owner: "you",
          secondary: [],
        };
    }
  }

  // ai + auto_dev share the build machinery.
  switch (i.status) {
    case "draft": {
      if (kind === "auto_dev") {
        return {
          headline: "Finish the setup.",
          body: "Connect the repository to watch and set the issue filters below.",
          owner: "you",
          secondary: [],
        };
      }
      if (i.evaluationState === "pending") {
        return {
          headline: "Evaluating your description…",
          body: "Feasibility verdict and credit estimate land here in under a minute.",
          owner: "agent",
          secondary: [],
        };
      }
      if (i.evaluationState === "done") {
        const pass = (i.evaluationVerdict ?? "pass") === "pass";
        if (pass) {
          return withEscalation({
            headline:
              i.estimateCredits != null
                ? `Feasible - estimated ${fmt(i.estimateCredits)} credits.`
                : "Feasible.",
            body: "Submit it and the project moves to review.",
            owner: "you",
            primary: A("submit", "Submit project"),
            secondary: [A("evaluate", "Re-run evaluation")],
          });
        }
        return withEscalation({
          headline: "The evaluation flagged issues.",
          body: "Adjust the description below, then run it again.",
          owner: "you",
          primary: A("evaluate", "Re-run evaluation"),
          secondary: [],
        });
      }
      return withEscalation({
        headline: "Ready when you are.",
        body: "Run the evaluation to get a feasibility verdict and a price.",
        owner: "you",
        primary: A("evaluate", "Run evaluation"),
        secondary: [],
      });
    }
    case "awaiting_review":
      return {
        headline: `${consultant} is reviewing your project.`,
        body: "You'll get an email when it moves.",
        owner: "consultant",
        secondary: [],
      };
    case "payment_due":
      return withEscalation({
        headline:
          i.estimateCredits != null
            ? `Fund ${fmt(i.estimateCredits)} credits and the build starts on its own.`
            : "Fund the estimate and the build starts on its own.",
        owner: "you",
        primary: A("fund", "Add credits"),
        secondary: [],
      });
    case "development": {
      const agg = aggregate(i);
      if (agg) return withEscalation(agg);
      if (dev === "running" || dev === "deploying") {
        return withEscalation({
          headline:
            dev === "deploying" ? "Deploying the demo…" : "The agent is building.",
          body: "Every edit, test and token below is live.",
          owner: "agent",
          secondary: [A("stop", "Stop build")],
        });
      }
      if (dev === "awaiting_merge") {
        return withEscalation({
          headline:
            i.prNumber != null
              ? `Pull request #${i.prNumber} is open.`
              : "The pull request is open.",
          body: "Merge it on your repository and the demo deploys by itself.",
          owner: "you",
          primary: A("open-pr", "Open the pull request"),
          secondary: [],
        });
      }
      if (dev === "failed") {
        return withEscalation({
          headline: "The build was interrupted - nothing was lost.",
          body: "Branch progress is kept; resume picks up where it stopped.",
          owner: "you",
          primary: A("resume", "Resume development"),
          secondary: [],
        });
      }
      if (kind === "auto_dev") {
        return withEscalation({
          headline: "Watching your repository.",
          body: "Matching issues become builds automatically - see the filters below.",
          owner: "agent",
          secondary: [],
        });
      }
      return withEscalation({
        headline: "Development is queued.",
        body: "The build starts in a moment.",
        owner: "agent",
        secondary: [],
      });
    }
    case "awaiting_customer": {
      const agg = aggregate(i);
      if (agg) return withEscalation(agg);
      if (i.demoUrl) {
        return withEscalation({
          headline: "Your demo is live - try it, then approve.",
          body: "Until you approve the delivery, it isn't done.",
          owner: "you",
          primary: A("approve", "Approve delivery"),
          secondary: [A("open-demo", "Open the demo")],
        });
      }
      if (dev === "awaiting_merge") {
        return withEscalation({
          headline:
            i.prNumber != null
              ? `Pull request #${i.prNumber} is waiting on you.`
              : "The pull request is waiting on you.",
          body: "Merge it and the demo deploys by itself.",
          owner: "you",
          primary: A("open-pr", "Open the pull request"),
          secondary: [],
        });
      }
      // A follow-up run dispatched while the project sits in awaiting_customer
      // (auto_dev issue builds, scoped requests after a merged PR) must read as
      // the live build it is - falling through to the "interrupted" copy here
      // contradicted the running console right below it.
      if (dev === "running" || dev === "deploying" || dev === "queued") {
        return withEscalation({
          headline:
            dev === "deploying"
              ? "Deploying the demo…"
              : dev === "queued"
                ? "Development is queued."
                : "The agent is building.",
          body:
            dev === "queued"
              ? "The build starts in a moment."
              : "Every edit, test and token below is live.",
          owner: "agent",
          secondary: dev === "queued" ? [] : [A("stop", "Stop build")],
        });
      }
      if (kind === "auto_dev" && dev !== "failed") {
        return withEscalation({
          headline: "Watching your repository.",
          body: "Matching issues become builds automatically - see the filters below.",
          owner: "agent",
          secondary: [],
        });
      }
      return withEscalation({
        headline: "The build was interrupted - nothing was lost.",
        body: "Branch progress is kept; resume picks up where it stopped.",
        owner: "you",
        primary: A("resume", "Resume development"),
        secondary: [],
      });
    }
    case "awaiting_admin":
      return {
        headline: `${consultant} has the project.`,
        body: "It moves again after their review - you'll get an email.",
        owner: "consultant",
        secondary: [],
      };
  }

  return { headline: String(i.status).replace(/_/g, " "), owner: "none", secondary: [] };
}
