"""billing profile country codes, province, and invoices on the ledger

Revision ID: b7c1d9e2f3a4
Revises: cef0e798a5e2
Create Date: 2026-08-25 23:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import pgvector.sqlalchemy


revision: str = 'b7c1d9e2f3a4'
down_revision: Union[str, None] = 'cef0e798a5e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# `organization.country` was free text, so it holds whatever customers typed.
# Stripe Tax resolves a rate from an ISO 3166-1 alpha-2 code and nothing else -
# "France" is not a country to it, it is a 400 in the middle of a payment - so
# the column narrows to two characters and the text has to be converted first.
# Anything this cannot place is cleared rather than truncated: a wrong code
# silently taxes an invoice at another country's rate, while an empty one shows
# up on the account page as an address to complete.
#
# The English names come from services/countries.SUPPORTED; the rest are the
# spellings a customer of an EU deployment actually types. Spelled out here
# rather than imported, because a migration has to keep meaning what it meant
# when it ran.
_NAME_TO_CODE = {
    "austria": "AT", "osterreich": "AT", "autriche": "AT",
    "belgium": "BE", "belgique": "BE", "belgie": "BE", "belgien": "BE",
    "bulgaria": "BG", "bulgarie": "BG",
    "croatia": "HR", "croatie": "HR", "hrvatska": "HR",
    "cyprus": "CY", "chypre": "CY",
    "czechia": "CZ", "czech republic": "CZ", "tchequie": "CZ",
    "republique tcheque": "CZ",
    "denmark": "DK", "danemark": "DK", "danmark": "DK",
    "estonia": "EE", "estonie": "EE", "eesti": "EE",
    "finland": "FI", "finlande": "FI", "suomi": "FI",
    "france": "FR", "french republic": "FR",
    "germany": "DE", "allemagne": "DE", "deutschland": "DE",
    "greece": "GR", "grece": "GR", "hellas": "GR",
    "hungary": "HU", "hongrie": "HU", "magyarorszag": "HU",
    "ireland": "IE", "irlande": "IE", "eire": "IE",
    "italy": "IT", "italie": "IT", "italia": "IT",
    "latvia": "LV", "lettonie": "LV", "latvija": "LV",
    "lithuania": "LT", "lituanie": "LT", "lietuva": "LT",
    "luxembourg": "LU", "letzebuerg": "LU",
    "malta": "MT", "malte": "MT",
    "netherlands": "NL", "pays-bas": "NL", "pays bas": "NL", "nederland": "NL",
    "holland": "NL", "the netherlands": "NL",
    "poland": "PL", "pologne": "PL", "polska": "PL",
    "portugal": "PT",
    "romania": "RO", "roumanie": "RO",
    "slovakia": "SK", "slovaquie": "SK", "slovensko": "SK",
    "slovenia": "SI", "slovenie": "SI", "slovenija": "SI",
    "spain": "ES", "espagne": "ES", "espana": "ES",
    "sweden": "SE", "suede": "SE", "sverige": "SE",
    "united kingdom": "GB", "royaume-uni": "GB", "royaume uni": "GB",
    "great britain": "GB", "england": "GB", "scotland": "GB", "wales": "GB",
    "uk": "GB",
    "switzerland": "CH", "suisse": "CH", "schweiz": "CH", "svizzera": "CH",
    "norway": "NO", "norvege": "NO", "norge": "NO",
    "united states": "US", "united states of america": "US", "usa": "US",
    "etats-unis": "US", "etats unis": "US", "america": "US",
    "canada": "CA",
}

_SUPPORTED = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
    "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK",
    "SI", "ES", "SE", "GB", "CH", "NO", "US", "CA",
}

# Strips accents and punctuation so "Österreich", "Etats-Unis" and "ETATS UNIS"
# all reach the table above by the same key.
_NORMALIZE = (
    "translate(lower(trim(country)), "
    "'áàâäãåéèêëíìîïóòôöõúùûüçñýÿ', 'aaaaaaeeeeiiiiooooouuuucnyy')"
)


def upgrade() -> None:
    op.add_column('organization', sa.Column('province', sa.String(length=2), nullable=True))

    cases = "\n".join(
        f"            WHEN {_NORMALIZE} = '{name}' THEN '{code}'"
        for name, code in _NAME_TO_CODE.items())
    codes = ", ".join(f"'{c}'" for c in sorted(_SUPPORTED))
    op.execute(f"""
        UPDATE organization SET country = CASE
            WHEN upper(trim(country)) IN ({codes}) THEN upper(trim(country))
{cases}
            ELSE NULL
        END
        WHERE country IS NOT NULL
    """)
    op.alter_column('organization', 'country',
                    existing_type=sa.String(length=128),
                    type_=sa.String(length=2),
                    existing_nullable=True)

    op.add_column('credit_transaction', sa.Column('topup_ref', sa.String(length=64), nullable=True))
    op.create_index(op.f('ix_credit_transaction_topup_ref'), 'credit_transaction',
                    ['topup_ref'], unique=False)
    op.add_column('credit_transaction', sa.Column('invoice_number', sa.String(length=64), nullable=True))
    op.add_column('credit_transaction', sa.Column('invoice_url', sa.Text(), nullable=True))
    op.add_column('credit_transaction', sa.Column('invoice_pdf', sa.Text(), nullable=True))
    op.add_column('credit_transaction', sa.Column('tax_amount', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('credit_transaction', 'tax_amount')
    op.drop_column('credit_transaction', 'invoice_pdf')
    op.drop_column('credit_transaction', 'invoice_url')
    op.drop_column('credit_transaction', 'invoice_number')
    op.drop_index(op.f('ix_credit_transaction_topup_ref'), table_name='credit_transaction')
    op.drop_column('credit_transaction', 'topup_ref')
    # The country names this migration converted are not recoverable, and a code
    # is a valid value in the wider column, so the data stays as codes.
    op.alter_column('organization', 'country',
                    existing_type=sa.String(length=2),
                    type_=sa.String(length=128),
                    existing_nullable=True)
    op.drop_column('organization', 'province')
