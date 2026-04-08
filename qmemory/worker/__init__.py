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

logger = logging.getLogger(__name__)

# Where to look for the "pause" signal file.
# If this file exists, the worker sleeps instead of running cycles.
PAUSE_FILE = Path.home() / ".qmemory" / "worker-paused"

# Default interval between cycles: once per day (86400 seconds).
DEFAULT_INTERVAL = 86400

# The reflector is more expensive than other jobs, so we stagger it.
# It only runs every Nth cycle (e.g. every 2nd cycle).
REFLECTOR_EVERY_N = 2


async def run_worker(interval: int = DEFAULT_INTERVAL, once: bool = False):
    """
    Main worker loop — runs all maintenance jobs, saves health report.

    Args:
        interval: Seconds between cycles. Default: 86400 (once per day).
        once:     If True, run one cycle and exit (for testing/cron).
    """
    from qmemory.core.token_budget import init_token_budget

    # Initialize the token budget — controls how many LLM tokens
    # the worker can spend per hour. "balanced" = 80k tokens/hour.
    init_token_budget("balanced")
    cycle = 0

    logger.info(
        "worker.started interval=%ds once=%s",
        interval,
        once,
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
        cycle_start = time.monotonic()

        # Accumulators for the health report
        all_findings: list[dict] = []
        links_created = 0
        dupes_merged = 0
        contradictions_found = 0

        try:
            # --- Job 1: Linker (every cycle) ---
            # Finds relationships between unlinked memories using a cheap LLM.
            from qmemory.core.linker import run_linker_cycle

            linker_result = await run_linker_cycle()
            links_created = linker_result.get("edges_created", 0)
            logger.info("worker.linker cycle=%d result=%s", cycle, linker_result)

            # --- Job 2: Dedup (every cycle) ---
            # Finds and merges duplicate memories missed by save-time dedup.
            from qmemory.core.dedup_worker import run_dedup_cycle

            dedup_result = await run_dedup_cycle()
            dupes_merged = dedup_result.get("dupes_merged", 0)
            logger.info("worker.dedup cycle=%d result=%s", cycle, dedup_result)

            # --- Job 3: Decay (every cycle, zero LLM cost) ---
            # Fades old memories' salience scores. Pure DB operation.
            from qmemory.core.decay import run_salience_decay

            decay_result = await run_salience_decay()
            logger.info("worker.decay cycle=%d result=%s", cycle, decay_result)

            # --- Job 4: Reflector (staggered — every other cycle) ---
            # More expensive: finds patterns, contradictions, compressions.
            if cycle % REFLECTOR_EVERY_N == 0:
                from qmemory.core.reflector import run_reflector_cycle

                reflect_result = await run_reflector_cycle()
                contradictions_found = reflect_result.get("contradictions", 0)
                logger.info(
                    "worker.reflector cycle=%d result=%s", cycle, reflect_result
                )

            # --- Job 5: Linter (every cycle) ---
            # Runs 4 health checks: orphans, stale, gaps, quality.
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

        # Exit if --once
        if once:
            logger.info("worker.once_done exiting")
            return

        await asyncio.sleep(interval)


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
