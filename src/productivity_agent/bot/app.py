from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import wraps

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from productivity_agent.config import Settings
from productivity_agent.parsing import parse_natural_command, parse_status_change
from productivity_agent.services import ProductivityService

logger = logging.getLogger(__name__)

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]

def build_application(settings: Settings, service: ProductivityService) -> Application:
    if not settings.has_telegram():
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_ID are required")
    application = ApplicationBuilder().token(settings.telegram_bot_token).post_init(post_init).build()
    application.bot_data["service"] = service
    application.bot_data["settings"] = settings

    application.add_handler(CommandHandler("start", _authorized(start)))
    application.add_handler(CommandHandler("help", _authorized(help_command)))
    application.add_handler(CommandHandler("settings", _authorized(settings_command)))
    application.add_handler(CommandHandler("today", _authorized(today)))
    application.add_handler(CommandHandler("briefing", _authorized(briefing)))
    application.add_handler(CommandHandler("week", _authorized(week)))
    application.add_handler(CommandHandler("review", _authorized(review)))
    application.add_handler(CommandHandler("project", _authorized(project)))
    application.add_handler(CommandHandler("life", _authorized(life)))
    application.add_handler(CommandHandler("add", _authorized(add)))
    application.add_handler(CommandHandler("done", _authorized(done)))
    application.add_handler(CommandHandler("reschedule", _authorized(reschedule)))
    application.add_handler(CommandHandler("focus", _authorized(focus)))
    application.add_handler(CommandHandler("stuck", _authorized(stuck)))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _authorized(text_message)))

    _schedule_jobs(application, settings)
    return application


def _authorized(handler: Handler) -> Handler:
    @wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        settings: Settings = context.application.bot_data["settings"]
        user = update.effective_user
        if not user or user.id != settings.telegram_allowed_user_id:
            if update.effective_message:
                await update.effective_message.reply_text("Нет доступа.")
            logger.warning("Unauthorized Telegram access attempt: %s", user.id if user else "unknown")
            return
        await handler(update, context)

    return wrapped


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("today", "План на сегодня"),
            BotCommand("briefing", "Утренний брифинг"),
            BotCommand("week", "Обзор недели"),
            BotCommand("review", "Вечерний review"),
            BotCommand("project", "Статус проекта"),
            BotCommand("life", "Личные задачи TickTick"),
            BotCommand("add", "Создать задачу"),
            BotCommand("done", "Закрыть задачу"),
            BotCommand("reschedule", "Перенести задачу"),
            BotCommand("focus", "Главный фокус"),
            BotCommand("stuck", "Зависшие задачи"),
            BotCommand("settings", "Настройки"),
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "👋 Я ассистент продуктивности.\n\n"
        "Можешь писать обычным текстом: «что мне делать сегодня», «покажи задачи по проекту», "
        "«что зависло». Команды доступны в меню Telegram.",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    service = _service(context)
    await _reply(update, await service.settings_text())


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, await _service(context).today())


async def briefing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, await _service(context).briefing())


async def week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, await _service(context).week())


async def review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, await _service(context).review())


async def project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    project_name = " ".join(context.args).strip()
    if not project_name:
        await _reply(update, "Укажи проект: /project CrossMeet")
        return
    await _reply(update, await _service(context).project(project_name))


async def life(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, await _service(context).life())


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, await _service(context).start_add(update.effective_message.text, update.effective_user.id))


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, await _service(context).start_done(update.effective_message.text, update.effective_user.id))


async def reschedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(
        update,
        await _service(context).start_reschedule(update.effective_message.text, update.effective_user.id),
    )


async def focus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, await _service(context).focus())


async def stuck(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, await _service(context).stuck())


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    service = _service(context)
    text = update.effective_message.text
    pending = await service.handle_pending_response(text, update.effective_user.id)
    if pending:
        await _reply(update, pending)
        return

    status_change = parse_status_change(text)
    if status_change:
        await _reply(update, await service.start_status_change(text, update.effective_user.id))
        return

    command = parse_natural_command(text)
    if command == "today":
        await _reply(update, await service.today())
    elif command == "briefing":
        await _reply(update, await service.briefing())
    elif command == "week":
        await _reply(update, await service.week())
    elif command == "review":
        await _reply(update, await service.review())
    elif command == "focus":
        await _reply(update, await service.focus())
    elif command == "stuck":
        await _reply(update, await service.stuck())
    elif command == "life":
        await _reply(update, await service.life())
    elif command == "settings":
        await _reply(update, await service.settings_text())
    elif command == "add":
        await _reply(update, await service.start_add(text, update.effective_user.id))
    elif command == "done":
        await _reply(update, await service.start_done(text, update.effective_user.id))
    elif command == "reschedule":
        await _reply(update, await service.start_reschedule(text, update.effective_user.id))
    else:
        history = _chat_history(context)
        response = await service.chat(text, history=history)
        _remember_message(context, role="user", content=text)
        _remember_message(context, role="assistant", content=response)
        await _reply(update, response)


async def _scheduled_briefing(context: ContextTypes.DEFAULT_TYPE) -> None:
    service = _service(context)
    settings: Settings = context.application.bot_data["settings"]
    await context.bot.send_message(chat_id=settings.telegram_allowed_user_id, text=await service.briefing())


async def _scheduled_review(context: ContextTypes.DEFAULT_TYPE) -> None:
    service = _service(context)
    settings: Settings = context.application.bot_data["settings"]
    await context.bot.send_message(chat_id=settings.telegram_allowed_user_id, text=await service.review())


async def _scheduled_week(context: ContextTypes.DEFAULT_TYPE) -> None:
    service = _service(context)
    settings: Settings = context.application.bot_data["settings"]
    await context.bot.send_message(chat_id=settings.telegram_allowed_user_id, text=await service.week())


async def _scheduled_overdue(context: ContextTypes.DEFAULT_TYPE) -> None:
    service = _service(context)
    settings: Settings = context.application.bot_data["settings"]
    await context.bot.send_message(chat_id=settings.telegram_allowed_user_id, text=await service.stuck())


def _schedule_jobs(application: Application, settings: Settings) -> None:
    if not application.job_queue:
        logger.warning("Telegram JobQueue is unavailable; scheduled briefings are disabled")
        return
    application.job_queue.run_daily(
        _scheduled_briefing,
        time=settings.parse_time(settings.morning_briefing_time),
        name="morning-briefing",
    )
    application.job_queue.run_daily(
        _scheduled_review,
        time=settings.parse_time(settings.evening_review_time),
        name="evening-review",
    )
    application.job_queue.run_daily(
        _scheduled_week,
        time=settings.parse_time(settings.weekly_review_time),
        days=(6,),
        name="weekly-overview",
    )
    application.job_queue.run_daily(
        _scheduled_overdue,
        time=settings.parse_time(settings.overdue_check_time),
        name="overdue-check",
    )


def _service(context: ContextTypes.DEFAULT_TYPE) -> ProductivityService:
    return context.application.bot_data["service"]


async def _reply(update: Update, text: str) -> None:
    await update.effective_message.reply_text(text[:4096])


def _chat_history(context: ContextTypes.DEFAULT_TYPE) -> list[dict[str, str]]:
    raw_history = context.user_data.get("chat_history", [])
    if not isinstance(raw_history, list):
        return []
    history: list[dict[str, str]] = []
    for item in raw_history[-8:]:
        if isinstance(item, dict) and isinstance(item.get("role"), str) and isinstance(item.get("content"), str):
            history.append({"role": item["role"], "content": item["content"][:1000]})
    return history


def _remember_message(context: ContextTypes.DEFAULT_TYPE, *, role: str, content: str) -> None:
    history = context.user_data.setdefault("chat_history", [])
    if not isinstance(history, list):
        context.user_data["chat_history"] = history = []
    history.append({"role": role, "content": content[:1200]})
    del history[:-8]
