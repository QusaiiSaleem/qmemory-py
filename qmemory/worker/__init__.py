"""
Qmemory Background Worker — maintains graph health automatically.

Runs 5 jobs per cycle:
  1. Linker     — finds and creates edges between unlinked memories
  2. Dedup      — finds and merges duplicate memories
  3. Decay      — fades old memories' salience scores
  4. Reflector  — finds patterns, contradictions, ghost entities
  5. Linter     — 6 health checks (orphans, stale, gaps, quality)

After all jobs, saves a health report to the database.

Default: runs once per day (86400s). Use --once for single run.
Pausable: touch ~/.qmemory/worker-paused to pause.
Token-budgeted: respects hourly LLM token limits.

Usage:
    qmemory worker                  # once per day
    qmemory worker --interval 3600  # every hour
    qmemory worker --once           # run once and exit
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from qmemory.db.client import _user_db, get_admin_db, query

logger = logging.getLogger(__name__)

# Where to look for the "pause" signal file.
# If this file exists, the worker sleeps instead of running cycles.
PAUSE_FILE = Path.home() / ".qmemory" / "worker-paused"

# Default interval between cycles: once per day (86400 seconds).
DEFAULT_INTERVAL = 86400

# The reflector is more expensive than other jobs, so we stagger it.
# It only runs every Nth cycle (e.g. every 2nd cycle).
REFLECTOR_EVERY_N = 2

# Admin database name for iterating users. Overridable via env/tests.
_ADMIN_DB_NAME: str = "admin"


async def _iter_active_user_dbs():
    """Yield (user_code, db_name) for every active user in admin DB."""
    async with get_admin_db(database=_ADMIN_DB_NAME) as admin:
        rows = await query(
            admin,
            "SELECT user_code, db_name FROM user WHERE is_active = true",
        )
    if not rows:
        return
    for row in rows:
        yield row["user_code"], row["db_name"]


async def run_worker(
    interval: int = DEFAULT_INTERVAL,
    once: bool = False,
    all_users: bool = False,
):
    """
    Main worker loop — runs all maintenance jobs, saves health report.

    Args:
        interval:  Seconds between cycles. Default: 86400 (once per day).
        once:      If True, run one cycle and exit (for testing/cron).
        all_users: If True, iterate every active user in the admin DB and
                   run one maintenance cycle against each of their private
                   databases. If False, run once against the default DB
                   (QMEMORY_SURREAL_DB env var).
    """
    from qmemory.core.token_budget import init_token_budget

    # Initialize the token budget — controls how many LLM tokens
    # the worker can spend per hour. "balanced" = 80k tokens/hour.
    init_token_budget("balanced")
    cycle = 0

    logger.info(
        "worker.started interval=%ds once=%s all_users=%s",
        interval,
        once,
        all_users,
    )

    while True:
        # --- Pause check ---
        # If the pause file exists, skip this cycle and check again in 60s.
        # To pause:  touch ~/.qmemory/worker-paused
        # To resume: rm ~/.qmemory/worker-paused
        if PAUSE_FILE.exists():
            logger.debug("worker.paused file=%s", PAUSE_FILE)
            if once:
                logger.info("worker.paused and --once set, exiting")
                return
            await asyncio.sleep(60)
            continue

        cycle += 1

        if all_users:
            user_count = 0
            async for user_code, db_name in _iter_active_user_dbs():
                user_count += 1
                token = _user_db.set(db_name)
                try:
                    logger.info(
                        "worker.user_cycle_start cycle=%d user=%s db=%s",
                        cycle, user_code, db_name,
                    )
                    await _run_one_cycle(cycle)
                    logger.info(
                        "worker.user_cycle_done cycle=%d user=%s",
                        cycle, user_code,
                    )
                except Exception:
                    logger.exception(
                        "worker.user_cycle_error cycle=%d user=%s",
                        cycle, user_code,
                    )
                finally:
                    _user_db.reset(token)
            logger.info("worker.cycle_summary cycle=%d users_processed=%d", cycle, user_count)
        else:
            try:
                await _run_one_cycle(cycle)
            except Exception:
                logger.exception("worker.cycle_error cycle=%d", cycle)

        # Exit if --once
        if once:
            logger.info("worker.once_done exiting")
            return

        await asyncio.sleep(interval)


async def _run_one_cycle(cycle: int) -> None:
    """Run the 5-job maintenance cycle against whichever DB _user_db points at.

    Accumulates findings and saves one health_report row at the end.
    """
    cycle_start = time.monotonic()

    all_findings: list[dict] = []
    links_created = 0
    dupes_merged = 0
    contradictions_found = 0

    try:
        # --- Job 1: Linker (every cycle) ---
        from qmemory.core.linker import run_linker_cycle

        linker_result = await run_linker_cycle()
        links_created = linker_result.get("edges_created", 0)
        logger.info("worker.linker cycle=%d result=%s", cycle, linker_result)

        # --- Job 2: Dedup (every cycle) ---
        from qmemory.core.dedup_worker import run_dedup_cycle

        dedup_result = await run_dedup_cycle()
        dupes_merged = dedup_result.get("dupes_merged", 0)
        logger.info("worker.dedup cycle=%d result=%s", cycle, dedup_result)

        # --- Job 3: Decay (every cycle, zero LLM cost) ---
        from qmemory.core.decay import run_salience_decay

        decay_result = await run_salience_decay()
        logger.info("worker.decay cycle=%d result=%s", cycle, decay_result)

        # --- Job 4: Reflector (staggered) ---
        if cycle % REFLECTOR_EVERY_N == 0:
            from qmemory.core.reflector import run_reflector_cycle

            reflect_result = await run_reflector_cycle()
            contradictions_found = reflect_result.get("contradictions", 0)
            logger.info(
                "worker.reflector cycle=%d result=%s", cycle, reflect_result
            )

        # --- Job 5: Linter (every cycle) ---
        from qmemory.core.linter import run_linter_checks

        linter_findings = await run_linter_checks()
        all_findings.extend(linter_findings)
        logger.info(
            "worker.linter cycle=%d findings=%d", cycle, len(linter_findings)
        )

    except Exception:
        logger.exception("worker.cycle_error cycle=%d", cycle)

    # --- Save health report ---
    elapsed_ms = int((time.monotonic() - cycle_start) * 1000)

    orphans = len([f for f in all_findings if f["check"] == "orphan"])
    stale = len([f for f in all_findings if f["check"] == "stale"])
    gaps = [
        f["node_id"].split(":")[-1]
        for f in all_findings
        if f["check"] == "gap"
    ]
    quality = len([f for f in all_findings if f["check"] == "quality"])

    # Add linker/dedup findings to the report
    if links_created > 0:
        all_findings.append({
            "check": "linker",
            "severity": "info",
            "node_id": "worker:linker",
            "detail": f"Linker created {links_created} new edges",
            "action": None,
            "fixed": True,
        })
    if dupes_merged > 0:
        all_findings.append({
            "check": "dedup",
            "severity": "info",
            "node_id": "worker:dedup",
            "detail": f"Dedup merged {dupes_merged} duplicate memories",
            "action": None,
            "fixed": True,
        })

    try:
        from qmemory.core.health import save_health_report

        await save_health_report(
            orphans_found=orphans,
            contradictions_found=contradictions_found,
            stale_found=stale,
            links_created=links_created,
            dupes_merged=dupes_merged,
            gaps=gaps,
            quality_issues=quality,
            findings=all_findings,
            duration_ms=elapsed_ms,
        )
    except Exception:
        logger.exception("worker.report_save_error cycle=%d", cycle)

    logger.info(
        "worker.cycle_done cycle=%d elapsed_ms=%d findings=%d",
        cycle,
        elapsed_ms,
        len(all_findings),
    )


def main():
    """Entry point for `python -m qmemory.worker`."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        logger.info("worker.stopped reason=keyboard_interrupt")
