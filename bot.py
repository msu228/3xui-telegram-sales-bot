import asyncio
import hmac
import hashlib
import threading
from aiogram import BaseMiddleware
from aiogram.filters import Command
import json
import logging
import os
import time
import uuid
from collections import defaultdict
from io import BytesIO

import qrcode
import requests
import urllib3
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import (
    BufferedInputFile,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    MenuButtonCommands,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
)



import os


def load_env_file(path: str = "/root/xui-bot/secrets.env") -> None:
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env_file()

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_TOKEN = os.environ["API_TOKEN"]
XUI_API_URL = os.environ["XUI_API_URL"]
XUI_PUBLIC_URL=os.environ["XUI_PUBLIC_URL"]
MANUAL_FILE_PATH = os.environ["MANUAL_FILE_PATH"]
RESOLVERS_FILE_PATH = os.environ["RESOLVERS_FILE_PATH"]
PROCESSED_PAYMENTS_FILE = os.environ["PROCESSED_PAYMENTS_FILE"]
CONTACT_EMAIL = os.environ["CONTACT_EMAIL"]
CONTACT_SIMPLEX = os.environ["CONTACT_SIMPLEX"]
DNS_TUNNEL_SERVER = os.environ["DNS_TUNNEL_SERVER"]
DNS_TUNNEL_PASSWORD = os.environ["DNS_TUNNEL_PASSWORD"]

# Отключаем предупреждения о самоподписанном SSL
#urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==================== НАСТРОЙКИ ====================

# Локальный URL панели (через который бот стучится на сервере под капотом)

# Публичный URL для клиентов без лишних путей

# Твой API-токен из панели (Настройки -> Безопасность -> API Token)

# Список ID инбаундов для промо-подписок
INBOUND_IDS = [ 20, 27]

# Список ID инбаундов для премиум-подписок
PREMIUM_INBOUND_IDS = [4, 6, 7, 8, 12, 13, 15, 16]

# Лимиты по IP
PROMO_LIMIT_IP = 2
PREMIUM_LIMIT_IP = 5

# Группы клиентов в панели
GROUP_PROMO = "promo"
GROUP_PREMIUM = "premium"

# Пути к файлам

# Настройки DNS-туннеля

# 1 ТБ в представлении API панели (проверено на практике)
PREMIUM_TOTAL_GB = 1099511627776

# Начальный премиум-доступ при создании: бесплатно на 3 дня
PREMIUM_INITIAL_DAYS = 3
PREMIUM_INITIAL_MS = PREMIUM_INITIAL_DAYS * 24 * 3600 * 1000

# Продление премиума: +30 дней
PREMIUM_RENEW_DAYS = 30
PREMIUM_RENEW_MS = PREMIUM_RENEW_DAYS * 24 * 3600 * 1000

# False = продление бесплатно на время тестов (звёзды не тратятся)
# True  = продление через оплату Telegram Stars (XTR)
RENEW_REQUIRES_STARS = True
STARS_RENEW_PRICE = 1

# Контакты

# Единое уведомление об условиях
TERMS_NOTICE = "Пользуясь любыми услугами, вы соглашаетесь с условиями (см. Инфо).\n"
# ==================================================

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

dp.message.filter(F.chat.type == "private")
dp.callback_query.filter(
    F.message.chat.type == "private"
)

# Блокировки на уровне конкретного tg_id для предотвращения гонок при быстрых кликах
class UserLocks:
    def __init__(self):
        self.locks = [asyncio.Lock() for _ in range(1024)]
    def __getitem__(self, uid):
        return self.locks[uid % len(self.locks)]
user_locks = UserLocks()


class PaymentStore:
    # Старый JSON-массив автоматически читается; новый формат хранит статусы.
    def __init__(self, filepath):
        self.filepath = filepath
        self.records = {}
        if os.path.exists(filepath):
            with open(filepath, encoding="utf-8") as f:
                data = json.load(f)
            self.records = ({str(x): {"status": "completed"} for x in data}
                            if isinstance(data, list) else data)
            if not isinstance(self.records, dict):
                raise ValueError("Invalid payment journal")

    def put(self, key, record):
        updated = dict(self.records)
        updated[key] = record
        os.makedirs(os.path.dirname(os.path.abspath(self.filepath)), exist_ok=True)
        tmp = self.filepath + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(updated, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.filepath)
        self.records = updated
        fd = os.open(os.path.dirname(os.path.abspath(self.filepath)), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

payment_store = PaymentStore(PROCESSED_PAYMENTS_FILE)

# Ограничение обычных действий, платежные события не отбрасываются.

class AntiFlood(BaseMiddleware):
    def __init__(self):
        self.pending = {}
        self.last_heavy = {}
        self.active = 0
        self.max_active = 40
        self.delay = 2.0

        self.aliases = {
            "📊 Моя подписка": "my_sub",
            "🎁 Промо-доступ": "get_promo",
            "💎 Получить премиум": "get_premium",
            "🔄 Продлить премиум (+30 дней)": "renew_premium_1m",
        }

        self.heavy = {
            "my_sub",
            "get_promo",
            "get_premium",
            "renew_premium_1m",
        }

    async def acknowledge(self, event, text=None):
        if isinstance(event, types.CallbackQuery):
            try:
                await event.answer(text=text)
            except Exception:
                pass

    async def __call__(self, handler, event, data):
        # Платежи не ограничиваем антифлудом.
        if getattr(event, "successful_payment", None):
            return await handler(event, data)

        user = getattr(event, "from_user", None)
        if not user:
            return await handler(event, data)

        uid = user.id
        if isinstance(event, types.Message) and getattr(data.get("handler"), "callback", None) in (support_receive, support_admin_reply):
            return await handler(event, data)
        action = (
            getattr(event, "data", None)
            or getattr(event, "text", None)
            or "unknown"
        )
        action = self.aliases.get(action, action)

        now = time.monotonic()

        if len(self.last_heavy) >= 10000:
            self.last_heavy = {
                k: v for k, v in self.last_heavy.items()
                if now - v < 60
            }

        # Повторное нажатие не создаёт новый запрос.
        actions = self.pending.get(uid, set())

        if action in actions:
            await self.acknowledge(event)
            return

        # Ограничиваем размер очереди.
        if len(actions) >= 2 or self.active >= self.max_active:
            await self.acknowledge(
                event, "⏳ Секундочку..."
            )
            return

        self.pending.setdefault(uid, set()).add(action)
        self.active += 1

        try:
            if action in self.heavy:
                now = time.monotonic()

                scheduled = max(
                    now,
                    self.last_heavy.get(uid, 0) + self.delay
                )

                self.last_heavy[uid] = scheduled
                wait = scheduled - now

                if wait > 0:
                    await self.acknowledge(
                        event, "⏳ Обрабатываем запрос..."
                    )
                    await asyncio.sleep(wait)

            return await handler(event, data)

        finally:
            self.active -= 1

            actions = self.pending.get(uid)
            if actions is not None:
                actions.discard(action)
                if not actions:
                    self.pending.pop(uid, None)


flood = AntiFlood()
dp.message.middleware(flood)
dp.callback_query.middleware(flood)
panel_slots = asyncio.Semaphore(3)

async def panel_call(fn, *args):
    async with panel_slots:
        return await asyncio.to_thread(fn, *args)

MENUS = {
    "subs": ("📊 Мои подписки", [("Посмотреть подписки", "my_sub"), ("Продлить премиум", "renew_premium_1m")]),
    "tariffs": ("🛒 Тарифы и доступ", [("🎁 Промо-доступ", "get_promo"), ("💎 Пробный премиум", "get_premium"), ("⭐ Купить / продлить на 30 дней", "renew_premium_1m"), ("ℹ️ Инфо", "show_info")]),
    "tools": ("🛠 Инструменты", [("🌐 DNS-туннель", "get_dns_tunnel"), ("📘 Руководство", "get_manual")]),
    "support": ("💬 Поддержка", [("✉️ Написать администратору", "support_open"), ("📬 Контакты", "show_contacts"), ("💳 Помощь с оплатой", "pay_help"), ("ℹ️ Условия", "show_info")]),
}

def get_main_reply_keyboard():
    labels = [v[0] for v in MENUS.values()]
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=x) for x in labels[:2]], [KeyboardButton(text=x) for x in labels[2:]]], resize_keyboard=True, is_persistent=True)

def section_keyboard(key):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=c)] for t,c in MENUS[key][1]] + [[InlineKeyboardButton(text="« Главное меню", callback_data="menu_home")]])

def get_start_inline_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=v[0], callback_data="menu_"+k)] for k,v in MENUS.items()])

@dp.message(F.text.in_([v[0] for v in MENUS.values()]))
async def section_reply(message):
    support_sessions.pop(message.from_user.id, None)
    key = next(k for k,v in MENUS.items() if v[0] == message.text)
    await message.answer(MENUS[key][0], reply_markup=section_keyboard(key))

@dp.callback_query(F.data.startswith("menu_"))
async def section_callback(callback):
    key = callback.data[5:]
    await callback.answer()
    if key == "home":
        await callback.message.edit_text("Главное меню", reply_markup=get_start_inline_keyboard())
    elif key in MENUS:
        await callback.message.edit_text(MENUS[key][0], reply_markup=section_keyboard(key))

@dp.message(Command("paysupport"))
async def pay_support(message):
    await message.answer("По вопросам оплаты укажите ID платежа. Не отправляйте ссылку подписки.\n" + get_contacts_text(), parse_mode="Markdown")

@dp.callback_query(F.data == "pay_help")
async def pay_help(callback):
    await callback.answer()
    await pay_support(callback.message)


def get_contacts_text() -> str:
    return (
        "📬 **Контакты:**\n\n"
        f"• **Email:** `{CONTACT_EMAIL}`\n"
        f"• **SimpleX:** {CONTACT_SIMPLEX}"
    )


def get_tariffs_info_text() -> str:
    """Информационный блок о сервисе, тарифах и условиях"""
    renew_price_note = (
        f" **{STARS_RENEW_PRICE} ⭐ XTR**"
        if RENEW_REQUIRES_STARS
        else "**бесплатно (тестовый режим без списания звёзд)**"
    )
    return (
        "ℹ️ **Информация о сервисе и условиях:**\n\n"
        "• **Формат работы:** сервис предоставляется **As Is** («как есть»), без гарантий бесперебойной работы, возвратов и SLA.\n"
        "• **Нагрузка и доступность:** нашими услугами пользуются десятки человек, и работающие подключения есть практически всегда.\n"
        "• **Модерация и возвраты:** администратор оставляет за собой право отключить клиента без возврата средств в случае подозрения на злоупотребления.\n"
        "• **Характер проекта:** проект некоммерческий — платные услуги ставят целью отбить затраты на инфраструктуру.\n"
        "• **Безопасность оплаты звёздами:** платить Telegram Stars безопасно, т.к. экстремист — Пабло, а не его телеграм.\n\n"
        "1️⃣ **Промо-доступ:**\n"
        f"• Трафик: Безлимит | Устройства: До {PROMO_LIMIT_IP} IP одновременно\n\n"
        "2️⃣ **Премиум:**\n"
        f"• «💎 Получить премиум» — **бесплатно (0 ⭐)** на **{PREMIUM_INITIAL_DAYS} дня** с лимитом **1 ТБ** (или синхронизация, если аккаунт уже создан)\n"
        f"• «🔄 Продлить премиум (+30 дней)» — продлевает на **+{PREMIUM_RENEW_DAYS} дней**, добавляет остаток старого лимита к новому лимиту **1 ТБ** ({renew_price_note})\n"
        f"• Устройства: До {PREMIUM_LIMIT_IP} IP одновременно\n\n"
        "3️⃣ **DNS-туннель (free):**\n"
        "• Резервный канал связи для мессенджеров при жёстких ограничениях мобильного интернета\n\n"
        "⚖️ **Условия:** торренты, спам и DDoS строго запрещены.\n"
        "  Торренты разрешены лишь на тех подключениях, где указано явно\n"
        "• LTE сейчас на т/о\n"
        f"{TERMS_NOTICE}"
    )


def get_dns_tunnel_text() -> str:
    """Формирует сообщение с параметрами DNS-туннеля, резолверами и клиентами"""
    resolvers_block = ""
    if os.path.isfile(RESOLVERS_FILE_PATH):
        try:
            with open(RESOLVERS_FILE_PATH, "r", encoding="utf-8") as f:
                raw_content = f.read().strip()
            if raw_content:
                if len(raw_content) > 2500:
                    raw_content = raw_content[:2500].rstrip() + "\n... (список обрезан)"
                resolvers_block = f"📋 **Список резолверов:**\n```\n{raw_content}\n```\n\n"
            else:
                resolvers_block = "📋 **Список резолверов:** файл пуст.\n\n"
        except Exception as e:
            logging.error("Не удалось прочитать ")
            resolvers_block = "📋 Список резолверов временно недоступен.\n\n"
    else:
        resolvers_block = "📋 Список резолверов временно недоступен.\n\n"

    return (
        "🌐 **DNS-туннель (free)**\n\n"
        f"• **Сервер:** `{DNS_TUNNEL_SERVER}`\n"
        f"• **Пароль:** `{DNS_TUNNEL_PASSWORD}`\n\n"
        f"{resolvers_block}"
        "📱 **Клиенты:**\n"
        "• [MasterDnsVPN](https://github.com/masterking32/MasterDnsVPN/blob/main/README_RU.MD)\n"
        "• Хороший клиент под Android: [MasterDnsVPN-AndroidClient](https://github.com/Hidden-Node/MasterDnsVPN-AndroidClient)\n"
        "• Для яблодрочеров [NexaTunel App](https://apps.apple.com/app/id6766783641)\n\n"
        "DNS-туннель -- заяц-тормоз, но чатиться позволит даже в условиях жесткого органичения мобильного интернета.\n\n"
        f"{TERMS_NOTICE}"
    )


def format_bytes(num_bytes: int | float) -> str:
    val = float(max(0, num_bytes or 0))
    for unit in ["Б", "КБ", "МБ", "ГБ", "ТБ"]:
        if val < 1024 or unit == "ТБ":
            return f"{val:.2f} {unit}"
        val /= 1024
    return f"{val:.2f} ТБ"


def format_expiry_human(expiry_ms: int) -> str:
    if not expiry_ms or expiry_ms <= 0:
        return "Без ограничения срока"
    return time.strftime("%d.%m.%Y %H:%M UTC", time.gmtime(expiry_ms / 1000))


def _normalize_total_bytes(raw_total: int | float) -> int:
    val = int(raw_total or 0)
    if val <= 0:
        return 0
    if val == 1073741824:
        return val * 1024
    if val >= 1024**4:
        return val
    return val * (1024**3)


class XUIClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._local = threading.local()

    @property
    def session(self):
        if not hasattr(self._local, "session"):
            session = requests.Session()
            session.verify = True
            session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            })
            self._local.session = session
        return self._local.session

    @staticmethod
    def _parse_clients(inbound_obj: dict) -> list[dict]:
        settings = inbound_obj.get("settings", {})
        if isinstance(settings, str):
            try:
                settings = json.loads(settings)
            except Exception:
                settings = {}
        clients = settings.get("clients", [])
        return clients if isinstance(clients, list) else []

    def get_all_inbounds(self) -> list[dict]:
        """Возвращает список всех инбаундов из панели"""
        url = f"{self.base_url}/panel/api/inbounds/list"
        res = self.session.get(url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data.get("success") and isinstance(data.get("obj"), list):
                return data["obj"]
        raise RuntimeError("Panel list failed")

    def get_client_from_inbound(self, inbound_id: int, email: str) -> dict | None:
        """Ищет клиента в инбаунде через общий список."""
        inbound_id = int(inbound_id)

        for inbound in self.get_all_inbounds():
            if int(inbound.get("id") or 0) != inbound_id:
                continue

            for client in self._parse_clients(inbound):
                if client.get("email") == email:
                    return client

            return None

        raise RuntimeError(
            f"Inbound {inbound_id} not found in panel"
        )
    def add_client_to_inbound(
        self,
        inbound_id: int,
        client_uuid: str,
        sub_uuid: str,
        email: str,
        tg_id: int,
        group_name: str,
        total_gb: int = 0,
        expiry_time: int = 0,
        limit_ip: int = 2,
    ) -> bool:
        """Добавляет клиента в инбаунд с указанной группой"""
        payload = {
            "id": int(inbound_id),
            "inboundId": int(inbound_id),
            "inboundIds": [int(inbound_id)],
            "client": {
                "id": client_uuid,
                "subId": sub_uuid,
                "email": email,
                "enable": True,
                "tgId": int(tg_id),
                "group": group_name,
                "expiryTime": int(expiry_time),
                "limitIp": int(limit_ip),
                "totalGB": int(total_gb),
                "flow": "",
                "reset": 0,
            },
        }
        url = f"{self.base_url}/panel/api/clients/add"
        res = self.session.post(url, json=payload, timeout=10)
        ok = res.status_code == 200 and res.json().get("success", False)
        if not ok:
            raise RuntimeError("Panel add failed")
        return ok

    def delete_client_by_email(self, email: str, inbound_id: int | None = None) -> bool:
        """Удаляет клиента через /panel/api/clients/del/{email}"""
        url = f"{self.base_url}/panel/api/clients/del/{email}"
        payload = {"email": email}
        if inbound_id is not None:
            payload["id"] = int(inbound_id)
            payload["inboundId"] = int(inbound_id)

        res = self.session.post(url, json=payload, timeout=10)
        if res.status_code == 200 and res.json().get("success", False):
            return True

        res_del = self.session.delete(url, json=payload, timeout=10)
        if res_del.status_code == 200 and res_del.json().get("success", False):
            return True

        raise RuntimeError("Panel delete failed")

    def _sync_inbounds_exact(
        self,
        target_inbound_ids: list[int],
        email: str,
        tg_id: int,
        client_uuid: str,
        sub_uuid: str,
        group_name: str,
        total_gb: int,
        expiry_time: int,
        limit_ip: int,
    ):
        """
        Приводит инбаунды клиента в точное соответствие с target_inbound_ids:
        - добавляет в недостающие целевые инбаунды;
        - удаляет из инбаундов, которых нет в target_inbound_ids.
        """
        target_set = {int(i) for i in target_inbound_ids}

        for inbound_id in target_inbound_ids:
            ib_int = int(inbound_id)
            existing = self.get_client_from_inbound(ib_int, email=email)
            if not existing:
                self.add_client_to_inbound(
                    inbound_id=ib_int,
                    client_uuid=client_uuid,
                    sub_uuid=sub_uuid,
                    email=email,
                    tg_id=tg_id,
                    group_name=group_name,
                    total_gb=total_gb,
                    expiry_time=expiry_time,
                    limit_ip=limit_ip,
                )

        all_inbounds = self.get_all_inbounds()
        for inbound in all_inbounds:
            raw_ib_id = inbound.get("id")
            if raw_ib_id is None:
                continue
            ib_int = int(raw_ib_id)
            if ib_int in target_set:
                continue

            for client in self._parse_clients(inbound):
                if client.get("email") == email:
                    self.delete_client_by_email(email=email, inbound_id=ib_int)

    def process_user_promo(self, tg_id: int):
        email = f"tg_{tg_id}"
        existing_client = None

        for inbound_id in INBOUND_IDS:
            found = self.get_client_from_inbound(int(inbound_id), email=email)
            if found:
                existing_client = found
                break

        if not existing_client:
            for inbound in self.get_all_inbounds():
                for client in self._parse_clients(inbound):
                    if client.get("email") == email:
                        existing_client = client
                        break
                if existing_client:
                    break

        if not existing_client:
            client_uuid = str(uuid.uuid4())
            sub_uuid = str(uuid.uuid4())
            already_exists = False
        else:
            client_uuid = existing_client.get("id") or str(uuid.uuid4())
            sub_uuid = existing_client.get("subId") or client_uuid
            already_exists = True

        self._sync_inbounds_exact(
            target_inbound_ids=INBOUND_IDS,
            email=email,
            tg_id=tg_id,
            client_uuid=client_uuid,
            sub_uuid=sub_uuid,
            group_name=GROUP_PROMO,
            total_gb=0,
            expiry_time=0,
            limit_ip=PROMO_LIMIT_IP,
        )

        return sub_uuid, email, already_exists

    def _find_existing_premium_client(self, tg_id: int) -> dict | None:
        email = f"prem_{tg_id}"
        for inbound_id in PREMIUM_INBOUND_IDS:
            found = self.get_client_from_inbound(int(inbound_id), email=email)
            if found:
                return found

        for inbound in self.get_all_inbounds():
            for client in self._parse_clients(inbound):
                if client.get("email") == email:
                    return client
        return None

    def get_or_sync_user_premium_3d(self, tg_id: int):
        """
        Бесплатное получение премиум-аккаунта на 3 дня (1 ТБ, группа premium).
        Если аккаунт уже есть — синхронизирует инбаунды без пересоздания.
        """
        email = f"prem_{tg_id}"
        existing = self._find_existing_premium_client(tg_id)

        if existing:
            client_uuid = existing.get("id")
            sub_uuid = existing.get("subId") or client_uuid
            expiry_ms = int(existing.get("expiryTime") or 0)
            self._sync_inbounds_exact(
                target_inbound_ids=PREMIUM_INBOUND_IDS,
                email=email,
                tg_id=tg_id,
                client_uuid=client_uuid,
                sub_uuid=sub_uuid,
                group_name=GROUP_PREMIUM,
                total_gb=PREMIUM_TOTAL_GB,
                expiry_time=expiry_ms,
                limit_ip=PREMIUM_LIMIT_IP,
            )
            return sub_uuid, email, expiry_ms, True

        now_ms = int(time.time() * 1000)
        expiry_ms = now_ms + PREMIUM_INITIAL_MS
        client_uuid = str(uuid.uuid4())
        sub_uuid = str(uuid.uuid4())

        self._sync_inbounds_exact(
            target_inbound_ids=PREMIUM_INBOUND_IDS,
            email=email,
            tg_id=tg_id,
            client_uuid=client_uuid,
            sub_uuid=sub_uuid,
            group_name=GROUP_PREMIUM,
            total_gb=PREMIUM_TOTAL_GB,
            expiry_time=expiry_ms,
            limit_ip=PREMIUM_LIMIT_IP,
        )

        return sub_uuid, email, expiry_ms, False


    def _compute_premium_carryover_total(self, email: str, existing: dict, all_inbounds: list[dict]) -> int:
        """
        Вычисляет новый лимит трафика при пересоздании:
        PREMIUM_TOTAL_GB (1 ТБ) + остаток старого лимита (если старый лимит был задан и не исчерпан).
        """
        used_bytes = 0
        max_total_bytes = 0

        for ib in all_inbounds:
            client_stats = ib.get("clientStats") or []
            for st in client_stats:
                if isinstance(st, dict) and st.get("email") == email:
                    used_bytes += int(st.get("up") or 0) + int(st.get("down") or 0)
                    st_total = _normalize_total_bytes(st.get("total") or 0)
                    if st_total > max_total_bytes:
                        max_total_bytes = st_total

            for cl in self._parse_clients(ib):
                if cl.get("email") == email:
                    cl_total = _normalize_total_bytes(cl.get("totalGB") or 0)
                    if cl_total > max_total_bytes:
                        max_total_bytes = cl_total

        if max_total_bytes <= 0:
            max_total_bytes = _normalize_total_bytes(existing.get("totalGB") or PREMIUM_TOTAL_GB)

        remaining_bytes = max(0, max_total_bytes - used_bytes)
        new_total = int(PREMIUM_TOTAL_GB + remaining_bytes)

        logging.info(
            f"Расчёт лимита для {email}: old_limit={format_bytes(max_total_bytes)}, "
            f"used={format_bytes(used_bytes)}, carryover={format_bytes(remaining_bytes)}, "
            f"new_total={format_bytes(new_total)} ({new_total})"
        )
        return new_total

    def renew_user_premium_by_recreate(self, tg_id: int):
        """
        Пересоздаёт премиум-клиента во всех целевых инбаундах через /panel/api/clients/del/{email} + /panel/api/clients/add
        с теми же id/subId/email/tgId/group, добавляя остаток старого трафика к новому лимиту 1 ТБ и продлевая на +30 дней.
        """
        email = f"prem_{tg_id}"
        existing = self._find_existing_premium_client(tg_id)
        if not existing:
            client_uuid, sub_uuid = str(uuid.uuid4()), str(uuid.uuid4())
            expiry = int(time.time()*1000) + PREMIUM_RENEW_MS
            self._sync_inbounds_exact(PREMIUM_INBOUND_IDS, email, tg_id,
                                      client_uuid, sub_uuid, GROUP_PREMIUM,
                                      PREMIUM_TOTAL_GB, expiry, PREMIUM_LIMIT_IP)
            return sub_uuid, email, expiry, PREMIUM_TOTAL_GB

        client_uuid = str(existing.get("id") or uuid.uuid4())
        sub_uuid = str(existing.get("subId") or client_uuid)
        limit_ip = int(existing.get("limitIp") or PREMIUM_LIMIT_IP)

        all_inbounds = self.get_all_inbounds()
        new_total_gb = self._compute_premium_carryover_total(email=email, existing=existing, all_inbounds=all_inbounds)

        now_ms = int(time.time() * 1000)
        max_expiry = int(existing.get("expiryTime") or 0)
        for inbound_id in PREMIUM_INBOUND_IDS:
            cl = self.get_client_from_inbound(int(inbound_id), email=email)
            if cl:
                c_exp = int(cl.get("expiryTime") or 0)
                if c_exp > max_expiry:
                    max_expiry = c_exp

        base_ms = max_expiry if max_expiry > now_ms else now_ms
        new_expiry_ms = base_ms + PREMIUM_RENEW_MS

        # Удаляем старую запись клиента по email
        self.delete_client_by_email(email=email)

        # Добавляем заново в актуальные PREMIUM_INBOUND_IDS с новыми +30 днями и новым лимитом
        for inbound_id in PREMIUM_INBOUND_IDS:
            self.add_client_to_inbound(
                inbound_id=int(inbound_id),
                client_uuid=client_uuid,
                sub_uuid=sub_uuid,
                email=email,
                tg_id=tg_id,
                group_name=GROUP_PREMIUM,
                total_gb=new_total_gb,
                expiry_time=new_expiry_ms,
                limit_ip=limit_ip,
            )

        return sub_uuid, email, new_expiry_ms, new_total_gb

    def get_user_subscriptions_summary(self, tg_id: int) -> str:
        """Собирает сводку по подпискам и остаткам лимитов пользователя из панели"""
        all_inbounds = self.get_all_inbounds()
        promo_email = f"tg_{tg_id}"
        prem_email = f"prem_{tg_id}"

        promo_found = False
        promo_sub_id = None
        promo_used_bytes = 0

        prem_found = False
        prem_sub_id = None
        prem_used_bytes = 0
        prem_total_bytes = PREMIUM_TOTAL_GB
        prem_expiry = 0

        for ib in all_inbounds:
            client_stats = ib.get("clientStats") or []
            stats_by_email = {
                st.get("email"): st for st in client_stats if isinstance(st, dict) and st.get("email")
            }

            for cl in self._parse_clients(ib):
                em = cl.get("email")
                st = stats_by_email.get(em, {})
                used = int(st.get("up") or 0) + int(st.get("down") or 0)

                if em == promo_email:
                    promo_found = True
                    promo_sub_id = promo_sub_id or cl.get("subId") or cl.get("id")
                    promo_used_bytes += used

                elif em == prem_email:
                    prem_found = True
                    prem_sub_id = prem_sub_id or cl.get("subId") or cl.get("id")
                    prem_used_bytes += used
                    cl_expiry = int(cl.get("expiryTime") or 0)
                    if cl_expiry > prem_expiry:
                        prem_expiry = cl_expiry
                    cl_total = _normalize_total_bytes(cl.get("totalGB") or 0)
                    if cl_total > 0:
                        prem_total_bytes = cl_total

        lines = [f"📊 **Ваши подписки:**\n"]

        if promo_found:
            promo_link = f"{XUI_PUBLIC_URL}/pablodirov/{promo_sub_id}"
            lines.append(
                f"🎁 **Промо:**\n"
                f"• Использовано трафика: `{format_bytes(promo_used_bytes)}`\n"
                f"• Лимит: Безлимит | Устройства: до {PROMO_LIMIT_IP} IP\n"
                f"• Ссылка: `{promo_link}`\n"
            )
        else:
            lines.append("🎁 **Промо:** не оформлен\n")

        if prem_found:
            prem_link = f"{XUI_PUBLIC_URL}/pablodirov/{prem_sub_id}"
            rem_bytes = max(0, prem_total_bytes - prem_used_bytes)
            lines.append(
                f"💎 **Премиум:**\n"
                f"• Использовано: `{format_bytes(prem_used_bytes)}`\n"
                f"• Остаток лимита: `{format_bytes(rem_bytes)}` из `{format_bytes(prem_total_bytes)}`\n"
                f"• Действует до: `{format_expiry_human(prem_expiry)}`\n"
                f"• Ссылка: `{prem_link}`\n"
            )
        else:
            lines.append("💎 **Премиум:** не оформлен\n")

        lines.append(TERMS_NOTICE.strip())
        return "\n".join(lines)


try:
    xui = XUIClient(XUI_API_URL, API_TOKEN)
    logging.info("Клиент 3x-ui успешно инициализирован по токену!")
except Exception as e:
    logging.error("Ошибка инициализации X-UI")
    exit(1)


def build_qr_photo(link: str) -> BufferedInputFile:
    qr_img = qrcode.make(link)
    bio = BytesIO()
    qr_img.save(bio, "PNG")
    bio.seek(0)
    return BufferedInputFile(bio.read(), filename="qr.png")


async def send_promo_access(target_message: types.Message, tg_id: int):
    async with user_locks[tg_id]:
        sub_uuid, email, already_exists = await panel_call(xui.process_user_promo, tg_id)

    sub_link = f"{XUI_PUBLIC_URL}/pablodirov/{sub_uuid}"
    photo = build_qr_photo(sub_link)

    header_text = (
        "ℹ️ **Промо-доступ обновлён:**\n\n"
        if already_exists
        else "✅ **Ваш промо-доступ успешно создан!**\n\n"
    )

    await target_message.answer_photo(
        photo=photo,
        caption=(
            header_text
            + "♾ **Трафик:** Безлимит\n"
            f"📱 **Устройства:** До {PROMO_LIMIT_IP} IP одновременно\n\n"
            "🔗 **Ваша ссылка на подписку:**\n"
            f"`{sub_link}`\n\n"
            "Рекомендуемые клиенты: Happ, Streisland, Shadowrocket, Eclave, v2rayNG, INCY, Throne.\n"
            "NO TORRENT! NO SPAM! Иначе - БАН!\n"
            f"{TERMS_NOTICE}"
        ),
        parse_mode="Markdown",
    )


async def send_premium_get_or_sync(target_message: types.Message, tg_id: int):
    async with user_locks[tg_id]:
        sub_uuid, email, expiry_ms, already_exists = await panel_call(
            xui.get_or_sync_user_premium_3d, tg_id
        )

    sub_link = f"{XUI_PUBLIC_URL}/pablodirov/{sub_uuid}"

    if already_exists:
        await target_message.answer(
            "🔄 **Премиум-профиль уже существует — подписка актуализирована!**\n"
            f"Чтобы продлить на **+{PREMIUM_RENEW_DAYS} дней**, нажмите **«🔄 Продлить премиум (+30 дней)»**.\n\n"
            f"⏳ **Действует до:** `{format_expiry_human(expiry_ms)}`\n"
            f"🔗 **Ссылка:** `{sub_link}`\n\n"
            f"{TERMS_NOTICE}",
            parse_mode="Markdown",
        )
        return

    photo = build_qr_photo(sub_link)
    await target_message.answer_photo(
        photo=photo,
        caption=(
            f"💎 **Ваш бесплатный премиум-доступ на {PREMIUM_INITIAL_DAYS} дня создан!**\n\n"
            "📦 **Лимит трафика:** 1 ТБ\n"
            f"⏳ **Действует до:** `{format_expiry_human(expiry_ms)}`\n"
            f"📱 **Устройства:** До {PREMIUM_LIMIT_IP} IP одновременно\n\n"
            "🔗 **Ваша премиум-ссылка на подписку:**\n"
            f"`{sub_link}`\n\n"
            f"{TERMS_NOTICE}"
        ),
        parse_mode="Markdown",
    )


async def send_renew_result(message, result):
    sub_uuid, email, expiry, total = result
    link = f"{XUI_PUBLIC_URL}/pablodirov/{sub_uuid}"
    await message.answer_photo(photo=build_qr_photo(link), caption=(
        f"✅ Премиум активирован / продлён на {PREMIUM_RENEW_DAYS} дней!\n"
        f"Лимит: {format_bytes(total)}\nДо: {format_expiry_human(expiry)}\n"
        f"Ссылка: `{link}`\n{TERMS_NOTICE}"), parse_mode="Markdown")

# Подписанный счёт: привязка к пользователю, цене и сроку действия.
def make_payload(tg_id):
    body = f"p1:{tg_id}:{STARS_RENEW_PRICE}:{int(time.time())}:{uuid.uuid4().hex[:16]}"
    sig = hmac.new(BOT_TOKEN.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return body + ":" + sig

def valid_payment(payload, tg_id, currency, amount, checkout=False):
    try:
        version, uid, price, ts, nonce, sig = payload.split(":")
        body = payload.rsplit(":", 1)[0]
        expected = hmac.new(BOT_TOKEN.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
        return (version == "p1" and hmac.compare_digest(sig, expected)
                and int(uid) == tg_id and currency == "XTR" and int(price) == amount
                and amount > 0 and (not checkout or (amount == STARS_RENEW_PRICE
                and 0 <= time.time()-int(ts) <= 3600)))
    except (ValueError, TypeError):
        return False

async def trigger_premium_renew_action(target_message, tg_id):
    if not RENEW_REQUIRES_STARS:
        async with user_locks[tg_id]:
            result = await panel_call(xui.renew_user_premium_by_recreate, tg_id)
        await send_renew_result(target_message, result)
        return
    await target_message.answer_invoice(
        title="Премиум на 30 дней", description="Создание или продление подписки на 30 дней, 1 ТБ плюс остаток.",
        payload=make_payload(tg_id), currency="XTR", provider_token="",
        prices=[LabeledPrice(label="30 дней", amount=STARS_RENEW_PRICE)])

@dp.pre_checkout_query()
async def on_pre_checkout_query(query):
    ok = valid_payment(query.invoice_payload, query.from_user.id, query.currency, query.total_amount, True)
    if ok:
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Счёт устарел или недействителен. Создайте новый.")

@dp.message(F.successful_payment)
async def on_successful_payment(message):
    p = message.successful_payment
    uid = message.from_user.id
    charge = p.telegram_payment_charge_id
    async with user_locks[uid]:
        if not charge:
            await message.answer("Не удалось идентифицировать платёж. Обратитесь в /paysupport.")
            return
        old = payment_store.records.get(charge)
        if old:
            await message.answer("Платёж уже учтён." if old["status"] == "completed" else "Платёж требует проверки. Обратитесь в /paysupport.")
            return
        record = {"tg_id": uid, "amount": p.total_amount, "payload": p.invoice_payload, "status": "review"}
        try:
            payment_store.put(charge, record)
            if not valid_payment(p.invoice_payload, uid, p.currency, p.total_amount):
                await message.answer(f"Платёж требует проверки. /paysupport. ID: {charge}")
                return
            payment_store.put(charge, {**record, "status": "processing"})
            result = await panel_call(xui.renew_user_premium_by_recreate, uid)
            payment_store.put(charge, {**record, "status": "completed"})
        except Exception:
            logging.error("Payment processing failed; manual reconciliation required")
            await message.answer(f"Оплата получена, но активация требует проверки. /paysupport. ID: {charge}")
            return
    # Ошибка доставки QR не должна приводить к повторному продлению.
    await send_renew_result(message, result)


async def send_manual_pdf(target_message: types.Message):
    if not os.path.isfile(MANUAL_FILE_PATH):
        await target_message.answer("❌ Файл руководства не найден на сервере.")
        return

    doc = FSInputFile(MANUAL_FILE_PATH, filename="Manual.pdf")
    await target_message.answer_document(
        document=doc,
        caption=f"📘 **Руководство пользования сетью**\n\n{TERMS_NOTICE}",
        parse_mode="Markdown",
    )


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    support_sessions.pop(message.from_user.id, None)
    await message.answer(
        f"Привет, {message.from_user.first_name}!\n\n"
        f"{TERMS_NOTICE}"
        "Выберите нужный раздел в меню:",
        reply_markup=get_main_reply_keyboard(),
    )
    await message.answer(
        "Быстрые действия:",
        reply_markup=get_start_inline_keyboard(),
    )


# --- Обработчики кнопок нижней клавиатуры ---
@dp.message(F.text == "📊 Моя подписка")
async def reply_my_sub(message: types.Message):
    try:
        summary = await panel_call(xui.get_user_subscriptions_summary, message.from_user.id)
        await message.answer(summary, parse_mode="Markdown")
    except Exception as e:
        logging.error("Ошибка получения сводки подписки")
        await message.answer("❌ Временная ошибка. Попробуйте позже или обратитесь в поддержку.")


@dp.message(F.text == "ℹ️ Инфо")
async def reply_info(message: types.Message):
    await message.answer(get_tariffs_info_text(), parse_mode="Markdown")


@dp.message(F.text == "📬 Контакты")
async def reply_contacts(message: types.Message):
    await message.answer(get_contacts_text(), parse_mode="Markdown")


@dp.message(F.text == "🎁 Промо-доступ")
async def reply_promo(message: types.Message):
    wait_msg = await message.answer("Обновляем промо-доступ...")
    try:
        await send_promo_access(message, message.from_user.id)
    except Exception as e:
        logging.error(
    "Ошибка создания промо-клиента: %s",
    type(e).__name__ + (
        ": " + str(e) if type(e) is RuntimeError else ""
    )
)
        await message.answer("❌ Временная ошибка. Попробуйте позже или обратитесь в поддержку.")
    finally:
        await wait_msg.delete()


@dp.message(F.text == "💎 Получить премиум")
async def reply_premium_get(message: types.Message):
    wait_msg = await message.answer(f"Проверяем / создаём премиум-доступ на {PREMIUM_INITIAL_DAYS} дня...")
    try:
        await send_premium_get_or_sync(message, message.from_user.id)
    except Exception as e:
        logging.error("Ошибка получения/синхронизации премиум-клиента")
        await message.answer("❌ Временная ошибка. Попробуйте позже или обратитесь в поддержку.")
    finally:
        await wait_msg.delete()


@dp.message(F.text == "🔄 Продлить премиум (+30 дней)")
async def reply_premium_renew(message: types.Message):
    try:
        await trigger_premium_renew_action(message, tg_id=message.from_user.id)
    except Exception as e:
        logging.error("Ошибка продления премиума")
        await message.answer("❌ Временная ошибка. Попробуйте позже или обратитесь в поддержку.")


@dp.message(F.text == "🌐 DNS-туннель (free)")
async def reply_dns_tunnel(message: types.Message):
    await message.answer(get_dns_tunnel_text(), parse_mode="Markdown", disable_web_page_preview=True)


@dp.message(F.text == "📘 Руководство пользования сетью")
async def reply_manual(message: types.Message):
    try:
        await send_manual_pdf(message)
    except Exception as e:
        logging.error("Ошибка отправки руководства")
        await message.answer("❌ Временная ошибка. Попробуйте позже или обратитесь в поддержку.")


# --- Обработчики inline-кнопок ---
@dp.callback_query(F.data == "my_sub")
async def cb_my_sub(callback: types.CallbackQuery):
    await callback.answer("Загружаем данные подписки...")
    try:
        summary = await panel_call(xui.get_user_subscriptions_summary, callback.from_user.id)
        await callback.message.answer(summary, parse_mode="Markdown")
    except Exception as e:
        logging.error("Ошибка получения сводки подписки")
        await callback.message.answer("❌ Временная ошибка. Попробуйте позже или обратитесь в поддержку.")


@dp.callback_query(F.data == "show_info")
async def cb_show_info(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer(get_tariffs_info_text(), parse_mode="Markdown")


@dp.callback_query(F.data == "show_contacts")
async def cb_show_contacts(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer(get_contacts_text(), parse_mode="Markdown")


@dp.callback_query(F.data == "get_promo")
async def cb_promo(callback: types.CallbackQuery):
    await callback.answer("Обновляем промо...")
    try:
        await send_promo_access(callback.message, callback.from_user.id)
    except Exception as e:
        logging.exception("Ошибка создания промо-клиента")
        await callback.message.answer("❌ Временная ошибка. Попробуйте позже или обратитесь в поддержку.")


@dp.callback_query(F.data == "get_premium")
async def cb_premium_get(callback: types.CallbackQuery):
    await callback.answer("Проверяем премиум...")
    try:
        await send_premium_get_or_sync(callback.message, callback.from_user.id)
    except Exception as e:
        logging.error("Ошибка получения/синхронизации премиум-клиента")
        await callback.message.answer("❌ Временная ошибка. Попробуйте позже или обратитесь в поддержку.")


@dp.callback_query(F.data == "renew_premium_1m")
async def cb_premium_renew(callback: types.CallbackQuery):
    await callback.answer("Обрабатываем продление...")
    try:
        await trigger_premium_renew_action(callback.message, tg_id=callback.from_user.id)
    except Exception as e:
        logging.error("Ошибка продления премиума")
        await callback.message.answer("❌ Временная ошибка. Попробуйте позже или обратитесь в поддержку.")


@dp.callback_query(F.data == "get_dns_tunnel")
async def cb_dns_tunnel(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer(get_dns_tunnel_text(), parse_mode="Markdown", disable_web_page_preview=True)


@dp.callback_query(F.data == "get_manual")
async def cb_manual(callback: types.CallbackQuery):
    await callback.answer("Отправляем руководство...")
    try:
        await send_manual_pdf(callback.message)
    except Exception as e:
        logging.error("Ошибка отправки руководства")
        await callback.message.answer("❌ Временная ошибка. Попробуйте позже или обратитесь в поддержку.")


# ==================== ПОДДЕРЖКА ====================
# Маршрут ответа подписан HMAC и хранится в сообщении бота у администратора.
# Поэтому ответы на старые обращения работают даже после перезапуска.
SUPPORT_ADMIN_ID = int(os.getenv("SUPPORT_ADMIN_ID", "0"))
support_sessions = {}
support_limits = {}
support_global = []
support_slots = asyncio.Semaphore(4)


def support_tag(uid):
    return hmac.new(BOT_TOKEN.encode(), f"support:{uid}".encode(), hashlib.sha256).hexdigest()[:24]


def support_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖ Завершить обращение", callback_data="support_close")]])


async def support_open(message, uid):
    if not SUPPORT_ADMIN_ID or uid == SUPPORT_ADMIN_ID:
        await message.answer("Для связи используйте контакты в разделе поддержки.")
        return
    now = time.monotonic()
    for key, expiry in list(support_sessions.items()):
        if expiry <= now:
            support_sessions.pop(key, None)
    if len(support_sessions) >= 5000 and uid not in support_sessions:
        await message.answer("Поддержка сейчас загружена. Попробуйте чуть позже.")
        return
    support_sessions[uid] = now + 1800
    await message.answer("✉️ Опишите проблему и приложите скриншоты, если нужно. Каждое сообщение отправляется администратору. Не присылайте пароли, QR-коды и ссылки подписок.\n\nДля выхода нажмите кнопку ниже или /cancel. Режим действует 30 минут.", reply_markup=support_keyboard())


@dp.callback_query(F.data == "support_open")
async def support_open_callback(callback):
    await callback.answer()
    await support_open(callback.message, callback.from_user.id)


@dp.message(Command("myid"))
async def support_myid(message):
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@dp.message(Command("support"))
async def support_open_command(message):
    await support_open(message, message.from_user.id)


@dp.callback_query(F.data == "support_close")
async def support_close_callback(callback):
    support_sessions.pop(callback.from_user.id, None)
    await callback.answer()
    await callback.message.answer("Режим отправки сообщений завершён. Ответ администратора всё равно придёт сюда.", reply_markup=get_main_reply_keyboard())


@dp.message(Command("cancel"))
async def support_cancel(message):
    support_sessions.pop(message.from_user.id, None)
    await message.answer("Режим отправки сообщений завершён.", reply_markup=get_main_reply_keyboard())


def support_media_ok(message):
    return bool(message.text or message.photo or (message.document and message.document.mime_type in {"image/png", "image/jpeg", "image/webp", "image/gif"} and (message.document.file_size or 0) <= 20 * 1024 * 1024))


def support_route(message):
    original = message.reply_to_message
    if not original or original.reply_to_message or not original.from_user or original.from_user.id != bot.id:
        return None
    # Заголовок создаёт только бот. Пользовательский контент копируется отдельно.
    text = original.text or ""
    first = text.split("\n", 1)[0]
    parts = first.split(":")
    if len(parts) != 3 or parts[0] != "SUPPORT" or not parts[1].isdigit():
        return None
    uid = int(parts[1])
    return uid if uid > 0 and uid != SUPPORT_ADMIN_ID and hmac.compare_digest(parts[2], support_tag(uid)) else None




@dp.message(F.from_user.id == SUPPORT_ADMIN_ID, F.reply_to_message)
async def support_admin_reply(message):
    uid = support_route(message)
    if not uid:
        await message.answer("Ответьте на заголовок обращения SUPPORT, расположенный перед сообщением пользователя.")
        return
    if not support_media_ok(message):
        await message.answer("Можно отправить текст или изображение.")
        return
    try:
        await bot.copy_message(chat_id=uid, from_chat_id=message.chat.id, message_id=message.message_id)
    except Exception as e:
        logging.warning("Support reply failed: %s", type(e).__name__)
        await message.answer("Ответ не доставлен. Возможно, пользователь заблокировал бота.")
        return
    await message.answer("✓ Ответ доставлен.")


def support_is_active(message):
    return bool(message.from_user and message.from_user.id != SUPPORT_ADMIN_ID and support_sessions.get(message.from_user.id, 0) > time.monotonic())


@dp.message(support_is_active)
async def support_receive(message):
    uid = message.from_user.id
    if not support_media_ok(message):
        await message.answer("Пришлите текст, фото или изображение файлом до 20 МБ.")
        return
    now = time.monotonic()
    for key, stamps in list(support_limits.items()):
        if not stamps or now - stamps[-1] >= 60:
            support_limits.pop(key, None)
    stamps = support_limits.setdefault(uid, [])
    stamps[:] = [t for t in stamps if now - t < 60]
    support_global[:] = [t for t in support_global if now - t < 60]
    if len(stamps) >= 8 or len(support_global) >= 40 or support_slots.locked():
        await message.answer("Сообщение пока не отправлено. Подождите немного и повторите отправку.")
        return
    stamps.append(now)
    support_global.append(now)
    user = message.from_user
    try:
        async with support_slots:
            header = await bot.send_message(SUPPORT_ADMIN_ID, f"SUPPORT:{uid}:{support_tag(uid)}\n✉️ Обращение от {user.full_name}\nTelegram ID: {uid}\nUsername: @{user.username}\nОтветьте на этот заголовок." if user.username else f"SUPPORT:{uid}:{support_tag(uid)}\n✉️ Обращение от {user.full_name}\nTelegram ID: {uid}\nОтветьте на этот заголовок.")
            try:
                await bot.copy_message(chat_id=SUPPORT_ADMIN_ID, from_chat_id=message.chat.id, message_id=message.message_id, reply_parameters=types.ReplyParameters(message_id=header.message_id))
            except Exception:
                try:
                    await header.delete()
                except Exception:
                    pass
                raise
    except Exception as e:
        logging.warning("Support delivery failed: %s", type(e).__name__)
        await message.answer("Не удалось доставить сообщение. Попробуйте позже или используйте контакты поддержки.")
        return
    await message.answer("✓ Сообщение передано администратору.")


async def on_startup(bot: Bot):
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())


async def main():
    dp.startup.register(on_startup)
    logging.info("Бот запущен и ожидает сообщения...")
    await dp.start_polling(bot, tasks_concurrency_limit=64)


if __name__ == "__main__":
    asyncio.run(main())
