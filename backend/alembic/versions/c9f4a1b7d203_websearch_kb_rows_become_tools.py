"""web search moves from knowledge bases to tools

A knowledge base is a CORPUS the agent consults; a keyed SERP API is a
capability it HAS. Web search sat on the KB page because it was modelled as a
"connected source with a credential", which put `web_search` on a different
admin page from the browser - the one other read-only web capability - and gave
it a different per-project gate.

The gates are the whole risk of this migration, because they are OPPOSITES:

  - a KB reaches a build only if the project's `kb_ids` names it (opt-IN -
    `rag.project_kb_ids` resolves NULL to [], so a project that selected
    nothing gets nothing, whatever the global flag says);
  - a §Tools row reaches a build unless the project overrides it (opt-OUT -
    `ProjectToolConfig.enabled` is tri-state, NULL = inherit the global flag).

So moving a row without touching projects would silently GRANT search to every
project that never asked for it, and bill the consultant's provider key for it.
This writes the explicit overrides that keep every existing project on exactly
the behaviour it has today - and only when the source is actually live, because
a globally disabled provider reaches nobody either way and blanket overrides
would then just be noise an admin has to clear by hand later.

Revision ID: c9f4a1b7d203
Revises: 42c565bfb52c
Create Date: 2026-08-27
"""
import json
import os
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c9f4a1b7d203'
down_revision: Union[str, None] = '42c565bfb52c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same default as `settings.websearch_mcp_url`; read from the environment so a
# deployment that moved the sidecar migrates to its own URL. Admin-editable
# afterwards either way.
_SIDECAR = os.environ.get("WEBSEARCH_MCP_URL", "http://websearch-mcp:3000").rstrip("/")


def _as_list(value) -> list:
    """`kb_ids` comes back as a list, a JSON string or None depending on driver."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return list(value) if isinstance(value, list) else []


def upgrade() -> None:
    conn = op.get_bind()
    kbs = conn.execute(sa.text(
        "SELECT id, uri, name, enabled, api_key_enc FROM knowledge_base "
        "WHERE kind = 'websearch' ORDER BY sort_order, created_at"
    )).mappings().all()

    for kb in kbs:
        provider = (kb["uri"] or "").strip()
        if not provider:
            continue
        slug = f"websearch_{provider}"

        tool_id = conn.execute(sa.text("SELECT id FROM tool WHERE slug = :s"),
                               {"s": slug}).scalar()
        if tool_id is None:
            tool_id = str(uuid.uuid4())
            conn.execute(sa.text("""
                INSERT INTO tool (id, slug, name, kind, url, enabled, params,
                                  api_key_enc, created_at)
                VALUES (:id, :slug, :name, 'websearch', :url, :enabled,
                        CAST(:params AS json), :key, now())
            """), {
                "id": tool_id,
                "slug": slug,
                "name": kb["name"],
                # Fixed per provider, unlike the donsetch row whose path moves
                # with its capability toggles.
                "url": f"{_SIDECAR}/{provider}/mcp",
                "enabled": bool(kb["enabled"]),
                "params": json.dumps({"provider": provider}),
                "key": kb["api_key_enc"],
            })

        if kb["enabled"]:
            # Live source: every project's answer to "may I search?" is already
            # recorded in its kb_ids, and it must survive verbatim.
            for proj in conn.execute(sa.text("SELECT id, kb_ids FROM project")).mappings().all():
                chose = kb["id"] in _as_list(proj["kb_ids"])
                exists = conn.execute(sa.text(
                    "SELECT id FROM project_tool_config "
                    "WHERE project_id = :p AND tool_id = :t"
                ), {"p": proj["id"], "t": tool_id}).scalar()
                if exists:
                    continue
                conn.execute(sa.text("""
                    INSERT INTO project_tool_config (id, project_id, tool_id, enabled, created_at)
                    VALUES (:id, :p, :t, :enabled, now())
                """), {"id": str(uuid.uuid4()), "p": proj["id"], "t": tool_id,
                       "enabled": chose})

        # The selection now means nothing for this row - drop the dangling id so
        # no project keeps pointing at a knowledge base that no longer exists.
        for proj in conn.execute(sa.text(
            "SELECT id, kb_ids FROM project"
        )).mappings().all():
            ids = _as_list(proj["kb_ids"])
            if kb["id"] not in ids:
                continue
            conn.execute(sa.text("UPDATE project SET kb_ids = CAST(:v AS json) WHERE id = :p"),
                         {"v": json.dumps([i for i in ids if i != kb["id"]]), "p": proj["id"]})

    conn.execute(sa.text("DELETE FROM knowledge_base WHERE kind = 'websearch'"))
    op.drop_index("uq_kb_websearch_provider", table_name="knowledge_base")


def downgrade() -> None:
    op.create_index("uq_kb_websearch_provider", "knowledge_base", ["uri"],
                    unique=True, postgresql_where=sa.text("kind = 'websearch'"))
    conn = op.get_bind()
    tools = conn.execute(sa.text(
        "SELECT id, name, url, enabled, params, api_key_enc FROM tool "
        "WHERE kind = 'websearch' ORDER BY created_at"
    )).mappings().all()

    for order, tool in enumerate(tools, start=2):
        params = tool["params"]
        if isinstance(params, str):
            params = json.loads(params or "{}")
        provider = (params or {}).get("provider")
        if not provider:
            continue
        kb_id = str(uuid.uuid4())
        conn.execute(sa.text("""
            INSERT INTO knowledge_base (id, kind, name, enabled, uri, api_key_enc,
                                        is_removable, sort_order, verified, created_at)
            VALUES (:id, 'websearch', :name, :enabled, :uri, :key, false, :order, false, now())
        """), {"id": kb_id, "name": tool["name"], "enabled": bool(tool["enabled"]),
               "uri": provider, "key": tool["api_key_enc"], "order": order})

        # A project got this source iff its override says so (or, with no
        # override, iff the global flag was on) - put that back into kb_ids.
        for proj in conn.execute(sa.text("SELECT id, kb_ids FROM project")).mappings().all():
            ov = conn.execute(sa.text(
                "SELECT enabled FROM project_tool_config WHERE project_id = :p AND tool_id = :t"
            ), {"p": proj["id"], "t": tool["id"]}).scalar()
            effective = bool(tool["enabled"]) if ov is None else bool(ov)
            ids = _as_list(proj["kb_ids"])
            if not effective or kb_id in ids:
                continue
            conn.execute(sa.text("UPDATE project SET kb_ids = CAST(:v AS json) WHERE id = :p"),
                         {"v": json.dumps(ids + [kb_id]), "p": proj["id"]})

        conn.execute(sa.text("DELETE FROM project_tool_config WHERE tool_id = :t"),
                     {"t": tool["id"]})

    conn.execute(sa.text("DELETE FROM tool WHERE kind = 'websearch'"))
