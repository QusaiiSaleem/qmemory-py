"""Admin CLI: status, create-db, create-user, list-users."""

from __future__ import annotations

import asyncio

import click

from qmemory.db.client import (
    apply_admin_schema,
    get_admin_db,
    is_healthy,
    query,
)
from qmemory.db.provision import provision_user_db


@click.group("admin")
def admin_group() -> None:
    """Administer the qmemory multi-user deployment."""


@admin_group.command("status")
def status_cmd() -> None:
    """Print admin DB status and row count."""

    async def _run() -> None:
        healthy = await is_healthy()
        click.echo(f"SurrealDB: {'healthy' if healthy else 'UNREACHABLE'}")
        if not healthy:
            return

        async with get_admin_db() as admin:
            try:
                await apply_admin_schema(admin)
            except Exception as exc:
                click.echo(f"WARNING: could not apply admin schema: {exc}")
            rows = await query(admin, "SELECT count() FROM user GROUP ALL")
        total = (rows[0]["count"] if rows else 0) if rows else 0
        click.echo(f"Admin DB: {total} users")

    asyncio.run(_run())


@admin_group.command("create-db")
@click.option("--name", required=True, help="User code (DB will be 'user_{name}')")
def create_db_cmd(name: str) -> None:
    """Create a user database with schema. No admin row created."""

    async def _run() -> None:
        db_name = await provision_user_db(name)
        click.echo(f"Provisioned database: {db_name}")
        click.echo(
            "Next: qmemory admin create-user "
            f"--user-code {name} --display-name '...' --db-name {db_name}"
        )

    asyncio.run(_run())


@admin_group.command("create-user")
@click.option("--user-code", required=True, help="user_code for the URL")
@click.option("--display-name", required=True, help="Human-friendly name")
@click.option("--db-name", required=True, help="Database name")
def create_user_cmd(user_code: str, display_name: str, db_name: str) -> None:
    """Insert a row linking user_code to db_name."""

    async def _run() -> None:
        async with get_admin_db() as admin:
            await apply_admin_schema(admin)
            existing = await query(
                admin,
                "SELECT id FROM user WHERE user_code = $code",
                {"code": user_code},
            )
            if existing:
                raise click.ClickException(f"user_code {user_code!r} already exists")

            await query(
                admin,
                """CREATE user SET
                    user_code = $code,
                    display_name = $name,
                    db_name = $db_name,
                    is_active = true""",
                {"code": user_code, "name": display_name, "db_name": db_name},
            )
        click.echo(f"Created user: {user_code} -> {db_name}")

    asyncio.run(_run())


@admin_group.command("list-users")
def list_users_cmd() -> None:
    """Print all rows in the admin user table."""

    async def _run() -> None:
        async with get_admin_db() as admin:
            rows = await query(
                admin,
                "SELECT user_code, display_name, db_name, is_active, last_active_at FROM user",
            )
        if not rows:
            click.echo("(no users)")
            return
        for row in rows:
            active = "active" if row.get("is_active") else "DISABLED"
            last = row.get("last_active_at") or "(never)"
            click.echo(
                f"  {row['user_code']:<20} {row['display_name']:<24} "
                f"{row['db_name']:<24} {active:<10} last_active={last}"
            )

    asyncio.run(_run())
