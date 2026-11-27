import asyncio
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
INBOUND_IDS = [18, 20, 27]

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
user_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


class PaymentStore:
    """Простое файловое хранилище обработанных ID платежей Telegram Stars"""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self._ids: set[str] = set()
        self._load()

    def _load(self):
        if os.path.isfile(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self._ids = {str(x) for x in data}
            except Exception as e:
                logging.error(f"Не удалось прочитать {self.filepath}: {e}")

    def _save(self):
        parent = os.path.dirname(os.path.abspath(self.filepath))
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp_path = f"{self.filepath}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(sorted(self._ids), f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.filepath)

    def has_processed(self, charge_id: str) -> bool:
        return charge_id in self._ids

    def mark_processed(self, charge_id: str):
        if charge_id:
            self._ids.add(charge_id)
            self._save()


payment_store = PaymentStore(PROCESSED_PAYMENTS_FILE)


def get_main_reply_keyboard() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура внизу экрана при входе в чат"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📊 Моя подписка"),
                KeyboardButton(text="ℹ️ Инфо"),
            ],
            [
                KeyboardButton(text="🎁 Промо-доступ"),
                KeyboardButton(text="💎 Получить премиум"),
            ],
            [
                KeyboardButton(text="🔄 Продлить премиум (+30 дней)"),
                KeyboardButton(text="🌐 DNS-туннель (free)"),
            ],
            [
                KeyboardButton(text="📘 Руководство пользования сетью"),
                KeyboardButton(text="📬 Контакты"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите действие в меню ниже...",
    )


def get_start_inline_keyboard() -> InlineKeyboardMarkup:
    """Инлайн-кнопки в приветственном сообщении"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Моя подписка", callback_data="my_sub")],
            [InlineKeyboardButton(text="ℹ️ Инфо", callback_data="show_info")],
            [InlineKeyboardButton(text="🎁 Промо-доступ", callback_data="get_promo")],
            [InlineKeyboardButton(text="💎 Получить премиум", callback_data="get_premium")],
            [InlineKeyboardButton(text="🔄 Продлить премиум (+30 дней)", callback_data="renew_premium_1m")],
            [InlineKeyboardButton(text="🌐 DNS-туннель (free)", callback_data="get_dns_tunnel")],
            [InlineKeyboardButton(text="📘 Руководство пользования сетью", callback_data="get_manual")],
            [InlineKeyboardButton(text="📬 Контакты", callback_data="show_contacts")],
        ]
    )


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
            logging.error(f"Не удалось прочитать {RESOLVERS_FILE_PATH}: {e}")
            resolvers_block = f"📋 **Список резолверов:** ошибка чтения файла (`{e}`)\n\n"
    else:
        resolvers_block = f"📋 **Список резолверов:** файл `{RESOLVERS_FILE_PATH}` не найден.\n\n"

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
        self.session = requests.Session()
        self.session.verify = True
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

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
        return []

    def get_client_from_inbound(self, inbound_id: int, email: str) -> dict | None:
        """Ищет клиента в конкретном инбаунде по email"""
        url = f"{self.base_url}/panel/api/inbounds/get/{int(inbound_id)}"
        res = self.session.get(url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data.get("success"):
                for client in self._parse_clients(data.get("obj", {})):
                    if client.get("email") == email:
                        return client
        return None

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
            logging.warning(f"Не удалось добавить {email} в inbound {inbound_id}: {res.text}")
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

        logging.warning(
            f"Не удалось удалить клиента {email} (inbound={inbound_id}): "
            f"POST={res.status_code} {res.text.strip()} | DELETE={res_del.status_code} {res_del.text.strip()}"
        )
        return False

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
            return None

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
    logging.error(f"Ошибка инициализации X-UI: {e}")
    exit(1)


def build_qr_photo(link: str) -> BufferedInputFile:
    qr_img = qrcode.make(link)
    bio = BytesIO()
    qr_img.save(bio, "PNG")
    bio.seek(0)
    return BufferedInputFile(bio.read(), filename="qr.png")


async def send_promo_access(target_message: types.Message, tg_id: int):
    async with user_locks[tg_id]:
        sub_uuid, email, already_exists = await asyncio.to_thread(xui.process_user_promo, tg_id)

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
        sub_uuid, email, expiry_ms, already_exists = await asyncio.to_thread(
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


async def send_premium_renew(target_message: types.Message, tg_id: int):
    async with user_locks[tg_id]:
        result = await asyncio.to_thread(xui.renew_user_premium_by_recreate, tg_id)

    if not result:
        await target_message.answer(
            "⚠️ У вас ещё нет премиум-профиля. Сначала нажмите **«💎 Получить премиум»** (бесплатно на 3 дня).\n\n"
            f"{TERMS_NOTICE}",
            parse_mode="Markdown",
        )
        return

    sub_uuid, email, new_expiry_ms, new_total_gb = result
    sub_link = f"{XUI_PUBLIC_URL}/pablodirov/{sub_uuid}"
    photo = build_qr_photo(sub_link)

    await target_message.answer_photo(
        photo=photo,
        caption=(
            f"🔄 **Премиум-доступ продлён на +{PREMIUM_RENEW_DAYS} дней!**\n"
            f"📦 **Новый лимит трафика:** `{format_bytes(new_total_gb)}` (с учётом остатка)\n"
            f"⏳ **Новый срок действия до:** `{format_expiry_human(new_expiry_ms)}`\n\n"
            "🔗 **Ваша ссылка на подписку:**\n"
            f"`{sub_link}`\n\n"
            f"{TERMS_NOTICE}"
        ),
        parse_mode="Markdown",
    )


async def trigger_premium_renew_action(target_message: types.Message, tg_id: int):
    """Запускает продление напрямую (если RENEW_REQUIRES_STARS=False) или выставляет счёт в Stars"""
    if not RENEW_REQUIRES_STARS:
        await send_premium_renew(target_message, tg_id)
        return

    payload = f"prem_stars:renew:{tg_id}:{int(time.time())}"
    await target_message.answer_invoice(
        title=f"Продление Премиум (+{PREMIUM_RENEW_DAYS} дней)",
        description="Продление срока действия на +30 дней и добавление остатка трафика. Сервис As Is.",
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label="Продление на 30 дней", amount=STARS_RENEW_PRICE)],
    )


@dp.pre_checkout_query()
async def on_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)


@dp.message(F.successful_payment)
async def on_successful_payment(message: types.Message):
    payment = message.successful_payment
    charge_id = payment.telegram_payment_charge_id or ""
    tg_id = message.from_user.id
    payload = payment.invoice_payload or ""

    if charge_id and payment_store.has_processed(charge_id):
        logging.warning(f"Повторный вебхук/сообщение по уже обработанному charge_id={charge_id}")
        await message.answer("ℹ️ Этот платёж уже был учтён ранее.")
        return

    logging.info(
        f"Оплата Stars принята: tg_id={tg_id}, amount={payment.total_amount} {payment.currency}, "
        f"charge_id={charge_id}, payload={payload}"
    )

    try:
        await send_premium_renew(message, tg_id)
        if charge_id:
            payment_store.mark_processed(charge_id)
    except Exception as e:
        logging.error(f"Ошибка продления после оплаты Stars (charge_id={charge_id}): {e}")
        await message.answer(
            f"✅ Оплата прошла, но при обновлении панели возникла ошибка: `{e}`\n"
            f"ID платежа для обращения: `{charge_id}`",
            parse_mode="Markdown",
        )


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
        summary = await asyncio.to_thread(xui.get_user_subscriptions_summary, message.from_user.id)
        await message.answer(summary, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Ошибка получения сводки подписки: {e}")
        await message.answer(f"❌ Не удалось получить данные подписки: {e}")


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
        logging.error(f"Ошибка создания промо-клиента: {e}")
        await message.answer(f"❌ Произошла ошибка: {e}")
    finally:
        await wait_msg.delete()


@dp.message(F.text == "💎 Получить премиум")
async def reply_premium_get(message: types.Message):
    wait_msg = await message.answer(f"Проверяем / создаём премиум-доступ на {PREMIUM_INITIAL_DAYS} дня...")
    try:
        await send_premium_get_or_sync(message, message.from_user.id)
    except Exception as e:
        logging.error(f"Ошибка получения/синхронизации премиум-клиента: {e}")
        await message.answer(f"❌ Произошла ошибка: {e}")
    finally:
        await wait_msg.delete()


@dp.message(F.text == "🔄 Продлить премиум (+30 дней)")
async def reply_premium_renew(message: types.Message):
    try:
        await trigger_premium_renew_action(message, tg_id=message.from_user.id)
    except Exception as e:
        logging.error(f"Ошибка продления премиума: {e}")
        await message.answer(f"❌ Не удалось выполнить продление: {e}")


@dp.message(F.text == "🌐 DNS-туннель (free)")
async def reply_dns_tunnel(message: types.Message):
    await message.answer(get_dns_tunnel_text(), parse_mode="Markdown", disable_web_page_preview=True)


@dp.message(F.text == "📘 Руководство пользования сетью")
async def reply_manual(message: types.Message):
    try:
        await send_manual_pdf(message)
    except Exception as e:
        logging.error(f"Ошибка отправки руководства: {e}")
        await message.answer(f"❌ Не удалось отправить файл: {e}")


# --- Обработчики inline-кнопок ---
@dp.callback_query(F.data == "my_sub")
async def cb_my_sub(callback: types.CallbackQuery):
    await callback.answer("Загружаем данные подписки...")
    try:
        summary = await asyncio.to_thread(xui.get_user_subscriptions_summary, callback.from_user.id)
        await callback.message.answer(summary, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Ошибка получения сводки подписки: {e}")
        await callback.message.answer(f"❌ Не удалось получить данные подписки: {e}")


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
        logging.error(f"Ошибка создания промо-клиента: {e}")
        await callback.message.answer(f"❌ Произошла ошибка: {e}")


@dp.callback_query(F.data == "get_premium")
async def cb_premium_get(callback: types.CallbackQuery):
    await callback.answer("Проверяем премиум...")
    try:
        await send_premium_get_or_sync(callback.message, callback.from_user.id)
    except Exception as e:
        logging.error(f"Ошибка получения/синхронизации премиум-клиента: {e}")
        await callback.message.answer(f"❌ Произошла ошибка: {e}")


@dp.callback_query(F.data == "renew_premium_1m")
async def cb_premium_renew(callback: types.CallbackQuery):
    await callback.answer("Обрабатываем продление...")
    try:
        await trigger_premium_renew_action(callback.message, tg_id=callback.from_user.id)
    except Exception as e:
        logging.error(f"Ошибка продления премиума: {e}")
        await callback.message.answer(f"❌ Не удалось выполнить продление: {e}")


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
        logging.error(f"Ошибка отправки руководства: {e}")
        await callback.message.answer(f"❌ Не удалось отправить файл: {e}")


async def on_startup(bot: Bot):
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())


async def main():
    dp.startup.register(on_startup)
    logging.info("Бот запущен и ожидает сообщения...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
