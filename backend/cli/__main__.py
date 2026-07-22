import argparse
import uvicorn

from app.core.logging import setup_logging
from app.core.settings import get_settings, init_workspace


def main():
    parser = argparse.ArgumentParser(description="麦林 Mailin 后端")
    sub = parser.add_subparsers(dest="command")

    serve_parser = sub.add_parser("serve", help="启动 API 服务")
    serve_parser.add_argument("--host", default=None)
    serve_parser.add_argument("--port", type=int, default=None)
    serve_parser.add_argument("--reload", action="store_true")

    eval_parser = sub.add_parser("eval", help="运行评估套件")
    eval_parser.add_argument("--suite", default="smoke")

    args = parser.parse_args()
    settings = get_settings()
    setup_logging(level=settings.log_level, log_format=settings.log_format)
    init_workspace(settings)

    if args.command == "eval":
        from eval.runner import run_suite

        run_suite(args.suite)
        return

    host = getattr(args, "host", None) or settings.host
    port = getattr(args, "port", None) or settings.port
    reload = getattr(args, "reload", False)

    uvicorn.run("app.main:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    main()
