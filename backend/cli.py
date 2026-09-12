"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser

from . import config


def main() -> int:
    parser = argparse.ArgumentParser(prog="livebound")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Start the server and open a browser")
    serve.add_argument("--host", default=config.DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    serve.add_argument("--no-browser", action="store_true")
    serve.add_argument("--reload", action="store_true")

    scan_parser = sub.add_parser("scan", help="Walk every source folder once")
    scan_parser.add_argument(
        "--full", action="store_true", help="re-read every image regardless of ingest revision"
    )
    sub.add_parser("resume", help="Clean up interrupted runs and report")

    backup_parser = sub.add_parser("backup", help="Back the database up, or list existing backups")
    backup_parser.add_argument("--list", action="store_true", help="show existing backups")
    backup_parser.add_argument("--reason", default="manual")

    starter_parser = sub.add_parser("starter", help="Evaluate platform starter decisions")
    starter_checks = starter_parser.add_subparsers(dest="starter_check", required=True)
    python_check = starter_checks.add_parser("python", help="Check this interpreter's version")
    python_check.add_argument("--interpreter", default=sys.executable)
    python_check.add_argument("--environment", default="its environment")

    frontend_check = starter_checks.add_parser("frontend", help="Plan frontend setup")
    frontend_check.add_argument("--npm", action="store_true")
    frontend_check.add_argument("--dev", action="store_true")
    frontend_check.add_argument("--update", action="store_true")
    frontend_check.add_argument("--skip-build", action="store_true")
    frontend_check.add_argument(
        "--decision", choices=("dependencies", "build"), help=argparse.SUPPRESS
    )

    dependencies_check = starter_checks.add_parser(
        "dependencies", help="Check frontend dependency freshness"
    )
    dependencies_check.add_argument("--launcher", required=True)

    args = parser.parse_args()
    command = args.command or "serve"

    if command == "serve":
        return _serve(args)
    if command == "scan":
        return _scan(args)
    if command == "resume":
        return _resume()
    if command == "backup":
        return _backup(args)
    if command == "starter":
        return _starter(args)
    parser.print_help()
    return 1


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from . import security

    url = f"http://{args.host}:{args.port}/"
    print(f"Livebound: {url}")
    print(f"Data: {config.data_dir()}")
    print(f"Diagnostics: {config.diagnostic_log_path().resolve()}")

    warning = security.warn_if_exposed(args.host)
    if warning:
        print(f"\n  {warning}\n")

    if not args.no_browser and not args.reload:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        "backend.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


def _starter(args: argparse.Namespace) -> int:
    from . import starter

    if args.starter_check == "python":
        if starter.python_is_supported(sys.version_info):
            return 0
        found = sys.version.split()[0] if sys.version else "unavailable"
        _starter_error(
            f"{args.interpreter} must be Python {starter.python_floor_label()} or newer "
            f"(found Python {found}). Remove {args.environment} and rerun, or install uv."
        )
        return 1

    if args.starter_check == "frontend":
        plan = starter.frontend_plan(
            config.project_root(),
            npm_available=args.npm,
            dev=args.dev,
            update=args.update,
            skip_build=args.skip_build,
        )
        if args.decision == "dependencies":
            return 0 if plan.install_dependencies else 1
        if args.decision == "build":
            return 0 if plan.build else 1
        if plan.warning:
            _starter_warning(plan.warning)
        if plan.error:
            _starter_error(plan.error)
            return 1
        return 0

    warning = starter.dependency_warning(config.project_root(), args.launcher)
    if warning:
        _starter_warning(warning)
    return 0


def _starter_warning(message: str) -> None:
    print(f"\033[33m::\033[0m {message}", file=sys.stderr)


def _starter_error(message: str) -> None:
    print(f"\033[31m::\033[0m {message}", file=sys.stderr)


def _scan(args: argparse.Namespace) -> int:
    from . import db, jobs
    from .scanner import service

    db.init_db()
    job = jobs.Job(id=0, kind="scan")
    result = service.scan_roots(job, full=args.full)
    print(f"Scanned: {result}")
    return 0


def _backup(args: argparse.Namespace) -> int:
    from . import backup, db

    db.init_db()
    if args.list:
        items = backup.list_backups()
        if not items:
            print("No backups yet.")
            return 0
        print(f"{len(items)} backup(s) in {backup.backup_dir()}:")
        for item in items:
            posts = (item["contents"] or {}).get("posts", "?")
            size = item["size"] / 1024**2
            print(f"  {item['created_at']}  {size:6.1f} MB  {posts:>4} Posts  {item['name']}")
        return 0

    result = backup.create(reason=args.reason)
    print(f"Backed up: {result['path']} ({result['size'] / 1024**2:.1f} MB)")
    # `backup.create` strips every credential, so the useful thing to say is not
    # a warning but what the user will have to do after restoring one.
    print("The backup carries no credentials: restoring it means connecting to CivitAI again.")
    return 0


def _resume() -> int:
    from . import db
    from .store import runs

    db.init_db()
    print(runs.recover_interrupted())
    return 0


if __name__ == "__main__":
    sys.exit(main())
