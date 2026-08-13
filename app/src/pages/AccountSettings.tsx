import { useEffect, useState } from "react";
import { accountApi } from "../lib/endpoints";
import { useAuth } from "../lib/auth";
import { useToast } from "../lib/toast";
import { Loading, Spinner } from "../components/ui";
import type { AccountType } from "../types";

// Account settings: switch individual ↔ organization, edit the display name,
// and (for organizations) the invoicing details - company name, VAT, full address.
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
  const [busy, setBusy] = useState(false);

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
              </div>
              <div className="field">
                <label htmlFor="vat">VAT ID (optional)</label>
                <input
                  id="vat"
                  type="text"
                  value={vatId}
                  onChange={(e) => setVatId(e.target.value)}
                />
              </div>
              <div className="field">
                <label htmlFor="addr1">Address</label>
                <input
                  id="addr1"
                  type="text"
                  required
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
                    required
                    autoComplete="postal-code"
                    value={postalCode}
                    onChange={(e) => setPostalCode(e.target.value)}
                  />
                </div>
                <div className="field" style={{ flex: 2 }}>
                  <label htmlFor="city">City</label>
                  <input
                    id="city"
                    type="text"
                    required
                    autoComplete="address-level2"
                    value={city}
                    onChange={(e) => setCity(e.target.value)}
                  />
                </div>
              </div>
              <div className="field">
                <label htmlFor="country">Country</label>
                <input
                  id="country"
                  type="text"
                  required
                  autoComplete="country-name"
                  value={country}
                  onChange={(e) => setCountry(e.target.value)}
                />
              </div>
            </>
          )}

          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy ? <Spinner /> : "Save changes"}
          </button>
        </form>
      </div>
    </div>
  );
}
