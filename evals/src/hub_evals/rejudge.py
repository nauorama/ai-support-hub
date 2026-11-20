"""Judge a finished run again with the active judge prompts (P7-06): `make eval-rejudge RUN=<dir>`.

Only judge calls: the stored answers of `<dir>/results.jsonl` are judged again, as the runner
judges them (`judge_ticket`, the same evidence and cost limit), and written with their metrics
and report to a new directory `<dir>-rejudged` (or `--out`). The source run is never changed. Its
`config.json` is copied with the new judge prompt versions, so the report says which judges
counted. A RAG run only: B0 judges cited documents read from the database at run time.

Judge calls go through the gateway with context `eval`; the disk cache serves a judge request
that did not change (the key-facts judge) for free. `EVAL_MAX_COST_USD` holds the paid calls.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from hub.config import SettingsError, load_settings
from hub.db.session import connect
from hub_evals import judges as judging
from hub_evals import results as files
from hub_evals.runner import (
    CACHE_DIR,
    TICKETS_FILE,
    EvalSetupError,
    Report,
    _finish,
    build_pipeline,
    eval_database_url,
    judge_ticket,
    load_tickets,
)

SUFFIX = "-rejudged"


async def rejudge(source: Path, out: Path, tickets_file: Path = TICKETS_FILE) -> Decimal:
    """Judge the answers of `source` again into `out`; returns what the judges paid."""
    settings = load_settings()
    stored = files.read_config(source / files.CONFIG_FILE)
    if stored is None:
        raise EvalSetupError(f"{source.name} has no config.json")
    if stored.variant == "b0_full_kb":
        raise EvalSetupError("a B0 run cannot be judged again (its sources are not stored)")
    results = files.read_results(source / files.RESULTS_FILE)
    tickets = load_tickets(tickets_file)
    by_id = {ticket.id: ticket for ticket in tickets}
    database = connect(eval_database_url(settings))
    try:
        pipeline, limit, cache = build_pipeline(
            settings, database, variant=stored.variant, cache_dir=CACHE_DIR, tools=False
        )
        judge_set = judging.load_judges(settings.prompts_dir, pipeline.gateway.models)
        config = stored.model_copy(update={"judge_prompt_versions": judge_set.prompt_versions})
        out.mkdir(parents=True, exist_ok=True)
        results_path = out / files.RESULTS_FILE
        judged: list[files.TicketResult] = []
        paid = Decimal(0)
        for result in results:
            ticket = by_id.get(result.ticket_id)
            if ticket is None or result.draft is None:
                judged.append(result)
                continue
            verdicts = await judge_ticket(
                pipeline.gateway, judge_set, ticket, result.draft, {}, limit
            )
            paid += verdicts.paid_cost_usd
            judged.append(result.model_copy(update={"judges": verdicts}))
        files.rewrite_results(results_path, judged)
        files.write_config(out / files.CONFIG_FILE, config)
        report = Report(run_dir=out, planned=len(tickets), skipped=0, ran=0)
        _finish(out, config, tickets, results_path, report, cache)
        await pipeline.checkpoints.close()
    finally:
        await database.dispose()
    return paid


def main(argv: Sequence[str]) -> int:
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="hub_evals.rejudge", description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(list(argv))
    source: Path = args.run_dir
    out: Path = args.out or source.with_name(source.name + SUFFIX)
    try:
        paid = asyncio.run(rejudge(source, out))
    except (EvalSetupError, SettingsError) as error:
        print(f"eval-rejudge: {error}", file=sys.stderr)
        return 1
    print(f"eval-rejudge: {out}; judges paid ${paid:.6f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
