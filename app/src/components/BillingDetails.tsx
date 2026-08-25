/** The billing profile as it will appear on an invoice, read-only.
 *
 * It sits on the Billing page because that is what it is for; editing stays on
 * the account page, which is where the account type and the contact name live.
 * The point of showing it here is that nothing else tells a customer what their
 * invoices actually say until one arrives. */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { accountApi } from "../lib/endpoints";
import { useAuth } from "../lib/auth";
import type { BillingCountry } from "../types";

const ADDRESS_FIELD_LABELS: Record<string, string> = {
  line1: "address",
  city: "city",
  state: "state or province",
  postal_code: "postal code",
  country: "country",
};

export default function BillingDetails() {
  const { me } = useAuth();
  const [countries, setCountries] = useState<BillingCountry[]>([]);

  useEffect(() => {
    accountApi
      .countries()
      .then((r) => setCountries(r.countries))
      .catch(() => setCountries([]));
  }, []);

  if (!me) return null;
  const org = me.org;
  const country = countries.find((c) => c.code === org.country);
  const missing = org.billing_address_missing ?? [];

  // The last line used to be able to read "ON, Canada" unconditionally. The
  // country comes off the record, and the subdivision only appears when the
  // country has one.
  const lines = [
    org.address_line1,
    org.address_line2,
    [org.postal_code, org.city].filter(Boolean).join(" "),
    [org.province, country?.name ?? org.country].filter(Boolean).join(", "),
  ].filter((line) => line && String(line).trim());

  return (
    <div className="card mt">
      <div className="section-title">Billing details</div>
      {/* Follows the account TYPE, not whichever field is filled in: switching
          back to individual keeps the stored company details so a round-trip
          loses nothing, and reading `company_name || name` addressed a personal
          invoice to a company the account no longer says it is. */}
      <div style={{ fontWeight: 600, marginBottom: 6 }}>
        {(org.type === "organization" ? org.company_name : null) || org.name}{" "}
        <span className="badge">{org.type === "organization" ? "Organization" : "Individual"}</span>
      </div>

      {lines.length > 0 ? (
        <div className="muted small" style={{ lineHeight: 1.7 }}>
          {lines.map((line, i) => (
            <div key={i}>{line}</div>
          ))}
        </div>
      ) : (
        <p className="muted small">No billing address on file.</p>
      )}

      {/* Organization accounts only, matching what is actually sent: in the EU
          a customer VAT number moves the invoice to reverse charge, so showing
          one a personal invoice will not carry would be a promise about tax. */}
      {org.type === "organization" && org.vat_id && (
        <div className="mono tiny" style={{ marginTop: 6 }}>
          {country?.tax_id_label ?? "Tax number"}: {org.vat_id}
        </div>
      )}

      {/* A partial address is the dangerous case, not an empty one: the block
          above renders whatever exists and reads as filled in, while Stripe
          receives nothing at all - it is withheld whole rather than sent
          incomplete, because a partial address resolves to the wrong tax rate
          instead of to an error. */}
      {missing.length > 0 && (
        <p className="small" style={{ color: "var(--danger)", marginTop: 8 }}>
          Incomplete, so it isn't sent to Stripe at all and your invoices won't carry it.
          Missing: {missing.map((f) => ADDRESS_FIELD_LABELS[f] ?? f).join(", ")}.
        </p>
      )}

      <p className="muted small" style={{ marginTop: 10 }}>
        What your invoices are addressed to, and what the tax on them is calculated from.{" "}
        <Link to="/settings/account">Edit</Link>
      </p>
    </div>
  );
}
