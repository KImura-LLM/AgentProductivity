from __future__ import annotations

from dotenv import load_dotenv

from productivity_agent.bootstrap import build_service
from productivity_agent.bot import build_application
from productivity_agent.config import load_settings
from productivity_agent.logging_config import configure_logging


def main() -> None:
    load_dotenv()
    settings = load_settings()
    configure_logging(settings.log_level, settings.secret_values())
    service = build_service(settings)
    application = build_application(settings, service)
    application.run_polling(allowed_updates=None)


if __name__ == "__main__":
    main()
