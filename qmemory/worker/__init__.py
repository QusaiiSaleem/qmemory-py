"""
Qmemory Background Worker — runs linker, reflector, and salience decay.

Self-scheduling: runs frequently when there's work, backs off when idle.
Token-budgeted: won't exceed hourly LLM token limits.
Pausable: stops processing if ~/.qmemory/worker-paused file exists.

Usage:
    python -m qmemory.worker        # direct
    qmemory worker                  # via CLI
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# --- Worker configuration ---
# Where to look for the "pause" signal file.
# If this file exists, the worker sleeps instead of running cycles.
PAUSE_FILE = Path.home() / ".qmemory" / "worker-paused"

# How long to wait between cycles (in seconds).
# When work is found, we check again sooner (5 min).
# When idle, we back off to save resources (30 min).
ACTIVE_INTERVAL = 300      # 5 min when work found
IDLE_INTERVAL = 1800       # 30 min when no work

# The reflector is more expensive than the linker, so we stagger it.
# It only runs every Nth cycle (e.g. every 2nd cycle).
REFLECTOR_EVERY_N = 2      # Run reflector every Nth cycle


async def run_worker():
    """
    Main worker loop — self-scheduling, token-budgeted, pausable.

    This runs forever (until Ctrl+C). Each cycle:
      1. Check if paused (via PAUSE_FILE)
      2. Run the linker (finds relationships between memories)
      3. Run salience decay (fades old memories — zero LLM cost)
      4. Every Nth cycle, run the reflector (finds patterns/contradictions)
      5. Sleep for ACTIVE_INTERVAL (5 min) if work was found,
         or IDLE_INTERVAL (30 min) if idle
    """
    from qmemory.core.token_budget import init_token_budget

    # Initialize the token budget — controls how many LLM tokens
    # the worker can spend per hour. "balanced" = 80k tokens/hour.
    init_token_budget("balanced")
    cycle = 0

    logger.info(
        "worker.started active_interval=%ds idle_interval=%ds",
        ACTIVE_INTERVAL,
        IDLE_INTERVAL,
    )

    while True:
        # --- Pause check ---
        # If the pause file exists, skip this cycle and check again in 60s.
        # To pause:  touch ~/.qmemory/worker-paused
        # To resume: rm ~/.qmemory/worker-paused
        if PAUSE_FILE.exists():
            logger.debug("worker.paused file=%s", PAUSE_FILE)
            await asyncio.sleep(60)
            continue

        cycle += 1
        cycle_start = time.monotonic()
        found_work = False

        try:
            # --- 1. Linker (every cycle) ---
            # Finds relationships between unlinked memories using a cheap LLM.
            # Returns {"found_work": True/False, "processed": N, "edges_created": N}
            from qmemory.core.linker import run_linker_cycle

            linker_result = await run_linker_cycle()
            if linker_result.get("found_work"):
                found_work = True
            logger.info("worker.linker cycle=%d result=%s", cycle, linker_result)

            # --- 2. Decay (piggybacks on linker, zero LLM cost) ---
            # Fades old memories' salience scores. Pure DB operation.
            # Returns {"tier1_decayed": N, "tier2_decayed": N, "tier3_enforced": N}
            from qmemory.core.decay import run_salience_decay

            decay_result = await run_salience_decay()
            logger.info("worker.decay cycle=%d result=%s", cycle, decay_result)

            # --- 3. Reflector (staggered — every other cycle) ---
            # More expensive: finds patterns, contradictions, compressions.
            # Only runs every REFLECTOR_EVERY_N cycles to save tokens.
            if cycle % REFLECTOR_EVERY_N == 0:
                from qmemory.core.reflector import run_reflector_cycle

                reflect_result = await run_reflector_cycle()
                if reflect_result.get("found_work"):
                    found_work = True
                logger.info(
                    "worker.reflector cycle=%d result=%s", cycle, reflect_result
                )

        except Exception:
            logger.exception("worker.cycle_error cycle=%d", cycle)

        # --- Timing and scheduling ---
        elapsed = (time.monotonic() - cycle_start) * 1000
        interval = ACTIVE_INTERVAL if found_work else IDLE_INTERVAL

        logger.info(
            "worker.cycle_done cycle=%d found_work=%s elapsed_ms=%.0f next_in=%ds",
            cycle,
            found_work,
            elapsed,
            interval,
        )

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
