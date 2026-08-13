import { Link } from "react-router-dom";
import Markdown from "../components/Markdown";
import { CopyButton } from "../components/ui";
import { useAuth } from "../lib/auth";
import programsDocRaw from "../docs/programs.md?raw";

// Programs authoring documentation (§28): the full repo contract rendered
// in-app, written to be handed to an AI assistant that develops the program.
export default function ProgramsDoc() {
  const { settings } = useAuth();
  const brandName = settings?.brand_name ?? "Openvisor";
  const doc = programsDocRaw.replace(/\{\{BRAND\}\}/g, brandName);

  return (
    <div>
      <div className="between mb">
        <h1 style={{ margin: 0 }}>Program development</h1>
        <div className="row gap-sm">
          <CopyButton value={doc} label="Copy for your AI" className="btn btn-primary" />
          <Link to="/programs" className="btn btn-sm btn-ghost">
            Back to Programs
          </Link>
        </div>
      </div>
      <p className="muted small">
        Everything a program repository must contain to run on {brandName}. Easiest path: copy the
        whole document and paste it to your AI assistant together with what your program should do.
      </p>
      <div className="card">
        <Markdown>{doc}</Markdown>
      </div>
    </div>
  );
}
