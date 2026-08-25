import { useEffect, useState } from "react";
import { accountApi } from "../lib/endpoints";
import { useAuth } from "../lib/auth";
import { useToast } from "../lib/toast";
import { Alert, Loading, Spinner } from "../components/ui";
import type { AccountType, BillingCountry } from "../types";

// Account settings: switch individual ↔ organization, edit the display name, and
// the billing details every invoice is rendered from - company name, tax number,
// and the address Stripe Tax computes the rate from (§18).

// Postal-code placeholders, purely a hint about the SHAPE expected. Deliberately
// not validation, here or on the server: a pattern that is subtly wrong for one
// country refuses a real address, and there is no upside to guessing.
const POSTAL_EXAMPLE: Record<string, string> = {
  FR: "75001", DE: "10119", BE: "1000", ES: "28001", IT: "00184", NL: "1011 AC",
  IE: "D02 AF30", PT: "1100-148", PL: "00-001", GB: "SW1A 1AA", CH: "8001",
  US: "94105", CA: "K1A 0B1",
};

// Stripe's field names, as the customer sees them on the form below. Keyed by
// what the API returns so a newly required field cannot render as a raw
// identifier. `state` covers both, because the form itself already relabels it.
const ADDRESS_FIELD_LABELS: Record<string, string> = {
  line1: "address",
  city: "city",
  state: "state or province",
  postal_code: "postal code",
  country: "country",
};

export default function AccountSettings() {
  const { me, refresh } = useAuth();
  const toast = useToast();
  const [accountType, setAccountType] = useState<AccountType>("individual");
  const [fullName, setFullName] = useState("");
  const [companyName, setCompanyName] = useState("");
  const [vatId, setVatId] = useState("");
  const [addressLine1, setAddressLine1] = useState("");
  const [addressLine2, setAddressLine2] = useState("");
  const [postalCode, setPostalCode] = useState("");
  const [city, setCity] = useState("");
  const [country, setCountry] = useState("");
  const [province, setProvince] = useState("");
  const [countries, setCountries] = useState<BillingCountry[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    accountApi
      .countries()
      .then((r) => setCountries(r.countries))
      .catch(() => setCountries([]));
  }, []);

  useEffect(() => {
    if (!me) return;
    setAccountType(me.org.type);
    setFullName(me.user.full_name ?? "");
    setCompanyName(me.org.company_name ?? "");
    setVatId(me.org.vat_id ?? "");
    setAddressLine1(me.org.address_line1 ?? "");
    setAddressLine2(me.org.address_line2 ?? "");
    setPostalCode(me.org.postal_code ?? "");
    setCity(me.org.city ?? "");
    setCountry(me.org.country ?? "");
    setProvince(me.org.province ?? "");
  }, [me]);

  if (!me) return <Loading />;

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await accountApi.update({
        account_type: accountType,
        full_name: fullName.trim(),
        company_name: companyName,
        vat_id: vatId,
        address_line1: addressLine1,
        address_line2: addressLine2,
        postal_code: postalCode,
        city,
        country,
        province,
      });
      await refresh();
      toast.push("Account updated", "ok");
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not save.", "err");
    } finally {
      setBusy(false);
    }
  }

  const isOrg = accountType === "organization";
  const selected = countries.find((c) => c.code === country);
  // Changing country drops the old subdivision rather than carrying it over:
  // "ON" is a real Ontario and a real nothing-at-all in Germany, and leaving it
  // in the form is how it reaches the server and then Stripe Tax.
  const changeCountry = (code: string) => {
    setCountry(code);
    setProvince("");
  };
  // A PARTIAL address is the dangerous case, not an empty one: the summary
  // reads as filled in while Stripe receives nothing at all, because an
  // incomplete address is withheld whole rather than sent (a partial one
  // resolves to the wrong tax rate instead of to an error).
  const missing = me.org.billing_address_missing ?? [];

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Account</h1>
          <p className="muted">
            Who we bill and how we address you. Signed in as {me.user.email}.
          </p>
        </div>
      </div>

      <div className="card" style={{ maxWidth: 560 }}>
        {missing.length > 0 && (
          <Alert kind="warn">
            Your billing address is incomplete, so it isn't sent to Stripe at all: checkout
            won't prefill it, and tax is computed from whatever you type there instead.
            Missing: {missing.map((f) => ADDRESS_FIELD_LABELS[f] ?? f).join(", ")}.
          </Alert>
        )}
        <form onSubmit={save}>
          <div className="field">
            <label>Account type</label>
            <div className="row">
              <label className="checkbox-row">
                <input
                  type="radio"
                  name="acct"
                  checked={!isOrg}
                  onChange={() => setAccountType("individual")}
                />
                Individual
              </label>
              <label className="checkbox-row">
                <input
                  type="radio"
                  name="acct"
                  checked={isOrg}
                  onChange={() => setAccountType("organization")}
                />
                Organization
              </label>
            </div>
          </div>

          <div className="field">
            <label htmlFor="fullname">Your full name</label>
            <input
              id="fullname"
              type="text"
              required
              autoComplete="name"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
            />
            {isOrg && <div className="hint">The contact person for this company account.</div>}
          </div>

          {isOrg && (
            <>
              <div className="field">
                <label htmlFor="company">Company name</label>
                <input
                  id="company"
                  type="text"
                  required
                  autoComplete="organization"
                  value={companyName}
                  onChange={(e) => setCompanyName(e.target.value)}
                />
                <div className="hint">The legal name your invoices are made out to.</div>
              </div>
              <div className="field">
                {/* The label follows the country: asking a customer in Denver for
                    a "VAT number" is a question the United States does not answer. */}
                <label htmlFor="vat">{selected?.tax_id_label ?? "Tax number"} (optional)</label>
                <input
                  id="vat"
                  type="text"
                  value={vatId}
                  onChange={(e) => setVatId(e.target.value)}
                />
                <div className="hint">
                  {selected?.tax_id_hint ||
                    "Shown on every invoice. In the EU, a valid VAT number moves the tax to you."}
                </div>
              </div>
            </>
          )}

          <div className="field">
            <label htmlFor="country">Country</label>
            <select
              id="country"
              required={isOrg}
              value={country}
              onChange={(e) => changeCountry(e.target.value)}
            >
              <option value="">Select a country</option>
              {countries.map((c) => (
                <option key={c.code} value={c.code}>
                  {c.name}
                </option>
              ))}
            </select>
          </div>

          <div className="field">
            <label htmlFor="addr1">Address</label>
            <input
              id="addr1"
              type="text"
              required={isOrg}
              autoComplete="address-line1"
              value={addressLine1}
              onChange={(e) => setAddressLine1(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="addr2">Address line 2 (optional)</label>
            <input
              id="addr2"
              type="text"
              autoComplete="address-line2"
              value={addressLine2}
              onChange={(e) => setAddressLine2(e.target.value)}
            />
          </div>
          <div className="row">
            <div className="field" style={{ flex: 1 }}>
              <label htmlFor="postal">Postal code</label>
              <input
                id="postal"
                type="text"
                required={isOrg}
                autoComplete="postal-code"
                placeholder={POSTAL_EXAMPLE[country] ?? ""}
                value={postalCode}
                onChange={(e) => setPostalCode(e.target.value)}
              />
            </div>
            <div className="field" style={{ flex: 2 }}>
              <label htmlFor="city">City</label>
              <input
                id="city"
                type="text"
                required={isOrg}
                autoComplete="address-level2"
                value={city}
                onChange={(e) => setCity(e.target.value)}
              />
            </div>
          </div>
          {/* Only where the rate depends on one. An EU customer has no
              subdivision to pick, and an empty dropdown reads as a required
              field they cannot satisfy. */}
          {selected && selected.subdivisions.length > 0 && (
            <div className="field">
              <label htmlFor="province">{country === "US" ? "State" : "Province"}</label>
              <select
                id="province"
                required={isOrg}
                value={province}
                onChange={(e) => setProvince(e.target.value)}
              >
                <option value="">Select</option>
                {selected.subdivisions.map((s) => (
                  <option key={s.code} value={s.code}>
                    {s.code} - {s.name}
                  </option>
                ))}
              </select>
              <div className="hint">
                Tax here is charged by {country === "US" ? "state" : "province"}, so an
                address without one is billed at the wrong rate.
              </div>
            </div>
          )}

          {!isOrg && (
            <div className="hint" style={{ marginBottom: 12 }}>
              Optional for an individual account, but it's what your invoices are addressed
              to and what the tax on them is calculated from.
            </div>
          )}

          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy ? <Spinner /> : "Save changes"}
          </button>
        </form>
      </div>
    </div>
  );
}
