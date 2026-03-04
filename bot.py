#!/usr/bin/env python3
"""
Nutrition Tracking Telegram Bot
Personal cutting tracker with AI-powered analysis
"""

import os
import json
import logging
from datetime import date
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)
from anthropic import Anthropic
import database as db

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))

anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Your personal plan ────────────────────────────────────────────────────────
PLAN = {
    "deficit": {
        "calories": 2300,
        "protein": 185,
        "fat": 68,
        "carbs": 210,
        "label": "🔥 Дефицит"
    },
    "maintenance": {
        "calories": 2900,
        "protein": 185,
        "fat": 95,
        "carbs": 340,
        "label": "⚖️ Поддержка"
    }
}

def get_day_type(for_date: date = None) -> str:
    d = for_date or date.today()
    return "maintenance" if d.weekday() >= 5 else "deficit"

def get_day_plan(for_date: date = None) -> dict:
    return PLAN[get_day_type(for_date)]

def is_allowed(update: Update) -> bool:
    if ALLOWED_USER_ID == 0:
        return True
    return update.effective_user.id == ALLOWED_USER_ID


# ── AI Functions ──────────────────────────────────────────────────────────────

def parse_food_with_ai(food_text: str) -> dict:
    prompt = f"""Ты нутрициолог-аналитик. Пользователь написал что съел: "{food_text}"

Определи КБЖУ для каждого продукта и общую сумму.
Отвечай ТОЛЬКО валидным JSON, без пояснений, без markdown:

{{
  "items": [
    {{"name": "название продукта", "amount": "количество", "calories": 0, "protein": 0, "fat": 0, "carbs": 0}}
  ],
  "total": {{"calories": 0, "protein": 0, "fat": 0, "carbs": 0}},
  "confidence": "high/medium/low",
  "notes": "короткое примечание если нужно (или пустая строка)"
}}

Если продукт неизвестен — используй среднестатистические значения. Все значения числами."""

    response = anthropic.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )

    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]

    return json.loads(text.strip())


def get_ai_analysis(daily_summary: dict, plan: dict, day_type: str, history: list) -> str:
    history_text = ""
    if history:
        history_text = "\n\nИстория последних дней:\n"
        for h in history[-7:]:
            history_text += f"- {h['date']}: {h['calories']:.0f} ккал, Б:{h['protein']:.0f}г, Ж:{h['fat']:.0f}г, У:{h['carbs']:.0f}г ({h['day_type']})\n"

    prompt = f"""Ты персональный нутрициолог для атлета на сушке. Профиль:
- 26 лет, мужчина, 83 кг, 189 см
- Активный: велосипед, прогулки, зал 2р/неделю, йога
- Цель: постоянное совершенствование, долгосрочный прогресс
- Схема: 5 дней дефицит / 2 дня поддержка

Сегодня ({day_type}), план: {plan['calories']} ккал, Б:{plan['protein']}г Ж:{plan['fat']}г У:{plan['carbs']}г

Фактически: {daily_summary['calories']:.0f} ккал, Б:{daily_summary['protein']:.0f}г Ж:{daily_summary['fat']:.0f}г У:{daily_summary['carbs']:.0f}г
{history_text}

Дай короткий (3-5 предложений) ценный анализ: как прошёл день, что видно в динамике, конкретный совет на завтра.
По-русски, дружелюбно, конкретно."""

    response = anthropic.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )

    return response.content[0].text.strip()


def get_weekly_analysis(weekly_data: list) -> str:
    if not weekly_data:
        return "Недостаточно данных."

    data_text = "\n".join([
        f"- {d['date']} ({d['day_type']}): {d['calories']:.0f} ккал, Б:{d['protein']:.0f}г, Ж:{d['fat']:.0f}г, У:{d['carbs']:.0f}г"
        for d in weekly_data
    ])

    prompt = f"""Нутрициолог, разбери неделю атлета на сушке (26л, 83кг, 189см, активный):

{data_text}

Цели: дефицит 2300 ккал (пн-пт), поддержка 2900 ккал (сб-вс), белок всегда 185г+

Дай разбор: соблюдение плана, среднее vs цели, баланс БЖУ, топ-2 рекомендации.
По-русски, конкретные цифры."""

    response = anthropic.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=700,
        messages=[{"role": "user", "content": prompt}]
    )

    return response.content[0].text.strip()


# ── Formatting ────────────────────────────────────────────────────────────────

def progress_bar(current: float, target: float, length: int = 8) -> str:
    pct = min(current / target, 1.3) if target > 0 else 0
    filled = int(pct * length)
    if pct > 1.1:
        bar = "🔴" * min(filled, length)
    elif pct > 0.85:
        bar = "🟢" * filled + "⬜" * (length - filled)
    else:
        bar = "🟡" * filled + "⬜" * (length - filled)
    return f"{bar} {int(pct*100)}%"


# ── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    db.init_db()
    plan = get_day_plan()
    text = f"""👋 *Привет! Твой личный нутрициолог готов.*

Сегодня: *{plan['label']}* — {plan['calories']} ккал
Б: {plan['protein']}г | Ж: {plan['fat']}г | У: {plan['carbs']}г

*Просто напиши что съел:*
› _"завтрак: овсянка 100г, 3 яйца, кофе с молоком"_
› _"курица 200г с рисом 150г и огурцом"_
› _"протеин 30г"_

*Команды:*
/итог — сводка дня + AI-анализ
/неделя — аналитика за 7 дней
/план — твой план на сегодня
/история — данные за 7 дней"""

    await update.message.reply_text(text, parse_mode="Markdown")


async def show_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    plan = get_day_plan()
    d = PLAN['deficit']
    m = PLAN['maintenance']
    weekday_ru = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"][date.today().weekday()]

    text = f"""📋 *Твой план*

Сегодня *{weekday_ru}* → {plan['label']}

🎯 Калории: `{plan['calories']}` ккал
🥩 Белок:   `{plan['protein']}г`
🥑 Жиры:   `{plan['fat']}г`
🍚 Углеводы: `{plan['carbs']}г`

*Схема недели:*
Пн–Пт → 🔥 Дефицит ({d['calories']} ккал)
Сб–Вс → ⚖️ Поддержка ({m['calories']} ккал)"""

    await update.message.reply_text(text, parse_mode="Markdown")


async def daily_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    today = date.today().isoformat()
    totals = db.get_daily_totals(today)
    entries = db.get_daily_entries(today)
    plan = get_day_plan()
    day_type = get_day_type()

    if totals['calories'] == 0:
        await update.message.reply_text("📭 Сегодня ещё нет записей. Напиши что съел!")
        return

    cal_left = plan['calories'] - totals['calories']
    prot_left = plan['protein'] - totals['protein']

    def signed(x): return f"+{x:.0f}" if x > 0 else f"{x:.0f}"

    summary = f"""📊 *Итог — {date.today().strftime('%d.%m.%Y')}*
{plan['label']} | {len(entries)} приёмов

*Калории:*
{progress_bar(totals['calories'], plan['calories'])}
`{totals['calories']:.0f}` / `{plan['calories']}` ккал ({signed(cal_left)} осталось)

*Макро:*
🥩 Белок:    `{totals['protein']:.0f}г` / `{plan['protein']}г`  {progress_bar(totals['protein'], plan['protein'], 6)}
🥑 Жиры:    `{totals['fat']:.0f}г` / `{plan['fat']}г`  {progress_bar(totals['fat'], plan['fat'], 6)}
🍚 Углеводы: `{totals['carbs']:.0f}г` / `{plan['carbs']}г`  {progress_bar(totals['carbs'], plan['carbs'], 6)}"""

    await update.message.reply_text(summary, parse_mode="Markdown")

    thinking = await update.message.reply_text("🤖 Анализирую...")
    history = db.get_history(days=7)
    analysis = get_ai_analysis(totals, plan, PLAN[day_type]['label'], history)
    await thinking.edit_text(f"💡 *AI-анализ:*\n\n{analysis}", parse_mode="Markdown")


async def weekly_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    data = db.get_history(days=7)

    if not data:
        await update.message.reply_text("📭 Недостаточно данных. Веди трекинг несколько дней!")
        return

    lines = ["📈 *Последние 7 дней:*\n"]
    for d in data:
        plan_cal = PLAN['maintenance']['calories'] if d['day_type'] == 'maintenance' else PLAN['deficit']['calories']
        diff = d['calories'] - plan_cal
        sign = "▲" if diff > 0 else "▼"
        lines.append(f"`{d['date']}` {d['calories']:.0f}ккал {sign}{abs(diff):.0f} | Б:{d['protein']:.0f}г")

    avg_cal = sum(d['calories'] for d in data) / len(data)
    avg_prot = sum(d['protein'] for d in data) / len(data)
    lines.append(f"\n📊 Среднее: `{avg_cal:.0f} ккал` | `{avg_prot:.0f}г белка`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    thinking = await update.message.reply_text("🤖 Готовлю недельный разбор...")
    analysis = get_weekly_analysis(data)
    await thinking.edit_text(f"🗓 *Недельный AI-анализ:*\n\n{analysis}", parse_mode="Markdown")


async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    history = db.get_history(days=7)

    if not history:
        await update.message.reply_text("📭 История пуста. Начни вести трекинг!")
        return

    lines = ["📜 *История (7 дней):*\n"]
    for d in history:
        icon = "⚖️" if d['day_type'] == 'maintenance' else "🔥"
        plan_cal = PLAN[d['day_type']]['calories']
        diff = d['calories'] - plan_cal
        sign = "+" if diff >= 0 else ""
        lines.append(f"{icon} `{d['date']}` — {d['calories']:.0f} ккал ({sign}{diff:.0f})\n   Б:{d['protein']:.0f} Ж:{d['fat']:.0f} У:{d['carbs']:.0f}\n")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def handle_food(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return

    text = update.message.text.strip()
    msg = await update.message.reply_text("⏳ Считаю КБЖУ...")

    try:
        result = parse_food_with_ai(text)
        today = date.today().isoformat()
        day_type = get_day_type()

        db.add_food_entry(
            date=today,
            description=text,
            items=json.dumps(result['items'], ensure_ascii=False),
            calories=result['total']['calories'],
            protein=result['total']['protein'],
            fat=result['total']['fat'],
            carbs=result['total']['carbs'],
            day_type=day_type
        )

        totals = db.get_daily_totals(today)
        plan = get_day_plan()
        cal_left = plan['calories'] - totals['calories']
        prot_left = plan['protein'] - totals['protein']

        items_text = ""
        for item in result['items']:
            items_text += f"  • {item['name']} {item['amount']}: {item['calories']:.0f} ккал | Б:{item['protein']:.0f} Ж:{item['fat']:.0f} У:{item['carbs']:.0f}\n"

        conf = {"high": "✅", "medium": "⚠️", "low": "❓"}.get(result.get('confidence', 'medium'), "⚠️")

        response = f"""{conf} *Записано:*

{items_text}
*Добавлено:* `{result['total']['calories']:.0f}` ккал | Б:`{result['total']['protein']:.0f}г` Ж:`{result['total']['fat']:.0f}г` У:`{result['total']['carbs']:.0f}г`

*За сегодня:* `{totals['calories']:.0f}` / `{plan['calories']}` ккал
Осталось: `{cal_left:.0f}` ккал | Белка ещё: `{prot_left:.0f}г`"""

        if result.get('notes'):
            response += f"\n\n💬 _{result['notes']}_"

        await msg.edit_text(response, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Food parse error: {e}")
        await msg.edit_text(
            "❌ Не смог распознать. Попробуй: _«курица 200г, рис 150г»_",
            parse_mode="Markdown"
        )


def main():
    db.init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("итог", daily_summary))
    app.add_handler(CommandHandler("неделя", weekly_report))
    app.add_handler(CommandHandler("план", show_plan))
    app.add_handler(CommandHandler("история", show_history))
    app.add_handler(CommandHandler("помощь", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_food))

    logger.info("🚀 Bot is running!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
