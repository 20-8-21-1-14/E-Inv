"""Management CLI for bootstrapping the admin-api.

Usage:
    python -m app.management create-superuser \\
        --email admin@company.com --password <strong-password>

Run this once after the first Alembic migration to create the initial super_admin.
If a super_admin with that email already exists the command is a no-op.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from einv_common.db import session_factory
from einv_common.models.user import AdminUser
from app.auth_utils import hash_password


async def _create_superuser(email: str, password: str) -> None:
    async with session_factory() as session:
        existing = (await session.execute(
            select(AdminUser).where(AdminUser.email == email)
        )).scalar_one_or_none()
        if existing is not None:
            print(f"[management] user {email!r} already exists (role={existing.role}). No-op.")
            return

        user = AdminUser(
            email=email,
            password_hash=hash_password(password),
            role="super_admin",
            is_active=True,
        )
        session.add(user)
        await session.commit()
        print(f"[management] super_admin created: {email}")


def main() -> None:
    parser = argparse.ArgumentParser(description="e-Invoice admin management CLI")
    sub = parser.add_subparsers(dest="command")

    create = sub.add_parser("create-superuser", help="Create the initial super_admin user")
    create.add_argument("--email",    required=True)
    create.add_argument("--password", required=True)

    args = parser.parse_args()
    if args.command == "create-superuser":
        asyncio.run(_create_superuser(args.email, args.password))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
