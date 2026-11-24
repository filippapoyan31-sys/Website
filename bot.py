import asyncio
import html
import os
from aiohttp import web
import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "8672323905:AAFaEbWqsjQu7Qg9ln5nVeoUiZsVhclhyEM")
ADMIN_IDS = {9168522364}
DB_PATH = "countries_rp.db"

TURN_ACTION_LIMIT = 5
UPGRADE_BASE_COST = 10_000_000
REFORM_COST = 15_000_000
HOURLY_INTERVAL_SECONDS = 3600

COUNTRY_TIERS = {
    "small": {
        "label": "🌱 Малая держава",
        "start_budget": 20_000_000,
        "base_income": 1_500_000,
    },
    "medium": {
        "label": "⚖️ Средняя держава",
        "start_budget": 50_000_000,
        "base_income": 3_000_000,
    },
    "superpower": {
        "label": "👑 Сверхдержава",
        "start_budget": 100_000_000,
        "base_income": 6_000_000,
    },
}

SPHERES = {
    "infra": ("🏗 Инфраструктура", "infra_lvl", 1_000_000),
    "services": ("🛎 Услуги", "services_lvl", 1_000_000),
    "industry": ("🏭 Промышленность", "industry_lvl", 1_800_000),
    "military": ("🛡 Военный бюджет", "military_lvl", 500_000),
    "export": ("📦 Экспорт", "export_lvl", 1_700_000),
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


class CreateCountryStates(StatesGroup):
    waiting_name = State()
    waiting_tier = State()


class BudgetTransferStates(StatesGroup):
    waiting_amount = State()


class SendMoneyStates(StatesGroup):
    waiting_recipient = State()
    waiting_amount = State()


def esc(text: str) -> str:
    return html.escape(str(text))


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS countries (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                tier TEXT NOT NULL DEFAULT 'medium',
                budget INTEGER NOT NULL DEFAULT 50000000,
                actions_left INTEGER NOT NULL DEFAULT 5,
                infra_lvl INTEGER NOT NULL DEFAULT 1,
                services_lvl INTEGER NOT NULL DEFAULT 1,
                industry_lvl INTEGER NOT NULL DEFAULT 1,
                military_lvl INTEGER NOT NULL DEFAULT 1,
                export_lvl INTEGER NOT NULL DEFAULT 1,
                reform_points INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cur = await db.execute("PRAGMA table_info(countries)")
        columns = {row[1] for row in await cur.fetchall()}
        if "tier" not in columns:
            await db.execute(
                "ALTER TABLE countries ADD COLUMN tier TEXT NOT NULL DEFAULT 'medium'"
            )

        await db.commit()


async def add_event(text: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO events (text) VALUES (?)", (text,))
        await db.execute(
            """
            DELETE FROM events
            WHERE id NOT IN (
                SELECT id FROM events ORDER BY id DESC LIMIT 100
            )
            """
        )
        await db.commit()


async def get_recent_events(limit: int = 15) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT text FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [r[0] for r in rows]


async def get_country(user_id: int) -> aiosqlite.Row | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM countries WHERE user_id = ?", (user_id,))
        return await cur.fetchone()


async def get_all_countries() -> list[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM countries ORDER BY budget DESC")
        return await cur.fetchall()


def format_money(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + " $"


def get_tier_label(tier_key: str | None) -> str:
    if tier_key and tier_key in COUNTRY_TIERS:
        return COUNTRY_TIERS[tier_key]["label"]
    return COUNTRY_TIERS["medium"]["label"]


def calc_hourly_income(c: aiosqlite.Row) -> int:
    tier_key = c["tier"] if "tier" in c.keys() else "medium"
    base = COUNTRY_TIERS.get(tier_key, COUNTRY_TIERS["medium"])["base_income"]

    spheres_income = 0
    for _, (_, col, rate_per_lvl) in SPHERES.items():
        spheres_income += c[col] * rate_per_lvl

    reforms_income = c["reform_points"] * 1_000_000
    return int(base + spheres_income + reforms_income)


def tier_selection_kb() -> InlineKeyboardMarkup:
    rows = []
    for key, info in COUNTRY_TIERS.items():
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{info['label']} — старт {format_money(info['start_budget'])}",
                    callback_data=f"pick_tier:{key}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Состояние страны", callback_data="menu:status")],
            [
                InlineKeyboardButton(text="📈 Развитие сфер", callback_data="menu:spheres"),
                InlineKeyboardButton(text="📜 Реформы", callback_data="menu:reforms"),
            ],
            [
                InlineKeyboardButton(text="💰 Бюджет", callback_data="menu:budget"),
                InlineKeyboardButton(text="🌍 Рейтинг стран", callback_data="menu:top"),
            ],
            [
                InlineKeyboardButton(text="🏛 Категории стран", callback_data="menu:tiers"),
                InlineKeyboardButton(text="📰 Лента событий", callback_data="menu:events"),
            ],
        ]
    )


def spheres_kb(country: aiosqlite.Row) -> InlineKeyboardMarkup:
    rows = []
    for key, (label, col, _) in SPHERES.items():
        lvl = country[col]
        cost = UPGRADE_BASE_COST * lvl
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{label} (Ур.{lvl}) — {format_money(cost)}",
                    callback_data=f"upgrade:{key}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:status")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def budget_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💸 Потратить 5 000 000 $",
                    callback_data="spend:5000000",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✍️ Ввести сумму списания",
                    callback_data="spend:custom",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🤝 Перевод стране",
                    callback_data="transfer:start",
                )
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:status")],
        ]
    )


def format_country_text(c: aiosqlite.Row) -> str:
    income = calc_hourly_income(c)
    tier_label = get_tier_label(c["tier"] if "tier" in c.keys() else "medium")
    return (
        f"🏴 <b>Страна:</b> {esc(c['name'])}\n"
        f"🏛 <b>Категория:</b> {tier_label}\n"
        f"💰 <b>Бюджет:</b> {format_money(c['budget'])}\n"
        f"⏱ <b>Доход в час:</b> +{format_money(income)}\n"
        f"⚡️ <b>Действий осталось:</b> {c['actions_left']}/{TURN_ACTION_LIMIT}\n"
        f"📜 <b>Очки реформ:</b> {c['reform_points']}\n\n"
        f"<b>Сферы развития:</b>\n"
        f"• Инфраструктура: <b>Ур. {c['infra_lvl']}</b>\n"
        f"• Услуги: <b>Ур. {c['services_lvl']}</b>\n"
        f"• Промышленность: <b>Ур. {c['industry_lvl']}</b>\n"
        f"• Военный бюджет: <b>Ур. {c['military_lvl']}</b>\n"
        f"• Экспорт: <b>Ур. {c['export_lvl']}</b>"
    )


async def apply_hourly_cycle(notify_players: bool = True) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM countries")
        countries = await cur.fetchall()

        updated_count = 0
        notifications: list[tuple[int, str]] = []

        for c in countries:
            income = calc_hourly_income(c)
            await db.execute(
                """
                UPDATE countries
                SET budget = budget + ?,
                    actions_left = ?
                WHERE user_id = ?
                """,
                (income, TURN_ACTION_LIMIT, c["user_id"]),
            )
            updated_count += 1
            if notify_players:
                msg = (
                    f"⏱ <b>Почасовое начисление для «{esc(c['name'])}»</b>\n"
                    f"💵 Получено за прокачку сфер и реформы: <b>+{format_money(income)}</b>\n"
                    f"⚡️ Лимит действий восстановлен: <b>{TURN_ACTION_LIMIT}/{TURN_ACTION_LIMIT}</b>"
                )
                notifications.append((c["user_id"], msg))

        await db.commit()

    if notify_players:
        for uid, text in notifications:
            try:
                await bot.send_message(uid, text, parse_mode="HTML")
            except Exception:
                pass

    return updated_count


async def hourly_worker() -> None:
    while True:
        await asyncio.sleep(HOURLY_INTERVAL_SECONDS)
        try:
            await apply_hourly_cycle(notify_players=True)
        except Exception:
            pass


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    country = await get_country(message.from_user.id)
    if country is None:
        await state.set_state(CreateCountryStates.waiting_name)
        await message.answer(
            "🌍 Добро пожаловать в экономическое РП!\n"
            "Введите <b>название вашего государства</b>, чтобы основать страну:",
            parse_mode="HTML",
        )
        return

    await message.answer(
        format_country_text(country),
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )


@dp.message(CreateCountryStates.waiting_name)
async def process_country_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 2 or len(name) > 50:
        await message.answer("Название должно быть от 2 до 50 символов. Введите ещё раз:")
        return

    await state.update_data(country_name=name)
    await state.set_state(CreateCountryStates.waiting_tier)
    await message.answer(
        f"🏴 Название: <b>{esc(name)}</b>\n"
        "Выберите <b>категорию (мощь) вашей страны</b>:\n\n"
        "• <b>Малая держава</b> — меньший стартовый бюджет и базовый доход\n"
        "• <b>Средняя держава</b> — сбалансированный старт\n"
        "• <b>Сверхдержава</b> — крупный стартовый бюджет и высокий базовый доход",
        reply_markup=tier_selection_kb(),
        parse_mode="HTML",
    )


@dp.callback_query(CreateCountryStates.waiting_tier, F.data.startswith("pick_tier:"))
async def process_country_tier(callback: CallbackQuery, state: FSMContext) -> None:
    tier_key = callback.data.split(":", 1)[1]
    if tier_key not in COUNTRY_TIERS:
        await callback.answer("Неизвестная категория.", show_alert=True)
        return

    data = await state.get_data()
    name = data.get("country_name")
    if not name:
        await state.clear()
        await callback.answer("Сессия создания сброшена. Нажмите /start.", show_alert=True)
        return

    tier_info = COUNTRY_TIERS[tier_key]

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO countries (user_id, name, tier, budget, actions_left)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                callback.from_user.id,
                name,
                tier_key,
                tier_info["start_budget"],
                TURN_ACTION_LIMIT,
            ),
        )
        await db.commit()

    await state.clear()
    await add_event(f"🏳️ Основано государство <b>{esc(name)}</b> ({tier_info['label']})")

    country = await get_country(callback.from_user.id)
    await callback.message.edit_text(
        f"✅ Государство <b>{esc(country['name'])}</b> основано!\n\n"
        + format_country_text(country),
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )
    await callback.answer("Страна создана!")


@dp.callback_query(F.data == "menu:status")
async def cb_status(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    country = await get_country(callback.from_user.id)
    if country is None:
        await callback.answer("Сначала нажмите /start и создайте страну.", show_alert=True)
        return
    await callback.message.edit_text(
        format_country_text(country),
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "menu:spheres")
async def cb_spheres(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    country = await get_country(callback.from_user.id)
    if country is None:
        await callback.answer("Сначала нажмите /start.", show_alert=True)
        return
    await callback.message.edit_text(
        f"📈 <b>Развитие сфер</b> (действий: {country['actions_left']}/{TURN_ACTION_LIMIT})\n"
        f"Бюджет: <b>{format_money(country['budget'])}</b>\n"
        f"Выберите сферу для улучшения:",
        reply_markup=spheres_kb(country),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("upgrade:"))
async def cb_upgrade_sphere(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    sphere_key = callback.data.split(":", 1)[1]
    if sphere_key not in SPHERES:
        await callback.answer("Неизвестная сфера.", show_alert=True)
        return

    label, col, _ = SPHERES[sphere_key]
    country = await get_country(callback.from_user.id)
    if country is None:
        await callback.answer("Сначала нажмите /start.", show_alert=True)
        return

    if country["actions_left"] <= 0:
        await callback.answer("Лимит действий на этот час исчерпан!", show_alert=True)
        return

    lvl = country[col]
    cost = UPGRADE_BASE_COST * lvl
    if country["budget"] < cost:
        await callback.answer("Недостаточно средств в бюджете!", show_alert=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            f"""
            UPDATE countries
            SET {col} = {col} + 1,
                budget = budget - ?,
                actions_left = actions_left - 1
            WHERE user_id = ? AND budget >= ? AND actions_left > 0
            """,
            (cost, callback.from_user.id, cost),
        )
        await db.commit()
        if cur.rowcount == 0:
            await callback.answer("Не удалось улучшить сферу. Проверьте баланс и действия.", show_alert=True)
            return

    updated = await get_country(callback.from_user.id)
    await add_event(
        f"📈 <b>{esc(updated['name'])}</b> улучшила сферу «{label}» до <b>Ур. {updated[col]}</b>"
    )
    await callback.message.edit_text(
        f"✅ <b>{label}</b> улучшена до <b>Ур. {updated[col]}</b>!\n\n"
        + format_country_text(updated),
        reply_markup=spheres_kb(updated),
        parse_mode="HTML",
    )
    await callback.answer("Сфера улучшена!")


@dp.callback_query(F.data == "menu:reforms")
async def cb_reforms_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    country = await get_country(callback.from_user.id)
    if country is None:
        await callback.answer("Сначала нажмите /start.", show_alert=True)
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"📜 Провести реформу ({format_money(REFORM_COST)})",
                    callback_data="reform:do",
                )
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:status")],
        ]
    )
    await callback.message.edit_text(
        f"📜 <b>Государственные реформы</b>\n"
        f"Каждая реформа даёт <b>+1 очко реформ</b> (увеличивает доход в час на 1 000 000 $).\n"
        f"Стоимость: <b>{format_money(REFORM_COST)}</b> и <b>1 действие</b>.\n"
        f"Текущие очки реформ: <b>{country['reform_points']}</b>\n"
        f"Действий осталось: <b>{country['actions_left']}/{TURN_ACTION_LIMIT}</b>",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "reform:do")
async def cb_do_reform(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    country = await get_country(callback.from_user.id)
    if country is None:
        await callback.answer("Сначала нажмите /start.", show_alert=True)
        return
    if country["actions_left"] <= 0:
        await callback.answer("Лимит действий на этот час исчерпан!", show_alert=True)
        return
    if country["budget"] < REFORM_COST:
        await callback.answer("Недостаточно средств для реформы!", show_alert=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            UPDATE countries
            SET reform_points = reform_points + 1,
                budget = budget - ?,
                actions_left = actions_left - 1
            WHERE user_id = ? AND budget >= ? AND actions_left > 0
            """,
            (REFORM_COST, callback.from_user.id, REFORM_COST),
        )
        await db.commit()
        if cur.rowcount == 0:
            await callback.answer("Не удалось провести реформу. Проверьте баланс и действия.", show_alert=True)
            return

    updated = await get_country(callback.from_user.id)
    await add_event(
        f"📜 <b>{esc(updated['name'])}</b> провела реформу (очков реформ: <b>{updated['reform_points']}</b>)"
    )
    await callback.message.edit_text(
        f"📜 Реформа проведена! Очки реформ: <b>{updated['reform_points']}</b>.\n\n"
        + format_country_text(updated),
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )
    await callback.answer("Реформа принята!")


@dp.callback_query(F.data == "menu:budget")
async def cb_budget(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    country = await get_country(callback.from_user.id)
    if country is None:
        await callback.answer("Сначала нажмите /start.", show_alert=True)
        return
    await callback.message.edit_text(
        f"💰 <b>Управление бюджетом</b>\n"
        f"Текущий бюджет: <b>{format_money(country['budget'])}</b>\n"
        f"Доход в час: <b>+{format_money(calc_hourly_income(country))}</b>\n"
        f"Действий осталось: <b>{country['actions_left']}/{TURN_ACTION_LIMIT}</b>",
        reply_markup=budget_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("spend:"))
async def cb_spend(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    mode = callback.data.split(":", 1)[1]
    country = await get_country(callback.from_user.id)
    if country is None:
        await callback.answer("Сначала нажмите /start.", show_alert=True)
        return

    if mode == "custom":
        await state.set_state(BudgetTransferStates.waiting_amount)
        await callback.message.answer(
            "✍️ Введите сумму в долларах (только целое число, например: <code>2500000</code>):",
            parse_mode="HTML",
        )
        await callback.answer()
        return

    amount = int(mode)
    if country["actions_left"] <= 0:
        await callback.answer("Лимит действий на этот час исчерпан!", show_alert=True)
        return
    if country["budget"] < amount:
        await callback.answer("Недостаточно средств!", show_alert=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            UPDATE countries
            SET budget = budget - ?,
                actions_left = actions_left - 1
            WHERE user_id = ? AND budget >= ? AND actions_left > 0
            """,
            (amount, callback.from_user.id, amount),
        )
        await db.commit()
        if cur.rowcount == 0:
            await callback.answer("Недостаточно средств или действий.", show_alert=True)
            return

    updated = await get_country(callback.from_user.id)
    await callback.message.edit_text(
        f"💸 Списано <b>{format_money(amount)}</b>.\n\n"
        + format_country_text(updated),
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )
    await callback.answer("Средства списаны")


@dp.message(BudgetTransferStates.waiting_amount)
async def process_custom_spend(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").replace(" ", "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer("Введите корректное положительное целое число:")
        return

    amount = int(raw)
    country = await get_country(message.from_user.id)
    if country is None:
        await state.clear()
        return
    if country["actions_left"] <= 0:
        await state.clear()
        await message.answer("Лимит действий на этот час исчерпан.", reply_markup=main_menu_kb())
        return
    if country["budget"] < amount:
        await message.answer(
            f"Недостаточно средств. Доступно: <b>{format_money(country['budget'])}</b>.",
            parse_mode="HTML",
        )
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            UPDATE countries
            SET budget = budget - ?,
                actions_left = actions_left - 1
            WHERE user_id = ? AND budget >= ? AND actions_left > 0
            """,
            (amount, message.from_user.id, amount),
        )
        await db.commit()
        if cur.rowcount == 0:
            await state.clear()
            await message.answer("Не удалось списать средства. Проверьте баланс и действия.", reply_markup=main_menu_kb())
            return

    await state.clear()
    updated = await get_country(message.from_user.id)
    await message.answer(
        f"💸 Списано <b>{format_money(amount)}</b>.\n\n"
        + format_country_text(updated),
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "transfer:start")
async def cb_transfer_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    sender = await get_country(callback.from_user.id)
    if sender is None:
        await callback.answer("Сначала нажмите /start.", show_alert=True)
        return
    if sender["actions_left"] <= 0:
        await callback.answer("Лимит действий на этот час исчерпан!", show_alert=True)
        return

    all_c = await get_all_countries()
    targets = [c for c in all_c if c["user_id"] != callback.from_user.id]
    if not targets:
        await callback.answer("Пока нет других стран для перевода.", show_alert=True)
        return

    rows = [
        [
            InlineKeyboardButton(
                text=f"🏴 {c['name']}",
                callback_data=f"transfer:to:{c['user_id']}",
            )
        ]
        for c in targets[:20]
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:budget")])

    await state.set_state(SendMoneyStates.waiting_recipient)
    await callback.message.edit_text(
        "🤝 <b>Перевод средств другой стране</b>\n"
        "Перевод списывает <b>1 действие</b>. Выберите государство-получателя:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("transfer:to:"))
async def cb_transfer_recipient(callback: CallbackQuery, state: FSMContext) -> None:
    target_id = int(callback.data.split(":", 2)[2])
    target = await get_country(target_id)
    if target is None:
        await callback.answer("Страна не найдена.", show_alert=True)
        return

    await state.update_data(target_id=target_id, target_name=target["name"])
    await state.set_state(SendMoneyStates.waiting_amount)
    await callback.message.answer(
        f"💵 Введите сумму перевода для <b>{esc(target['name'])}</b> (целое число, например: <code>10000000</code>):",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.message(SendMoneyStates.waiting_amount)
async def process_transfer_amount(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").replace(" ", "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer("Введите корректное положительное целое число:")
        return

    amount = int(raw)
    data = await state.get_data()
    target_id = data.get("target_id")
    target_name = data.get("target_name", "стране")

    sender = await get_country(message.from_user.id)
    target = await get_country(target_id) if target_id else None

    if sender is None or target is None:
        await state.clear()
        await message.answer("Ошибка перевода. Попробуйте снова.", reply_markup=main_menu_kb())
        return
    if sender["actions_left"] <= 0:
        await state.clear()
        await message.answer("Лимит действий на этот час исчерпан.", reply_markup=main_menu_kb())
        return
    if sender["budget"] < amount:
        await message.answer(
            f"Недостаточно средств. Доступно: <b>{format_money(sender['budget'])}</b>.",
            parse_mode="HTML",
        )
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            UPDATE countries
            SET budget = budget - ?,
                actions_left = actions_left - 1
            WHERE user_id = ? AND budget >= ? AND actions_left > 0
            """,
            (amount, message.from_user.id, amount),
        )
        if cur.rowcount == 0:
            await state.clear()
            await message.answer("Не удалось выполнить перевод. Проверьте баланс и действия.", reply_markup=main_menu_kb())
            return

        await db.execute(
            """
            UPDATE countries
            SET budget = budget + ?
            WHERE user_id = ?
            """,
            (amount, target_id),
        )
        await db.commit()

    await state.clear()
    await add_event(
        f"🤝 <b>{esc(sender['name'])}</b> перевела <b>{format_money(amount)}</b> стране <b>{esc(target_name)}</b>"
    )

    try:
        await bot.send_message(
            target_id,
            f"💵 Страна <b>{esc(sender['name'])}</b> перевела вам <b>{format_money(amount)}</b>!",
            parse_mode="HTML",
        )
    except Exception:
        pass

    updated = await get_country(message.from_user.id)
    await message.answer(
        f"✅ Переведено <b>{format_money(amount)}</b> стране <b>{esc(target_name)}</b>!\n\n"
        + format_country_text(updated),
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "menu:top")
async def cb_top(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    rows = await get_all_countries()

    if not rows:
        text = "🌍 Пока ни одна страна не зарегистрирована."
    else:
        lines = ["🌍 <b>Топ стран по бюджету:</b>\n"]
        for idx, r in enumerate(rows[:10], start=1):
            lines.append(f"{idx}. <b>{esc(r['name'])}</b> — {format_money(r['budget'])}")
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:status")]]
    )
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "menu:tiers")
async def cb_tiers(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    rows = await get_all_countries()
    if not rows:
        text = "🏛 Пока ни одна страна не зарегистрирована."
    else:
        lines = ["🏛 <b>Категории стран мира:</b>\n"]
        for c in rows:
            tier_label = get_tier_label(c["tier"] if "tier" in c.keys() else "medium")
            lines.append(f"• <b>{esc(c['name'])}</b> — {tier_label}")
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:status")]]
    )
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "menu:events")
async def cb_events(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    events = await get_recent_events(limit=15)
    if not events:
        text = "📰 <b>Лента событий</b>\n\nПока событий ещё не было."
    else:
        lines = ["📰 <b>Лента событий (переводы, улучшения, реформы):</b>\n"]
        for item in events:
            lines.append(f"• {item}")
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:status")]]
    )
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@dp.message(Command("countries"))
async def cmd_countries(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    rows = await get_all_countries()
    if not rows:
        await message.answer("Список стран пуст.")
        return
    lines = ["📋 <b>Все страны:</b>\n"]
    for c in rows:
        tier_label = get_tier_label(c["tier"] if "tier" in c.keys() else "medium")
        lines.append(
            f"• <b>{esc(c['name'])}</b> (<code>{c['user_id']}</code>) | {tier_label} | {format_money(c['budget'])} | ход: {c['actions_left']}"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("set_budget"))
async def cmd_set_budget(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].lstrip("-").isdigit():
        await message.answer("Формат: <code>/set_budget USER_ID СУММА</code>", parse_mode="HTML")
        return

    uid = int(parts[1])
    amount = int(parts[2])
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("UPDATE countries SET budget = ? WHERE user_id = ?", (amount, uid))
        await db.commit()
        if cur.rowcount == 0:
            await message.answer("Страна с таким USER_ID не найдена.")
            return

    await message.answer(
        f"✅ Бюджет игрока <code>{uid}</code> установлен: <b>{format_money(amount)}</b>.",
        parse_mode="HTML",
    )


@dp.message(Command("add_budget"))
async def cmd_add_budget(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].lstrip("-").isdigit():
        await message.answer("Формат: <code>/add_budget USER_ID СУММА</code>", parse_mode="HTML")
        return

    uid = int(parts[1])
    delta = int(parts[2])
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE countries SET budget = budget + ? WHERE user_id = ?",
            (delta, uid),
        )
        await db.commit()
        if cur.rowcount == 0:
            await message.answer("Страна с таким USER_ID не найдена.")
            return

    await message.answer(
        f"✅ Бюджет игрока <code>{uid}</code> изменён на <b>{format_money(delta)}</b>.",
        parse_mode="HTML",
    )


@dp.message(Command("backup"))
async def cmd_backup(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    if not os.path.exists(DB_PATH):
        await message.answer("Файл базы данных пока не создан.")
        return
    await message.answer_document(
        FSInputFile(DB_PATH),
        caption="💾 Резервная копия базы стран (countries_rp.db)",
    )


@dp.message(Command("next_turn"))
async def cmd_next_turn(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Эта команда доступна только администраторам.")
        return

    count = await apply_hourly_cycle(notify_players=True)
    await message.answer(
        f"🔄 <b>Начисление и сброс действий выполнены!</b>\n"
        f"Обработано стран: <b>{count}</b>, лимит действий восстановлен до <b>{TURN_ACTION_LIMIT}</b>.",
        parse_mode="HTML",
    )

async def _health_handler(request: web.Request) -> web.Response:
    return web.Response(text="OK")



async def start_http_server() -> web.AppRunner:
    port = int(os.environ.get("PORT", "8000"))
    app = web.Application()
    app.router.add_get("/", _health_handler)
    app.router.add_get("/health", _health_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    return runner

async def main() -> None:
    await init_db()
    asyncio.create_task(hourly_worker())
    runner = await start_http_server()
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()




if __name__ == "__main__":
    asyncio.run(main())
