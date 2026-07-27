#!/usr/bin/env python3
"""Run the complete uCoin-to-app catalogue workflow."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from ucoin_catalog import UCOIN_CATALOG_FILENAME, slugify


PROJECT_DIR = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run uCoin scrape and app catalogue generation in sequence.")
    parser.add_argument("country", help="Country to scrape, e.g. India or Canada.")
    parser.add_argument("period", nargs="?", default=None, help="Optional period filter passed to ucoin_catalog.py.")
    parser.add_argument("--start-year", type=int, help="Keep only coins whose issue start year is at least this year.")
    parser.add_argument("--output-dir", default="", help="Destination folder. Defaults to the country slug.")
    parser.add_argument("--catalog-output", default="", help="Explicit ucoin-catalog.json output path.")
    parser.add_argument("--attach-cdp", action="store_true", help="Use an already-open Chromium/Chrome CDP session.")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222", help="CDP URL used with --attach-cdp.")
    parser.add_argument("--manual-session", action="store_true", help="Pause for manual Cloudflare/login confirmation.")
    parser.add_argument("--no-manual-session", action="store_true", help="Do not pause before scraping.")
    parser.add_argument("--headless", action="store_true", help="Pass --headless to ucoin_catalog.py.")
    parser.add_argument("--timeout", type=int, default=60, help="Timeout in seconds passed to ucoin_catalog.py.")
    parser.add_argument("--max-pages", type=int, default=50, help="Maximum catalogue pages to scrape.")
    parser.add_argument("--retries", type=int, default=2, help="Retries per temporary page failure.")
    parser.add_argument("--sequential-fallback", action="store_true", help="Try page=N+1 if pagination links are incomplete.")
    parser.add_argument(
        "--no-wait-for-final",
        action="store_true",
        help="Only create app-catalog-pending.json and do not wait for app-catalog-final.json.",
    )
    return parser.parse_args()


def default_catalog_path(args: argparse.Namespace) -> Path:
    if args.catalog_output:
        return Path(args.catalog_output)
    output_dir = Path(args.output_dir) if args.output_dir else Path(slugify(args.country))
    return output_dir / UCOIN_CATALOG_FILENAME


def build_scrape_command(args: argparse.Namespace) -> list[str]:
    command = [sys.executable, str(PROJECT_DIR / "ucoin_catalog.py"), args.country]
    if args.period:
        command.append(args.period)
    command.extend(["--json", "--timeout", str(args.timeout), "--max-pages", str(args.max_pages), "--retries", str(args.retries)])
    if args.start_year is not None:
        command.extend(["--start-year", str(args.start_year)])
    if args.output_dir:
        command.extend(["--output-dir", args.output_dir])
    if args.catalog_output:
        command.extend(["--output", args.catalog_output])
    if args.attach_cdp:
        command.extend(["--attach-cdp", "--cdp-url", args.cdp_url])
    if args.manual_session:
        command.append("--manual-session")
    if args.no_manual_session:
        command.append("--no-manual-session")
    if args.headless:
        command.append("--headless")
    if args.sequential_fallback:
        command.append("--sequential-fallback")
    return command


def build_generate_command(args: argparse.Namespace, catalog_path: Path) -> list[str]:
    command = [sys.executable, str(PROJECT_DIR / "generate_resume_json.py"), "--input", str(catalog_path)]
    if not args.no_wait_for_final:
        command.append("--wait-for-final")
    return command


def run_step(name: str, command: list[str]) -> None:
    print(f"\n== {name} ==")
    print(" ".join(command))
    subprocess.run(command, cwd=PROJECT_DIR, check=True)


def main() -> int:
    args = parse_args()
    catalog_path = default_catalog_path(args)
    try:
        run_step("Scrape uCoin catalogue", build_scrape_command(args))
        run_step("Generate app catalogue", build_generate_command(args, catalog_path))
    except subprocess.CalledProcessError as exc:
        return exc.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())