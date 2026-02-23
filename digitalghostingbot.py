import asyncio
import logging
import sqlite3
import re
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ================== НАСТРОЙКИ ==================
import os
BOT_TOKEN = os.getenv('BOT_TOKEN')
admin_ids_str = os.getenv('ADMIN_IDS')
if admin_ids_str:
    ADMIN_IDS = [int(x.strip()) for x in admin_ids_str.split(',') if x.strip()]
else:
    ADMIN_IDS = []
    print("⚠️ ВНИМАНИЕ: ADMIN_IDS не заданы, админ-панель будет недоступна")
PAYMENT_DETAILS = "Номер телефона для СБП: +7 993 636-08-79\nПолучатель: Иван Р. (Сбербанк)"
CONTACT_EMAIL = "digitalghosting.archive@gmail.com"

# Наличие товаров (только доступные для заказа)
AVAILABLE_PRODUCTS = {
    'flash': True,   # USB флешка
    'floppy': True,  # Дискета
    'cd': True,      # CD/DVD
    'vhs': False,    # нет в наличии
    'audio': False,  # нет в наличии
    'hdd': False     # нет в наличии
}

# ================== БАЗА ДАННЫХ ==================
def init_db():
    conn = sqlite3.connect('digital_ghosting.db')
    cur = conn.cursor()
    # Временные данные пользователей
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            temp_code TEXT,
            temp_name TEXT,
            temp_product TEXT,
            temp_city TEXT,
            temp_comment TEXT
        )
    ''')
    # Заказы
    cur.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            order_code TEXT,
            customer_name TEXT,
            product TEXT,
            city TEXT,
            comment TEXT,
            screenshot_file_id TEXT,
            status TEXT DEFAULT 'pending',
            confirmed_by INTEGER,
            confirmed_at TEXT,
            location_photo_id TEXT,
            location_coords TEXT,
            created_at TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# ================== СОСТОЯНИЯ FSM ==================
class OrderStates(StatesGroup):
    waiting_for_code = State()
    waiting_for_name = State()
    waiting_for_product = State()
    waiting_for_flash_capacity = State()
    waiting_for_city = State()
    waiting_for_comment = State()
    waiting_for_screenshot = State()

class AdminStates(StatesGroup):
    waiting_for_order_id_confirm = State()
    waiting_for_order_id_cancel = State()
    waiting_for_order_id_location = State()
    waiting_for_location_photo = State()
    waiting_for_location_coords = State()

# ================== КЛАВИАТУРЫ ==================
def main_menu_keyboard():
    kb = [
        [KeyboardButton(text="📦 Новый заказ")],
        [KeyboardButton(text="❓ Помощь")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def product_keyboard():
    builder = InlineKeyboardBuilder()
    product_names = {
        "flash": "💾 USB флешка",
        "floppy": "💽 Дискета 3.5\"",
        "cd": "📀 CD/DVD диск",
    }
    for key, name in product_names.items():
        if AVAILABLE_PRODUCTS.get(key, False):
            builder.button(text=name, callback_data=f"product_{key}")
    builder.adjust(2)
    return builder.as_markup()

def flash_capacity_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="4GB (350₽) 🔥скидка", callback_data="flash_4")
    builder.button(text="8GB (540₽)", callback_data="flash_8")
    builder.button(text="16GB (665₽)", callback_data="flash_16")
    builder.button(text="32GB (730₽)", callback_data="flash_32")
    builder.button(text="64GB (900₽)", callback_data="flash_64")
    builder.button(text="128GB (1750₽)", callback_data="flash_128")
    builder.adjust(2)
    return builder.as_markup()

def admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Статистика", callback_data="admin_stats")
    builder.button(text="📋 Список заказов", callback_data="admin_list")
    builder.button(text="✅ Подтвердить заказ", callback_data="admin_confirm")
    builder.button(text="❌ Отменить заказ", callback_data="admin_cancel")
    builder.button(text="📍 Отправить координаты", callback_data="admin_location")
    builder.button(text="💣 Удалить ВСЕ заказы", callback_data="admin_delete_all")
    builder.adjust(2)
    return builder.as_markup()

def skip_comment_keyboard():
    kb = [[KeyboardButton(text="🚫 Пропустить комментарий")]]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=True)

# ================== РАБОТА С БАЗОЙ ДАННЫХ ==================
def save_temp_user(user_id: int, **kwargs):
    conn = sqlite3.connect('digital_ghosting.db')
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    fields = ', '.join(kwargs.keys())
    placeholders = ', '.join(['?'] * len(kwargs))
    values = [user_id] + list(kwargs.values())
    cur.execute(f"INSERT INTO users (user_id, {fields}) VALUES (?, {placeholders})", values)
    conn.commit()
    conn.close()

def get_temp_user(user_id: int):
    conn = sqlite3.connect('digital_ghosting.db')
    cur = conn.cursor()
    cur.execute("SELECT temp_code, temp_name, temp_product, temp_city, temp_comment FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {
            'code': row[0],
            'name': row[1],
            'product': row[2],
            'city': row[3],
            'comment': row[4]
        }
    return None

def clear_temp_user(user_id: int):
    conn = sqlite3.connect('digital_ghosting.db')
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def save_order(user_id: int, code: str, name: str, product: str, city: str, comment: str, screenshot_file_id: str):
    conn = sqlite3.connect('digital_ghosting.db')
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO orders 
        (user_id, order_code, customer_name, product, city, comment, screenshot_file_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, code, name, product, city, comment, screenshot_file_id, datetime.now().isoformat()))
    conn.commit()
    order_id = cur.lastrowid
    conn.close()
    return order_id

def get_order(order_id: int):
    conn = sqlite3.connect('digital_ghosting.db')
    cur = conn.cursor()
    cur.execute('''
        SELECT id, user_id, order_code, customer_name, product, city, comment,
               screenshot_file_id, status, confirmed_by, confirmed_at,
               location_photo_id, location_coords, created_at
        FROM orders WHERE id = ?
    ''', (order_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        columns = ['id', 'user_id', 'order_code', 'customer_name', 'product', 'city', 'comment',
                   'screenshot_file_id', 'status', 'confirmed_by', 'confirmed_at',
                   'location_photo_id', 'location_coords', 'created_at']
        return dict(zip(columns, row))
    return None

def get_all_orders():
    conn = sqlite3.connect('digital_ghosting.db')
    cur = conn.cursor()
    cur.execute('''
        SELECT id, user_id, order_code, customer_name, product, city, comment,
               screenshot_file_id, status, confirmed_by, confirmed_at,
               location_photo_id, location_coords, created_at
        FROM orders ORDER BY created_at DESC
    ''')
    rows = cur.fetchall()
    conn.close()
    columns = ['id', 'user_id', 'order_code', 'customer_name', 'product', 'city', 'comment',
               'screenshot_file_id', 'status', 'confirmed_by', 'confirmed_at',
               'location_photo_id', 'location_coords', 'created_at']
    return [dict(zip(columns, row)) for row in rows]

def update_order_status(order_id: int, status: str, confirmed_by: int = None):
    conn = sqlite3.connect('digital_ghosting.db')
    cur = conn.cursor()
    if confirmed_by:
        cur.execute('''
            UPDATE orders SET status = ?, confirmed_by = ?, confirmed_at = ?
            WHERE id = ?
        ''', (status, confirmed_by, datetime.now().isoformat(), order_id))
    else:
        cur.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
    conn.commit()
    conn.close()

def update_order_location(order_id: int, photo_id: str, coords: str):
    conn = sqlite3.connect('digital_ghosting.db')
    cur = conn.cursor()
    cur.execute('''
        UPDATE orders SET location_photo_id = ?, location_coords = ?, status = 'delivered'
        WHERE id = ?
    ''', (photo_id, coords, order_id))
    conn.commit()
    conn.close()

def delete_all_orders():
    conn = sqlite3.connect('digital_ghosting.db')
    cur = conn.cursor()
    cur.execute("DELETE FROM orders")
    conn.commit()
    conn.close()

def get_stats():
    conn = sqlite3.connect('digital_ghosting.db')
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM orders")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM orders WHERE status = 'pending'")
    pending = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM orders WHERE status = 'confirmed'")
    confirmed = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM orders WHERE status = 'delivered'")
    delivered = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM orders WHERE status = 'cancelled'")
    cancelled = cur.fetchone()[0]
    conn.close()
    return total, pending, confirmed, delivered, cancelled

# ================== ИНИЦИАЛИЗАЦИЯ БОТА ==================
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ================== ОБЩИЕ КОМАНДЫ ==================
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👻 *Добро пожаловать в Digital Ghosting!*\n\n"
        "Это бот для оформления заказов на артефакты из нашего архива.\n"
        "Чтобы начать новый заказ, нажми кнопку ниже или отправь /neworder.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )

@dp.message(Command("neworder"))
async def cmd_new_order(message: types.Message, state: FSMContext):
    await state.set_state(OrderStates.waiting_for_code)
    await message.answer(
        "Введите код заказа, который вы получили на сайте (формат: DG-XXXXXXX):",
        reply_markup=ReplyKeyboardRemove()
    )

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    clear_temp_user(message.from_user.id)
    await message.answer("Действие отменено.", reply_markup=main_menu_keyboard())

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📖 *Справка*\n\n"
        "Команды:\n"
        "/start - главное меню\n"
        "/neworder - новый заказ\n"
        "/cancel - отменить текущее действие\n"
        "/admin - панель администратора",
        parse_mode="Markdown"
    )

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Доступ запрещён.")
        return
    await message.answer(
        "🔧 *Админ-панель*",
        parse_mode="Markdown",
        reply_markup=admin_keyboard()
    )

# ================== ДИАЛОГ ОФОРМЛЕНИЯ ЗАКАЗА ==================
@dp.message(OrderStates.waiting_for_code)
async def process_code(message: types.Message, state: FSMContext):
    code = message.text.strip()
    if not re.match(r'^DG-[A-Z2-9]{8}$', code):
        await message.answer(
            "❌ Неверный формат. Код должен быть вида DG-XXXXXXX (например, DG-ABCDEFGH).\n"
            "Попробуйте ещё раз или введите /cancel."
        )
        return
    await state.update_data(order_code=code)
    save_temp_user(message.from_user.id, temp_code=code)
    await state.set_state(OrderStates.waiting_for_name)
    await message.answer("Введите ваше имя или никнейм (как к вам обращаться):")

@dp.message(OrderStates.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Имя не может быть пустым. Введите имя:")
        return
    await state.update_data(customer_name=name)
    save_temp_user(message.from_user.id, temp_name=name)
    await state.set_state(OrderStates.waiting_for_product)
    await message.answer(
        "Выберите тип носителя:",
        reply_markup=product_keyboard()
    )

@dp.callback_query(F.data.startswith("product_"), OrderStates.waiting_for_product)
async def process_product_selection(callback: types.CallbackQuery, state: FSMContext):
    product_code = callback.data.split("_")[1]
    if not AVAILABLE_PRODUCTS.get(product_code, False):
        await callback.answer("❌ Этот товар временно отсутствует", show_alert=True)
        return

    product_names = {
        "flash": "USB флешка",
        "floppy": "Дискета 3.5\"",
        "cd": "CD/DVD диск"
    }
    product_name = product_names.get(product_code, "Неизвестный товар")

    if product_code == "flash":
        await state.update_data(product_base=product_name, product_code="flash")
        save_temp_user(callback.from_user.id, temp_product=product_name)
        await state.set_state(OrderStates.waiting_for_flash_capacity)
        await callback.message.edit_text(
            "Выберите объём флешки:",
            reply_markup=flash_capacity_keyboard()
        )
    else:
        await state.update_data(product=product_name)
        save_temp_user(callback.from_user.id, temp_product=product_name)
        await state.set_state(OrderStates.waiting_for_city)
        await callback.message.edit_text(f"✅ Выбран товар: {product_name}.\nТеперь укажите ваш город и район (примерно):")
    await callback.answer()

@dp.callback_query(F.data.startswith("flash_"), OrderStates.waiting_for_flash_capacity)
async def process_flash_capacity(callback: types.CallbackQuery, state: FSMContext):
    capacity_map = {
        "flash_4": "4GB (350₽, скидка!)",
        "flash_8": "8GB (540₽)",
        "flash_16": "16GB (665₽)",
        "flash_32": "32GB (730₽)",
        "flash_64": "64GB (900₽)",
        "flash_128": "128GB (1750₽)"
    }
    selected = capacity_map.get(callback.data, "USB флешка")
    await state.update_data(product=selected)
    save_temp_user(callback.from_user.id, temp_product=selected)
    await state.set_state(OrderStates.waiting_for_city)
    await callback.message.edit_text(f"✅ Выбрано: {selected}.\nТеперь укажите ваш город и район (примерно):")
    await callback.answer()

@dp.message(OrderStates.waiting_for_city)
async def process_city(message: types.Message, state: FSMContext):
    city = message.text.strip()
    if not city:
        await message.answer("Город не может быть пустым. Введите город:")
        return
    await state.update_data(city=city)
    save_temp_user(message.from_user.id, temp_city=city)
    await state.set_state(OrderStates.waiting_for_comment)
    await message.answer(
        "Добавьте комментарий к заказу (пожелания, особые указания).\n"
        "Если комментарий не нужен, нажмите кнопку ниже.",
        reply_markup=skip_comment_keyboard()
    )

@dp.message(OrderStates.waiting_for_comment)
async def process_comment(message: types.Message, state: FSMContext):
    comment = message.text.strip()
    if comment == "🚫 Пропустить комментарий" or comment.lower() in ["нет", "skip"]:
        comment = ""
    await state.update_data(comment=comment)
    save_temp_user(message.from_user.id, temp_comment=comment)

    data = await state.get_data()
    summary = (
        f"📋 *Сводка заказа*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📦 Код: {data.get('order_code')}\n"
        f"👤 Имя: {data.get('customer_name')}\n"
        f"💿 Товар: {data.get('product')}\n"
        f"🏙 Город: {data.get('city')}\n"
        f"💬 Комментарий: {comment or '—'}\n\n"
        f"💳 *Оплата*\n"
        f"{PAYMENT_DETAILS}\n\n"
        f"📸 Отправьте скриншот или чек об оплате:"
    )
    await message.answer(summary, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    await state.set_state(OrderStates.waiting_for_screenshot)

@dp.message(OrderStates.waiting_for_screenshot, F.photo)
async def process_screenshot(message: types.Message, state: FSMContext):
    photo = message.photo[-1]
    file_id = photo.file_id

    data = await state.get_data()
    user_id = message.from_user.id
    code = data.get('order_code')
    name = data.get('customer_name')
    product = data.get('product')
    city = data.get('city')
    comment = data.get('comment', '')

    order_id = save_order(user_id, code, name, product, city, comment, file_id)

    clear_temp_user(user_id)
    await state.clear()

    await message.answer(
        f"✅ *Заказ #{order_id} принят!*\n\n"
        "Ожидайте подтверждения в течение 24 часов. Мы уведомим вас, как только заказ будет подтверждён.\n"
        "Спасибо за обращение в Digital Ghosting!",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )

    # Уведомление админам
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🔔 *Новый заказ!*\n\n"
                f"🆔 Заказ #{order_id}\n"
                f"👤 Пользователь: {user_id}\n"
                f"🧑 Имя: {name}\n"
                f"💿 Товар: {product}\n"
                f"🏙 Город: {city}\n"
                f"💬 Комментарий: {comment or '—'}",
                parse_mode="Markdown"
            )
            confirm_kb = InlineKeyboardBuilder()
            confirm_kb.button(text="✅ Подтвердить заказ", callback_data=f"confirm_{order_id}")
            await bot.send_photo(
                admin_id,
                file_id,
                caption=f"Скрин оплаты заказа #{order_id}",
                reply_markup=confirm_kb.as_markup()
            )
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")

@dp.message(OrderStates.waiting_for_screenshot)
async def process_screenshot_invalid(message: types.Message):
    await message.answer("❌ Пожалуйста, отправьте фотографию (скриншот оплаты).")

# ================== АДМИН-ПАНЕЛЬ (ОБРАБОТЧИКИ КНОПОК) ==================
@dp.callback_query(F.data.startswith("admin_"))
async def admin_callback(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    action = callback.data.split("_")[1]

    if action == "stats":
        total, pending, confirmed, delivered, cancelled = get_stats()
        text = (
            f"📊 *Статистика*\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📦 Всего заказов: {total}\n"
            f"⏳ Ожидают: {pending}\n"
            f"✅ Подтверждено: {confirmed}\n"
            f"📦 Доставлено: {delivered}\n"
            f"❌ Отменено: {cancelled}"
        )
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=admin_keyboard())
        await callback.answer()

    elif action == "list":
        orders = get_all_orders()
        if not orders:
            await callback.message.edit_text("📭 Заказов нет.", reply_markup=admin_keyboard())
            await callback.answer()
            return
        text = "📋 *Последние заказы*\n━━━━━━━━━━━━━━━\n"
        for i, order in enumerate(orders[:10], 1):
            text += f"{i}. #{order['id']} – {order['customer_name']} – {order['product']} – {order['status']}\n"
        if len(orders) > 10:
            text += f"\n... и ещё {len(orders)-10} заказов."
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=admin_keyboard())
        await callback.answer()

    elif action == "confirm":
        await state.set_state(AdminStates.waiting_for_order_id_confirm)
        await callback.message.edit_text(
            "✏️ Введите ID заказа, который нужно подтвердить:",
            reply_markup=None
        )
        await callback.answer()

    elif action == "cancel":
        await state.set_state(AdminStates.waiting_for_order_id_cancel)
        await callback.message.edit_text(
            "✏️ Введите ID заказа, который нужно отменить:",
            reply_markup=None
        )
        await callback.answer()

    elif action == "location":
        await state.set_state(AdminStates.waiting_for_order_id_location)
        await callback.message.edit_text(
            "✏️ Введите ID заказа для отправки координат:",
            reply_markup=None
        )
        await callback.answer()

    elif action == "delete_all":
        # Подтверждение удаления всех заказов
        kb = InlineKeyboardBuilder()
        kb.button(text="💣 ДА, удалить всё", callback_data="delete_all_confirm")
        kb.button(text="❌ Отмена", callback_data="delete_all_cancel")
        await callback.message.edit_text(
            "⚠️ *Вы уверены?*\nЭто действие необратимо и удалит ВСЕ заказы из базы данных.",
            parse_mode="Markdown",
            reply_markup=kb.as_markup()
        )
        await callback.answer()

# ================== ПОДТВЕРЖДЕНИЕ УДАЛЕНИЯ ВСЕХ ЗАКАЗОВ ==================
@dp.callback_query(F.data == "delete_all_confirm")
async def delete_all_confirm(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    delete_all_orders()
    await callback.message.edit_text(
        "✅ *Все заказы удалены.*",
        parse_mode="Markdown",
        reply_markup=admin_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "delete_all_cancel")
async def delete_all_cancel(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.message.edit_text(
        "🔙 Действие отменено.",
        reply_markup=admin_keyboard()
    )
    await callback.answer()

# ================== ОБРАБОТКА ВВОДА ID ДЛЯ РАЗНЫХ ДЕЙСТВИЙ ==================
@dp.message(AdminStates.waiting_for_order_id_confirm)
async def admin_confirm_by_id(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Доступ запрещён.")
        await state.clear()
        return
    try:
        order_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Некорректный ID. Введите число.")
        return

    order = get_order(order_id)
    if not order:
        await message.answer(f"❌ Заказ с ID {order_id} не найден.")
        await state.clear()
        return

    update_order_status(order_id, 'confirmed', confirmed_by=message.from_user.id)
    try:
        await bot.send_message(
            order['user_id'],
            f"✅ *Ваш заказ #{order_id} подтверждён!*\n\nСкоро вы получите координаты и фото места.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Не удалось уведомить пользователя {order['user_id']}: {e}")

    await message.answer(f"✅ Заказ #{order_id} подтверждён.")
    await state.clear()

@dp.message(AdminStates.waiting_for_order_id_cancel)
async def admin_cancel_by_id(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Доступ запрещён.")
        await state.clear()
        return
    try:
        order_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Некорректный ID. Введите число.")
        return

    order = get_order(order_id)
    if not order:
        await message.answer(f"❌ Заказ с ID {order_id} не найден.")
        await state.clear()
        return

    update_order_status(order_id, 'cancelled')
    try:
        await bot.send_message(
            order['user_id'],
            f"❌ *Ваш заказ #{order_id} отменён.*\n\n"
            f"Если у вас есть вопросы, обратитесь на нашу официальную почту:\n{CONTACT_EMAIL}",
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Не удалось уведомить пользователя {order['user_id']}: {e}")

    await message.answer(f"✅ Заказ #{order_id} отменён, пользователь уведомлён.")
    await state.clear()

@dp.message(AdminStates.waiting_for_order_id_location)
async def admin_location_by_id(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Доступ запрещён.")
        await state.clear()
        return
    try:
        order_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Некорректный ID. Введите число.")
        return

    order = get_order(order_id)
    if not order:
        await message.answer(f"❌ Заказ с ID {order_id} не найден.")
        await state.clear()
        return

    await state.update_data(target_order_id=order_id)
    await state.set_state(AdminStates.waiting_for_location_photo)
    await message.answer("📸 Отправьте фото места (где спрятан артефакт):")

@dp.message(AdminStates.waiting_for_location_photo, F.photo)
async def admin_location_photo(message: types.Message, state: FSMContext):
    photo = message.photo[-1]
    file_id = photo.file_id
    await state.update_data(location_photo_id=file_id)
    await state.set_state(AdminStates.waiting_for_location_coords)
    await message.answer("✏️ Теперь отправьте координаты (текст, можно ссылку на карту):")

@dp.message(AdminStates.waiting_for_location_photo)
async def admin_location_photo_invalid(message: types.Message):
    await message.answer("❌ Пожалуйста, отправьте фото.")

@dp.message(AdminStates.waiting_for_location_coords)
async def admin_location_coords(message: types.Message, state: FSMContext):
    coords = message.text.strip()
    if not coords:
        await message.answer("❌ Координаты не могут быть пустыми.")
        return

    data = await state.get_data()
    order_id = data.get('target_order_id')
    photo_id = data.get('location_photo_id')

    update_order_location(order_id, photo_id, coords)

    order = get_order(order_id)
    if order:
        try:
            await bot.send_photo(
                order['user_id'],
                photo_id,
                caption=f"📍 *Ваш артефакт ждёт вас!*\n\n{coords}\n\nУдачи в поисках!",
                parse_mode="Markdown"
            )
        except Exception as e:
            logging.error(f"Не удалось отправить координаты пользователю {order['user_id']}: {e}")

    await message.answer(f"✅ Координаты для заказа #{order_id} отправлены пользователю.")
    await state.clear()

# ================== ОБРАБОТКА ИНЛАЙН-КНОПКИ ПОДТВЕРЖДЕНИЯ ИЗ УВЕДОМЛЕНИЯ ==================
@dp.callback_query(F.data.startswith("confirm_"))
async def confirm_order_inline(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    order_id = int(callback.data.split("_")[1])
    order = get_order(order_id)
    if not order:
        await callback.answer("❌ Заказ не найден", show_alert=True)
        return

    update_order_status(order_id, 'confirmed', confirmed_by=callback.from_user.id)
    try:
        await bot.send_message(
            order['user_id'],
            f"✅ *Ваш заказ #{order_id} подтверждён!*\n\nСкоро вы получите координаты и фото места.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Не удалось уведомить пользователя {order['user_id']}: {e}")

    await callback.answer("✅ Заказ подтверждён", show_alert=True)
    # Убираем кнопку из сообщения
    await callback.message.edit_caption(
        callback.message.caption + "\n\n✅ Подтверждён",
        reply_markup=None
    )

# ================== МЕНЮ ПОЛЬЗОВАТЕЛЯ ==================
@dp.message(F.text == "📦 Новый заказ")
async def menu_new_order(message: types.Message, state: FSMContext):
    await cmd_new_order(message, state)

@dp.message(F.text == "❓ Помощь")
async def menu_help(message: types.Message):
    await cmd_help(message)

@dp.message()
async def echo(message: types.Message):
    await message.answer("Используйте команды или кнопки меню.")

# ================== ЗАПУСК ==================
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
