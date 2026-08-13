import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import AuthShell from "../components/AuthShell";
import { Alert, Spinner } from "../components/ui";
import { authApi } from "../lib/endpoints";

export default function VerifyEmail() {
  const [params] = useSearchParams();
  const token = params.get("token");
  const [state, setState] = useState<"pending" | "ok" | "error">("pending");
  const [message, setMessage] = useState("");
  const ran = useRef(false);

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;
    if (!token) {
      setState("error");
      setMessage("Missing verification token.");
      return;
    }
    authApi
      .verifyEmail(token)
      .then(() => setState("ok"))
      .catch((err) => {
        setState("error");
        setMessage(err instanceof Error ? err.message : "Verification failed.");
      });
  }, [token]);

  return (
    <AuthShell title="Email verification">
      {state === "pending" && (
        <div className="loading-center">
          <Spinner /> Verifying…
        </div>
      )}
      {state === "ok" && (
        <>
          <Alert kind="success">Your email is verified. You can now create projects.</Alert>
          <Link to="/login" className="btn btn-primary btn-block">
            Continue to sign in
          </Link>
        </>
      )}
      {state === "error" && (
        <>
          <Alert kind="error">{message}</Alert>
          <Link to="/login" className="btn btn-block">
            Back to sign in
          </Link>
        </>
      )}
    </AuthShell>
  );
}
