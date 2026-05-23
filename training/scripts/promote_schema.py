"""Promote approved ColumnAliasProposals into a new SchemaVersion.

Workflow:
  1. Fetch all 'approved' ColumnAliasProposal records
  2. Merge their suggested_field → unmatched_header mappings into the current
     active SchemaVersion's content
  3. Bump the minor version number (1.0 → 1.1 → 1.2 …)
  4. Insert a new SchemaVersion row and set is_active=True
  5. Deactivate the old SchemaVersion
  6. Mark proposals as 'promoted'

After this runs, the column_mapper will pick up the new aliases within
OCR_SCHEMA_TTL seconds (default 15 min) without any worker restart.

Usage:
    python training/scripts/promote_schema.py \
        --changelog "Added: 'Giá trị' → unit_price, 'Số lượng hàng' → quantity"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

from einv_common.db import session_factory
from einv_common.models.training import ColumnAliasProposal, SchemaVersion
from sqlalchemy import select, update


def bump_minor(version: str) -> str:
    """'1.0' → '1.1', '1.9' → '1.10', '2.3' → '2.4'"""
    parts = version.split(".")
    if len(parts) >= 2:
        try:
            parts[-1] = str(int(parts[-1]) + 1)
            return ".".join(parts)
        except ValueError:
            pass
    return version + ".1"


async def run(args: argparse.Namespace) -> None:
    async with session_factory() as session:
        # ── Fetch approved proposals ─────────────────────────────────────────
        result = await session.execute(
            select(ColumnAliasProposal)
            .where(ColumnAliasProposal.status == "approved")
            .where(ColumnAliasProposal.suggested_field.is_not(None))
        )
        proposals = list(result.scalars().all())

        if not proposals:
            print("[promote_schema] no approved proposals found — nothing to do")
            return

        print(f"[promote_schema] {len(proposals)} approved proposals to merge")

        # ── Load current active schema ───────────────────────────────────────
        result = await session.execute(
            select(SchemaVersion)
            .where(SchemaVersion.is_active == True)    # noqa: E712
            .limit(1)
        )
        current = result.scalar_one_or_none()

        if current:
            content = dict(current.content)
            new_version = bump_minor(current.version)
        else:
            # Bootstrap from label_schema.json
            schema_path = Path(__file__).parent.parent / "annotation" / "label_schema.json"
            content = json.loads(schema_path.read_text(encoding="utf-8"))
            new_version = "1.1"

        # ── Merge proposals into content ─────────────────────────────────────
        aliases = (
            content
            .setdefault("doc_types", {})
            .setdefault("vat_invoice", {})
            .setdefault("column_aliases", {})
        )

        added: list[str] = []
        for prop in proposals:
            field = prop.suggested_field
            header = prop.unmatched_header
            if field not in aliases:
                aliases[field] = []
            if header not in aliases[field]:
                aliases[field].append(header)
                added.append(f"{header!r} → {field}")

        # Also bump the schema's own version field
        content["version"] = new_version

        print("  Merged aliases:")
        for line in added:
            print(f"    + {line}")

        # ── Deactivate old version ───────────────────────────────────────────
        if current:
            await session.execute(
                update(SchemaVersion)
                .where(SchemaVersion.id == current.id)
                .values(is_active=False)
            )

        # ── Insert new version ───────────────────────────────────────────────
        new_sv = SchemaVersion(
            version=new_version,
            content=content,
            changelog=args.changelog or f"Auto-promote: {', '.join(added)}",
            is_active=True,
            activated_at=datetime.now(timezone.utc),
        )
        session.add(new_sv)

        # ── Mark proposals as promoted ───────────────────────────────────────
        prop_ids = [p.id for p in proposals]
        await session.execute(
            update(ColumnAliasProposal)
            .where(ColumnAliasProposal.id.in_(prop_ids))
            .values(status="promoted")
        )

        await session.commit()
        print(f"\n[promote_schema] SchemaVersion {new_version} is now active")
        print(f"  column_mapper will reload within OCR_SCHEMA_TTL seconds")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--changelog", default=None, help="Human-readable summary of changes")
    args = parser.parse_args()
    asyncio.run(run(args))
