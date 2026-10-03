import argparse
import os
import uvicorn

from app.core.logging import attach_runtime_log_file, setup_logging
from app.core.settings import get_settings, init_workspace


def main():
    parser = argparse.ArgumentParser(description="麦林 Mailin 后端")
    sub = parser.add_subparsers(dest="command")

    serve_parser = sub.add_parser("serve", help="启动 API 服务")
    serve_parser.add_argument("--host", default=None)
    serve_parser.add_argument("--port", type=int, default=None)
    serve_parser.add_argument("--reload", action="store_true")
    serve_parser.add_argument("--mode", choices=("development", "production"), default=None)
    serve_parser.add_argument("--runtime-root", default=None)
    serve_parser.add_argument("--resources-dir", default=None)

    migrate_parser = sub.add_parser("migrate", help="一次性迁移旧运行数据")
    migrate_parser.add_argument("--runtime-root", required=True)
    migrate_parser.add_argument("--resources-dir", required=True)
    migrate_parser.add_argument("--legacy-data-root", required=True)
    migrate_parser.add_argument("--legacy-cache-dir")
    migrate_parser.add_argument("--legacy-temp-dir")
    migrate_parser.add_argument("--legacy-log-dir")

    args = parser.parse_args()
    for env_name, value in {
        "MAILIN_MODE": getattr(args, "mode", None),
        "MAILIN_RUNTIME_ROOT": getattr(args, "runtime_root", None),
        "MAILIN_RESOURCES_DIR": getattr(args, "resources_dir", None),
        "MAILIN_HOST": getattr(args, "host", None),
        "MAILIN_PORT": str(getattr(args, "port", None)) if getattr(args, "port", None) is not None else None,
    }.items():
        if value is not None:
            os.environ[env_name] = str(value)

    settings = get_settings()
    if args.command == "migrate":
        from pathlib import Path
        from app.core.runtime_migration import migrate_legacy_layout

        sources = {"data": Path(args.legacy_data_root)}
        for key, value in (("cache", args.legacy_cache_dir), ("tmp", args.legacy_temp_dir), ("log", args.legacy_log_dir)):
            if value:
                sources[key] = Path(value)
        migrate_legacy_layout(settings.layout, legacy_roots=sources)
        return
    setup_logging(level=settings.log_level, log_format=settings.log_format)
    init_workspace(settings)
    attach_runtime_log_file(settings.log_dir)

    host = getattr(args, "host", None) or settings.host
    port = getattr(args, "port", None)
    if port is None:
        port = 0 if settings.mode == "production" else settings.port
    reload = getattr(args, "reload", False)

    uvicorn.run("app.main:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    main()
