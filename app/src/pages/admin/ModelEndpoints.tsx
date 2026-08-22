import { useEffect, useState } from "react";
import { modelEndpointApi } from "../../lib/endpoints";
import { useToast } from "../../lib/toast";
import { Alert, Loading, Modal } from "../../components/ui";
import type { CatalogModel, ModelEndpoint, ModelProvider } from "../../types";

// Admin-only management of saved LLM API endpoints (§model config). Each endpoint
// bundles a provider preset, an OpenAI-compatible base URL, and an envelope-
// encrypted API key (never returned by the API). A project's Model-config modal
// then picks one of these instead of re-typing a base URL + key each time.

// Preset base URLs. "custom" leaves the base URL empty for a self-hosted or other
// OpenAI-compatible endpoint. These are prefills only - the admin can edit them.
const PRESETS: { id: ModelProvider; label: string; base_url: string; keyHint: string }[] = [
  { id: "openai", label: "OpenAI", base_url: "https://api.openai.com/v1", keyHint: "sk-…" },
  { id: "anthropic", label: "Anthropic", base_url: "https://api.anthropic.com/v1", keyHint: "sk-ant-…" },
  { id: "mistral", label: "Mistral", base_url: "https://api.mistral.ai/v1", keyHint: "…" },
  // OpenAI-compatible gateways: one key, many models. EURouter and CARouter keep
  // the request inside the EU / Canada, which is what a sovereign track needs.
  { id: "openrouter", label: "OpenRouter", base_url: "https://openrouter.ai/api/v1", keyHint: "sk-or-v1-…" },
  { id: "eurouter", label: "EURouter", base_url: "https://api.eurouter.ai/v1", keyHint: "sk-eurouter-…" },
  { id: "carouter", label: "CARouter", base_url: "https://carouter.ai/v1", keyHint: "…" },
  { id: "custom", label: "Custom", base_url: "", keyHint: "API key" },
];

const PROVIDER_LABEL: Record<ModelProvider, string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  mistral: "Mistral",
  openrouter: "OpenRouter",
  eurouter: "EURouter",
  carouter: "CARouter",
  custom: "Custom",
};

export default function ModelEndpoints() {
  const toast = useToast();
  const [rows, setRows] = useState<ModelEndpoint[] | null>(null);
  const [editing, setEditing] = useState<ModelEndpoint | "new" | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  function load() {
    modelEndpointApi
      .list()
      .then(setRows)
      .catch((err) =>
        toast.push(err instanceof Error ? err.message : "Could not load model endpoints.", "err"),
      );
  }

  useEffect(load, []);

  async function remove(ep: ModelEndpoint) {
    if (!window.confirm(`Remove the "${ep.label}" endpoint?`)) return;
    setBusyId(ep.id);
    try {
      await modelEndpointApi.remove(ep.id);
      toast.push("Endpoint removed", "ok");
      load();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not remove.", "err");
    } finally {
      setBusyId(null);
    }
  }

  // Preflight the API surfaces a build will hit (chat/completions + responses for
  // OpenAI gpt-5/codex models) so a bad key/model/permission surfaces here, not
  // minutes into a paid dev run.
  async function test(ep: ModelEndpoint) {
    setBusyId(ep.id);
    try {
      const r = await modelEndpointApi.test(ep.id);
      const part = (name: string, p: { ok: boolean; error: string | null } | null) =>
        p === null ? null : `${name} ${p.ok ? "OK" : `failed: ${(p.error ?? "").slice(0, 160)}`}`;
      const effortPart = r.effort
        ? `effort "${r.effort.value}" ${r.effort.accepted ? "accepted" : `NOT accepted (${r.effort.detail.slice(0, 120)})`}`
        : null;
      // §chat images: the probe IS the discovery - there is no capability API.
      const visionPart = r.vision
        ? `images ${r.vision.supported === true ? "supported" : r.vision.supported === false
            ? "NOT supported" : "unknown"} (${r.vision.detail.slice(0, 90)})`
        : null;
      const parts = [part("chat/completions", r.chat_completions), part("responses", r.responses),
                     effortPart, visionPart]
        .filter(Boolean)
        .join(" · ");
      const allOk = r.chat_completions.ok && (r.responses === null || r.responses.ok)
        && (r.effort === null || r.effort.accepted);
      toast.push(parts, allOk ? "ok" : "err");
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Test failed.", "err");
    } finally {
      setBusyId(null);
    }
  }

  if (!rows) return <Loading />;

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Model configuration</h1>
          <p className="muted">
            Saved API endpoints the dev agent can run through. Pick one per project in its Model
            config.
          </p>
        </div>
        <button type="button" className="btn btn-primary" onClick={() => setEditing("new")}>
          Add endpoint
        </button>
      </div>

      <Alert kind="info">
        Each endpoint stores an OpenAI-compatible base URL, an API key (encrypted and never shown
        again) and a model - picked from the provider's live model list (OpenAI, Anthropic,
        Mistral) or typed as a custom name. Models outside the platform price table also carry
        their own price. Assign an endpoint to a project from its admin Model config.
      </Alert>

      {rows.length === 0 ? (
        <div className="card mt center muted" style={{ maxWidth: 760 }}>
          No endpoints yet. Add one to route a project's builds to a specific provider.
        </div>
      ) : (
        <div className="card mt" style={{ maxWidth: 760 }}>
          {rows.map((ep) => (
            <div key={ep.id} className="kb-row between" style={{ alignItems: "flex-start", gap: "1rem" }}>
              <div style={{ minWidth: 0 }}>
                <div className="row gap-sm" style={{ alignItems: "center", flexWrap: "wrap" }}>
                  <span className="badge badge-kb-mcp">{PROVIDER_LABEL[ep.provider]}</span>
                  <strong>{ep.label}</strong>
                  <span className={`badge ${ep.has_api_key ? "badge-ok" : "badge-warn"}`}>
                    {ep.has_api_key ? "Key set" : "No key"}
                  </span>
                </div>
                <div className="muted small mt-xs" style={{ wordBreak: "break-all" }}>
                  {ep.model_name || "no model set"} · {ep.base_url}
                  {!ep.model_priced && ep.input_price != null && (
                    <>
                      {" "}· custom ${ep.input_price}/${ep.output_price}
                      {ep.cached_input_price != null && <> (cached ${ep.cached_input_price})</>} per 1M
                    </>
                  )}
                </div>
                <div className="row gap-sm mt-xs">
                  <button
                    type="button"
                    className="btn btn-sm"
                    disabled={busyId === ep.id}
                    onClick={() => setEditing(ep)}
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    className="btn btn-sm"
                    disabled={busyId === ep.id || !ep.has_api_key || !ep.model_name}
                    onClick={() => test(ep)}
                  >
                    {busyId === ep.id ? "Testing…" : "Test"}
                  </button>
                  <button
                    type="button"
                    className="btn btn-sm btn-danger"
                    disabled={busyId === ep.id}
                    onClick={() => remove(ep)}
                  >
                    Remove
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {editing && (
        <EndpointModal
          endpoint={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            load();
          }}
        />
      )}
    </div>
  );
}

function EndpointModal({
  endpoint,
  onClose,
  onSaved,
}: {
  endpoint: ModelEndpoint | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const isEdit = endpoint !== null;
  const [label, setLabel] = useState(endpoint?.label ?? "");
  const [provider, setProvider] = useState<ModelProvider>(endpoint?.provider ?? "openai");
  const [baseUrl, setBaseUrl] = useState(
    endpoint?.base_url ?? PRESETS.find((p) => p.id === "openai")!.base_url,
  );
  const [apiKey, setApiKey] = useState("");
  const [modelName, setModelName] = useState(endpoint?.model_name ?? "");
  // The provider's live model list (null = not loaded). customModel switches the
  // picker back to a free-text field for a model the provider doesn't list.
  const [models, setModels] = useState<CatalogModel[] | null>(null);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [loadingModels, setLoadingModels] = useState(false);
  const [customModel, setCustomModel] = useState(false);
  // api_model aliases the static price table can bill - a model in it needs no
  // custom price, so the price inputs stay hidden.
  const [priced, setPriced] = useState<Set<string>>(new Set());
  const [inputPrice, setInputPrice] = useState(
    endpoint?.input_price != null ? String(endpoint.input_price) : "",
  );
  const [outputPrice, setOutputPrice] = useState(
    endpoint?.output_price != null ? String(endpoint.output_price) : "",
  );
  const [cachedInputPrice, setCachedInputPrice] = useState(
    endpoint?.cached_input_price != null ? String(endpoint.cached_input_price) : "",
  );
  const [effort, setEffort] = useState<string>(endpoint?.reasoning_effort ?? "");
  const [supportsImages, setSupportsImages] = useState<boolean>(
    endpoint?.supports_images === true,
  );
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    modelEndpointApi
      .pricedModels()
      .then((r) => setPriced((prev) => new Set([...prev, ...r.models])))
      .catch(() => {}); // soft-fail: prices are then asked for; the API drops superfluous ones
  }, []);

  const canLoadModels = Boolean(
    baseUrl.trim() && (apiKey.trim() || (isEdit && endpoint?.has_api_key)),
  );

  async function loadModels() {
    if (!canLoadModels || loadingModels) return;
    setLoadingModels(true);
    setModelsError(null);
    try {
      const r = await modelEndpointApi.models({
        provider,
        base_url: baseUrl.trim(),
        ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
        ...(isEdit ? { endpoint_id: endpoint!.id } : {}),
      });
      setModels(r.models);
      setModelsError(r.error);
      if (r.models.length) {
        setPriced(
          (prev) => new Set([...prev, ...r.models.filter((m) => m.priced).map((m) => m.id)]),
        );
        // a saved model the provider no longer lists keeps working as a custom one
        if (modelName && !r.models.some((m) => m.id === modelName)) setCustomModel(true);
      }
    } catch (err) {
      setModels([]);
      setModelsError(err instanceof Error ? err.message : "Could not load models.");
    } finally {
      setLoadingModels(false);
    }
  }

  // Editing an endpoint with a stored key: the list is one call away - fetch it.
  useEffect(() => {
    if (isEdit && endpoint?.has_api_key) void loadModels();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Picking a preset fills the base URL (unless the admin already typed a custom
  // one) so the common case is one click. "Custom" clears it for a self-hosted host.
  function pickPreset(id: ModelProvider) {
    setProvider(id);
    setModels(null); // the list belongs to the previous provider/URL
    const preset = PRESETS.find((p) => p.id === id)!;
    const known = PRESETS.some((p) => p.base_url && p.base_url === baseUrl);
    if (id === "custom") {
      if (known) setBaseUrl("");
    } else if (!baseUrl || known) {
      setBaseUrl(preset.base_url);
    }
  }

  const keyHint = PRESETS.find((p) => p.id === provider)!.keyHint;
  const pickerAvailable = (models?.length ?? 0) > 0;
  const usePicker = pickerAvailable && !customModel;
  const modelPriced = modelName.trim() !== "" && priced.has(modelName.trim());

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    try {
      const key = apiKey.trim();
      const common = {
        label: label.trim(),
        provider,
        base_url: baseUrl.trim(),
        model_name: modelName.trim(),
        input_price: inputPrice.trim() === "" ? null : Number(inputPrice),
        output_price: outputPrice.trim() === "" ? null : Number(outputPrice),
        cached_input_price: cachedInputPrice.trim() === "" ? null : Number(cachedInputPrice),
        reasoning_effort: (effort || "") as "" | "low" | "medium" | "high",
        // Only send a declaration when it is one: unchecked must not overwrite a
        // probe verdict of "supported" with a hand-typed "no".
        ...(supportsImages ? { supports_images: true } : {}),
      };
      if (isEdit) {
        await modelEndpointApi.update(endpoint!.id, {
          ...common,
          ...(key ? { api_key: key } : {}),
        });
      } else {
        await modelEndpointApi.create({ ...common, api_key: key });
      }
      toast.push(isEdit ? "Endpoint saved" : "Endpoint added", "ok");
      onSaved();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not save.", "err");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal title={isEdit ? "Edit endpoint" : "Add model endpoint"} onClose={onClose} wide>
      <form onSubmit={submit}>
        <label className="field">
          <span>Provider preset</span>
          <div className="row gap-sm" style={{ flexWrap: "wrap" }}>
            {PRESETS.map((p) => (
              <button
                key={p.id}
                type="button"
                className={`btn btn-sm ${provider === p.id ? "btn-primary" : ""}`}
                onClick={() => pickPreset(p.id)}
              >
                {p.label}
              </button>
            ))}
          </div>
        </label>
        <label className="field">
          <span>Label</span>
          <input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="OpenAI production"
            required
            maxLength={128}
          />
        </label>
        <label className="field">
          <span>Base URL</span>
          <input
            value={baseUrl}
            onChange={(e) => {
              setBaseUrl(e.target.value);
              setModels(null); // the loaded list belongs to the previous URL
            }}
            placeholder="https://api.example.com/v1"
            type="url"
            required
            maxLength={512}
          />
        </label>
        <label className="field">
          <span>
            API key {isEdit && <span className="muted small">(leave blank to keep the current key)</span>}
          </span>
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={isEdit && endpoint?.has_api_key ? "••••••••" : keyHint}
            autoComplete="new-password"
            required={!isEdit}
          />
        </label>
        <label className="row gap-sm" style={{ alignItems: "flex-start" }}>
          <input
            type="checkbox"
            checked={supportsImages}
            onChange={(e) => setSupportsImages(e.target.checked)}
            style={{ marginTop: "0.25rem" }}
          />
          <span>
            This model can read images
            <span className="tiny faint" style={{ display: "block" }}>
              Enables image attachments in the chat of projects using this endpoint. Leave
              unchecked and press Test instead - the probe sends a 1-pixel image and records
              the provider's answer. Tick this only when you know the model is multimodal but
              the probe can't confirm it; your declaration then outranks the probe.
            </span>
          </span>
        </label>
        <label className="field">
          <span>
            Reasoning effort{" "}
            <span
              title="Requested reasoning depth for models that support it (GPT-5/o-series, Qwen thinking…). Dev builds default to HIGH when unset; tiny utility calls (titles, chat classification, branch names) always run LOW. Providers without the parameter ignore it safely."
              style={{ cursor: "help", opacity: 0.7 }}
            >
              ⓘ
            </span>
          </span>
          <input
            list="effort-suggestions"
            value={effort}
            onChange={(e) => setEffort(e.target.value.trim())}
            placeholder="Provider default (dev builds: high)"
            maxLength={16}
          />
          <datalist id="effort-suggestions">
            <option value="minimal" />
            <option value="low" />
            <option value="medium" />
            <option value="high" />
            <option value="xhigh" />
            <option value="max" />
          </datalist>
          <p className="tiny faint mt">
            Free-form for exotic providers - after saving, Test reports whether the
            provider actually accepts the value (there is no discovery API; the probe is
            the discovery).
          </p>
        </label>
        <label className="field">
          <span>Model</span>
          {usePicker ? (
            <select
              value={models!.some((m) => m.id === modelName) ? modelName : ""}
              onChange={(e) => {
                if (e.target.value === "__custom__") {
                  setCustomModel(true);
                  setModelName("");
                } else {
                  setModelName(e.target.value);
                }
              }}
              required
            >
              <option value="" disabled>
                Pick a model…
              </option>
              {models!.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.id}
                </option>
              ))}
              <option value="__custom__">Custom model…</option>
            </select>
          ) : (
            <input
              value={modelName}
              onChange={(e) => setModelName(e.target.value)}
              placeholder="gpt-5.6-luna"
              required
              maxLength={128}
            />
          )}
          <div className="row gap-sm mt-xs">
            {models === null && (
              <button
                type="button"
                className="btn btn-sm"
                disabled={!canLoadModels || loadingModels}
                onClick={loadModels}
                title={canLoadModels ? "" : "Enter the base URL and API key first"}
              >
                {loadingModels ? "Loading models…" : "Load model list"}
              </button>
            )}
            {pickerAvailable && customModel && (
              <button type="button" className="btn btn-sm" onClick={() => setCustomModel(false)}>
                Pick from the list instead
              </button>
            )}
          </div>
          {modelsError && (
            <p className="tiny faint" style={{ margin: "0.35rem 0 0" }}>
              Model list unavailable ({modelsError}) - enter the model name manually.
            </p>
          )}
        </label>
        {modelName.trim() !== "" && !modelPriced && (
          <div className="field">
            <label>
              Pricing <span className="muted small">(per 1M tokens, USD)</span>
            </label>
            <p className="tiny faint" style={{ margin: "0 0 0.35rem" }}>
              This model isn't in the platform price table - enter its input and output cost
              so its usage can be billed.
            </p>
            <div className="row gap-sm">
              <input
                type="number"
                min={0}
                step="any"
                value={inputPrice}
                onChange={(e) => setInputPrice(e.target.value)}
                placeholder="Input $ / 1M"
                required
              />
              <input
                type="number"
                min={0}
                step="any"
                value={outputPrice}
                onChange={(e) => setOutputPrice(e.target.value)}
                placeholder="Output $ / 1M"
                required
              />
              <input
                type="number"
                min={0}
                step="any"
                value={cachedInputPrice}
                onChange={(e) => setCachedInputPrice(e.target.value)}
                placeholder="Cached input $ / 1M (optional)"
              />
            </div>
            <p className="tiny faint" style={{ margin: "0.35rem 0 0" }}>
              Cached input: the provider's rate for prompt-cache reads. Leave empty if the
              provider publishes none - cached tokens then bill at the full input price.
            </p>
          </div>
        )}
        <div className="row gap-sm mt">
          <button type="submit" className="btn btn-primary" disabled={saving}>
            {isEdit ? "Save changes" : "Add endpoint"}
          </button>
          <button type="button" className="btn" onClick={onClose} disabled={saving}>
            Cancel
          </button>
        </div>
      </form>
    </Modal>
  );
}
