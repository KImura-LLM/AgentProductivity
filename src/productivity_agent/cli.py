from __future__ import annotations

import argparse
import asyncio
import json
import webbrowser
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

from productivity_agent.bootstrap import build_service
from productivity_agent.config import load_settings
from productivity_agent.storage import JsonStateStore


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="agent-productivity-cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("config", help="Print safe runtime configuration")
    subparsers.add_parser("doctor", help="Check local configuration without exposing secrets")
    subparsers.add_parser("ticktick-auth-url", help="Print TickTick OAuth authorization URL")
    authorize = subparsers.add_parser(
        "ticktick-authorize",
        help="Open TickTick OAuth and store tokens from the local callback",
    )
    authorize.add_argument("--no-open", action="store_true", help="Print the URL without opening a browser")
    authorize.add_argument(
        "--compat",
        action="store_true",
        help="Use TickTick-compatible authorize URL without redirect_uri/state parameters",
    )
    authorize.add_argument(
        "--no-state",
        action="store_true",
        help="Do not include OAuth state while still including redirect_uri",
    )
    authorize.add_argument(
        "--api-host",
        action="store_true",
        help="Use api.ticktick.com for the authorize endpoint",
    )
    exchange = subparsers.add_parser("ticktick-exchange-code", help="Exchange TickTick OAuth code")
    exchange.add_argument("code")
    notion_sources = subparsers.add_parser(
        "notion-data-sources",
        help="Print data sources for a Notion database/container id",
    )
    notion_sources.add_argument("database_id")
    notion_schema = subparsers.add_parser(
        "notion-schema",
        help="Print property names and types for a Notion data source id",
    )
    notion_schema.add_argument("data_source_id")
    args = parser.parse_args()
    asyncio.run(_run(args))


async def _run(args: argparse.Namespace) -> None:
    settings = load_settings()
    service = build_service(settings)
    if args.command == "config":
        print(json.dumps(settings.as_safe_dict(), ensure_ascii=False, indent=2))
    elif args.command == "doctor":
        print(_doctor(settings))
    elif args.command == "ticktick-auth-url":
        print(service.repository.ticktick.build_authorization_url())
    elif args.command == "ticktick-authorize":
        try:
            tokens = await _ticktick_authorize(
                service,
                no_open=args.no_open,
                compat=args.compat,
                no_state=args.no_state,
                api_host=args.api_host,
            )
        except TimeoutError:
            print(
                "Timed out waiting for TickTick callback. Run the command again and approve "
                "access in the browser within 5 minutes."
            )
            return
        safe = {key: value for key, value in tokens.items() if key not in {"access_token", "refresh_token"}}
        print(json.dumps(safe, ensure_ascii=False, indent=2))
    elif args.command == "ticktick-exchange-code":
        tokens = await service.repository.ticktick.exchange_code(args.code)
        safe = {key: value for key, value in tokens.items() if key not in {"access_token", "refresh_token"}}
        print(json.dumps(safe, ensure_ascii=False, indent=2))
    elif args.command == "notion-data-sources":
        data_sources = await service.repository.notion.retrieve_database_data_sources(args.database_id)
        print(json.dumps(data_sources, ensure_ascii=False, indent=2))
    elif args.command == "notion-schema":
        properties = await service.repository.notion.retrieve_data_source_properties(args.data_source_id)
        print(json.dumps(properties, ensure_ascii=False, indent=2, sort_keys=True))


async def _ticktick_authorize(
    service,
    no_open: bool,
    compat: bool,
    no_state: bool,
    api_host: bool,
) -> dict:
    redirect = urlparse(service.settings.ticktick_redirect_uri)
    host = redirect.hostname or "127.0.0.1"
    port = redirect.port or 8765
    path = redirect.path or "/callback"
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_line = await reader.readline()
        while True:
            line = await reader.readline()
            if line in {b"\r\n", b"\n", b""}:
                break
        try:
            method, target, _ = request_line.decode("utf-8").split(" ", 2)
            parsed = urlparse(target)
            params = parse_qs(parsed.query)
            code = params.get("code", [None])[0]
            error = params.get("error", [None])[0]
            if method != "GET" or parsed.path != path:
                status = "404 Not Found"
                body = "Unknown callback path."
            elif error:
                status = "400 Bad Request"
                body = f"TickTick authorization failed: {error}"
                if not future.done():
                    future.set_exception(RuntimeError(body))
            elif code:
                status = "200 OK"
                body = "TickTick authorization received. You can return to Codex."
                if not future.done():
                    future.set_result(code)
            else:
                status = "400 Bad Request"
                body = "Missing authorization code."
        except Exception as exc:  # noqa: BLE001 - callback must always return an HTTP response.
            status = "400 Bad Request"
            body = f"Could not parse callback: {exc}"
            if not future.done():
                future.set_exception(exc)

        response = (
            f"HTTP/1.1 {status}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            "Connection: close\r\n"
            "\r\n"
            f"{body}"
        )
        writer.write(response.encode("utf-8"))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, host, port)
    url = service.repository.ticktick.build_authorization_url(
        include_redirect_uri=not compat,
        include_state=not compat and not no_state,
    )
    if api_host:
        url = url.replace("https://ticktick.com/oauth/authorize", "https://api.ticktick.com/oauth/authorize")
    print(f"Open this URL and authorize TickTick:\n{url}", flush=True)
    if not no_open:
        webbrowser.open(url)
    try:
        code = await asyncio.wait_for(future, timeout=300)
    finally:
        server.close()
        await server.wait_closed()
    return await service.repository.ticktick.exchange_code(code)


def _doctor(settings) -> str:
    lines = ["AgentProductivity configuration check"]
    missing: list[str] = []

    if not settings.telegram_bot_token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if settings.telegram_allowed_user_id is None:
        missing.append("TELEGRAM_ALLOWED_USER_ID")
    if not settings.openrouter_api_key:
        missing.append("OPENROUTER_API_KEY")
    if not settings.notion_token:
        missing.append("NOTION_TOKEN")
    if not settings.ticktick_client_id:
        missing.append("TICKTICK_CLIENT_ID")
    if not settings.ticktick_client_secret:
        missing.append("TICKTICK_CLIENT_SECRET")

    try:
        notion_databases = settings.notion_databases
    except Exception as exc:  # noqa: BLE001 - report config problems without stack trace.
        notion_databases = []
        lines.append(f"[FAIL] NOTION_DATABASES_JSON is invalid: {exc}")

    if not notion_databases:
        missing.append("NOTION_DATABASES_JSON or NOTION_TASKS_DATABASE_ID")

    state_tokens = JsonStateStore(settings.app_state_path).get_ticktick_tokens()
    ticktick_has_token = bool(
        settings.ticktick_access_token
        or settings.ticktick_refresh_token
        or state_tokens.get("access_token")
        or state_tokens.get("refresh_token")
    )
    if not ticktick_has_token:
        lines.append("[WARN] TickTick OAuth tokens are not stored yet.")
        lines.append("       After filling TICKTICK_CLIENT_ID/SECRET, run:")
        lines.append("       agent-productivity-cli ticktick-auth-url")
        lines.append("       agent-productivity-cli ticktick-exchange-code <code>")
    else:
        lines.append("[OK] TickTick OAuth tokens are stored.")

    if missing:
        lines.append("[FAIL] Missing required values:")
        lines.extend(f"       - {name}" for name in missing)
    else:
        lines.append("[OK] Required environment values are present.")

    lines.append(f"[OK] Timezone: {settings.timezone}")
    lines.append(f"[OK] Morning briefing: {settings.morning_briefing_time}")
    lines.append(f"[OK] Evening review: {settings.evening_review_time}")
    if notion_databases:
        lines.append("[OK] Notion databases: " + ", ".join(db.name for db in notion_databases))
    return "\n".join(lines)


if __name__ == "__main__":
    main()
