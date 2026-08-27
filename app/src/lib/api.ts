// Same-origin fetch wrapper. Caches the CSRF token, attaches it to mutations,
// always sends credentials, throws typed ApiError, and redirects to /login on 401.

export class ApiError extends Error {
  status: number;
  detail: string;
  // Structured error payload when the API returned an object `detail`
  // (e.g. program input validation: {message, errors: {field: msg}}).
  data: unknown;
  constructor(status: number, detail: string, data: unknown = null) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.data = data;
  }
}

const MUTATING = new Set(["POST", "PUT", "PATCH", "DELETE"]);

let csrfToken: string | null = null;
let csrfPromise: Promise<string> | null = null;

async function getCsrf(): Promise<string> {
  if (csrfToken) return csrfToken;
  if (!csrfPromise) {
    csrfPromise = fetch("/api/auth/csrf", { credentials: "include" })
      .then(async (r) => {
        if (!r.ok) throw new ApiError(r.status, "Failed to fetch CSRF token");
        const data = await r.json();
        csrfToken = data.csrf_token as string;
        return csrfToken;
      })
      .finally(() => {
        csrfPromise = null;
      });
  }
  return csrfPromise;
}

// Force a fresh CSRF token on the next mutation (e.g. after login rotates it).
export function resetCsrf(): void {
  csrfToken = null;
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  // Multipart upload: sent as-is, Content-Type left to the browser (boundary).
  formData?: FormData;
  // The auth bootstrap check opts out of the automatic redirect so it can
  // render the login screen instead of hard-navigating.
  redirectOn401?: boolean;
  signal?: AbortSignal;
}

function redirectToLogin() {
  const path = window.location.pathname;
  if (path !== "/login" && path !== "/signup") {
    const next = encodeURIComponent(path + window.location.search);
    window.location.assign(`/login?next=${next}`);
  }
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const method = (opts.method ?? "GET").toUpperCase();
  const headers: Record<string, string> = {};
  const init: RequestInit = { method, credentials: "include", signal: opts.signal };

  if (opts.formData !== undefined) {
    init.body = opts.formData;
  } else if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  if (MUTATING.has(method)) {
    headers["X-CSRF-Token"] = await getCsrf();
  }
  init.headers = headers;

  const res = await fetch(`/api${path}`, init);

  if (res.status === 401) {
    if (opts.redirectOn401 !== false) redirectToLogin();
    const d = await detailOf(res, "Unauthorized");
    throw new ApiError(401, d.text, d.data);
  }

  if (!res.ok) {
    const d = await detailOf(res, res.statusText || "Request failed");
    throw new ApiError(res.status, d.text, d.data);
  }

  if (res.status === 204) return undefined as T;
  const text = await res.text();
  if (!text) return undefined as T;
  return JSON.parse(text) as T;
}

async function detailOf(res: Response, fallback: string): Promise<{ text: string; data: unknown }> {
  try {
    const data = await res.json();
    if (data && typeof data.detail === "string") return { text: data.detail, data: null };
    if (data && data.detail && typeof data.detail === "object") {
      const message = (data.detail as { message?: string }).message;
      return { text: message || fallback, data: data.detail };
    }
  } catch {
    // non-JSON body
  }
  return { text: fallback, data: null };
}

export const api = {
  get: <T>(path: string, opts?: RequestOptions) => request<T>(path, { ...opts, method: "GET" }),
  post: <T>(path: string, body?: unknown, opts?: RequestOptions) =>
    request<T>(path, { ...opts, method: "POST", body }),
  postForm: <T>(path: string, formData: FormData, opts?: RequestOptions) =>
    request<T>(path, { ...opts, method: "POST", formData }),
  put: <T>(path: string, body?: unknown, opts?: RequestOptions) =>
    request<T>(path, { ...opts, method: "PUT", body }),
  putForm: <T>(path: string, formData: FormData, opts?: RequestOptions) =>
    request<T>(path, { ...opts, method: "PUT", formData }),
  patch: <T>(path: string, body?: unknown, opts?: RequestOptions) =>
    request<T>(path, { ...opts, method: "PATCH", body }),
  del: <T>(path: string, opts?: RequestOptions) => request<T>(path, { ...opts, method: "DELETE" }),
};
