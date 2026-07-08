"""
services/subscription.py — منطق کسب‌وکار ایجاد اشتراک
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database.crud import create_subscription, get_user_subscriptions
from database.models import Subscription
from services.xui_api import XUIClient, XUIError
from utils.qrcode_gen import generate_qr_code


# ──────────────────────────────────────────────
# نتیجه ایجاد اشتراک
# ──────────────────────────────────────────────

@dataclass
class NewSubscriptionResult:
    subscription: Subscription       # رکورد ذخیره‌شده در دیتابیس
    sub_link: str                    # لینک subscription (برای import همه کانفیگ‌ها)
    qr_bytes: bytes                  # QR Code از sub_link
    client_uuid: str                 # UUID کلاینت در پنل
    email: str                       # ایمیل منحصر به فرد در پنل
    config_links: list[str]          # لیست کانفیگ‌های کامل (vless://... vmess://...)
    limit_ip: int = 0                # محدودیت IP همزمان (0 = نامحدود)


# ──────────────────────────────────────────────
# تولید ایمیل منحصر به فرد
# ──────────────────────────────────────────────

_COUNTER_KEY_CLIENT = "sub_counter_client"
_COUNTER_KEY_GIFT   = "sub_counter_gift"


async def _next_email(session: AsyncSession, is_gift: bool) -> str:
    """
    شمارنده اتمی از DB می‌خواند و email منحصربه‌فرد برمی‌گرداند.
    خرید عادی  → client-1, client-2, ...
    اشتراک تست → Gift-1, Gift-2, ...
    """
    from database.crud import get_setting, set_setting
    key = _COUNTER_KEY_GIFT if is_gift else _COUNTER_KEY_CLIENT
    raw = await get_setting(session, key, "0")
    n = int(raw) + 1
    await set_setting(session, key, str(n))
    prefix = "Gift" if is_gift else "client"
    return f"{prefix}-{n}"


async def _sync_counter_with_panel(session: AsyncSession, xui: "XUIClient") -> None:
    """
    شمارنده‌های DB را با وضعیت واقعی پنل همگام می‌کند.
    اگر در پنل client-50 وجود داشت، counter_client را به ≥50 می‌برد.
    این تابع یک‌بار در ابتدای ایجاد اشتراک صدا زده می‌شود.
    """
    from database.crud import get_setting, set_setting
    try:
        all_clients = await xui.get_all_clients()
        max_client = 0
        max_gift = 0
        for c in all_clients:
            email = c.email.strip()
            if email.startswith("client-"):
                try:
                    n = int(email[len("client-"):])
                    max_client = max(max_client, n)
                except ValueError:
                    pass
            elif email.lower().startswith("gift-"):
                try:
                    n = int(email[5:])
                    max_gift = max(max_gift, n)
                except ValueError:
                    pass

        # فقط اگر پنل شماره بالاتری داشت counter را به‌روز کن
        if max_client > 0:
            current = int(await get_setting(session, _COUNTER_KEY_CLIENT, "0"))
            if max_client >= current:
                await set_setting(session, _COUNTER_KEY_CLIENT, str(max_client))
                logger.info(f"counter_client همگام شد با پنل: {max_client}")

        if max_gift > 0:
            current_g = int(await get_setting(session, _COUNTER_KEY_GIFT, "0"))
            if max_gift >= current_g:
                await set_setting(session, _COUNTER_KEY_GIFT, str(max_gift))
                logger.info(f"counter_gift همگام شد با پنل: {max_gift}")
    except Exception as e:
        logger.warning(f"همگام‌سازی counter با پنل ناموفق (ادامه می‌دهیم): {e}")


# ──────────────────────────────────────────────
# سرویس اصلی ایجاد اشتراک
# ──────────────────────────────────────────────

async def create_new_subscription(
    session: AsyncSession,
    user_id: int,
    telegram_id: int,
    inbound_id: int,
    traffic_gb: int = 0,
    expire_days: int = 0,
    is_gift: bool = False,
    plan_id: int = 0,
) -> NewSubscriptionResult:
    """
    ایجاد اشتراک جدید — flow کامل:
      1. دریافت inbound از پنل
      2. تولید email منحصر به فرد
      3. ایجاد client در پنل از طریق XUIClient
      4. ذخیره در دیتابیس
      5. تولید subscription link + QR Code

    Args:
        session: AsyncSession دیتابیس
        user_id: کلید اولیه User در دیتابیس
        telegram_id: آی‌دی تلگرام (برای ایمیل)
        inbound_id: شناسه inbound در پنل (0 = انتخاب خودکار)
        traffic_gb: محدودیت ترافیک (0=نامحدود)
        expire_days: مدت اعتبار روز (0=پیش‌فرض از config)
        plan_id: شناسه پلن در دیتابیس (اگر داده شود اینباندهای اختصاصی پلن اولویت دارند)

    Returns:
        NewSubscriptionResult

    Raises:
        XUIError: در صورت خطا از پنل
    """
    # ── خواندن مشخصات پلن از DB (اولویت بالاتر از پارامترهای پیش‌فرض) ──────────
    # اگر plan_id داده شده، حجم / مدت / limit_ip را از پلن بخوان.
    # این مهم‌ترین فیکس است: بدون این، وقتی payments.py فقط plan_id می‌فرستد
    # و traffic_gb/expire_days را نمی‌فرستد، مقادیر پیش‌فرض config اعمال می‌شود
    # نه مقادیر پلن خریداری‌شده.
    from database.crud import get_enabled_inbound_ids, get_plan
    _plan_obj_cache: object = None
    if plan_id:
        _plan_obj_cache = await get_plan(session, plan_id)
        if _plan_obj_cache:
            # حجم: اگه caller مقدار صریح نداده (0 = نداده)
            if traffic_gb == 0 and _plan_obj_cache.traffic_gb >= 0:  # type: ignore[union-attr]
                traffic_gb = _plan_obj_cache.traffic_gb  # type: ignore[union-attr]
            # مدت: اگه caller مقدار صریح نداده (0 = نداده)
            if expire_days == 0 and _plan_obj_cache.duration_days > 0:  # type: ignore[union-attr]
                expire_days = _plan_obj_cache.duration_days  # type: ignore[union-attr]

    # استفاده از مقادیر پیش‌فرض config فقط اگر نه پلن و نه caller مقداری نداده
    if traffic_gb == 0 and not (plan_id and _plan_obj_cache):
        traffic_gb = settings.default_traffic_gb
    if expire_days == 0:
        expire_days = settings.default_subscription_days

    # ── انتخاب اینباندهای هدف ─────────────────────────────
    # اولویت‌بندی:
    #   1. inbound_id صریح (مثلاً ایجاد دستی ادمین)
    #   2. اینباندهای اختصاصی پلن (plan.inbound_ids)
    #   3. اینباندهای فعال عمومی (adm_inbounds)
    #   4. fallback: اینباند 1
    enabled_ids = await get_enabled_inbound_ids(session)

    if inbound_id != 0:
        # اگه صریحاً یه اینباند داده شده، فقط همون
        target_inbound_ids = [inbound_id]
    else:
        # بررسی اینباندهای اختصاصی پلن
        plan_specific_ids: list[int] = []
        if plan_id:
            plan_obj = _plan_obj_cache or await get_plan(session, plan_id)
            if plan_obj:
                plan_specific_ids = plan_obj.get_inbound_ids()  # type: ignore[union-attr]

        if plan_specific_ids:
            # اینباندهای اختصاصی پلن — فقط همون‌ها
            target_inbound_ids = plan_specific_ids
            logger.info(f"اینباندهای اختصاصی پلن {plan_id}: {target_inbound_ids}")
        elif enabled_ids:
            # اینباندهای فعال عمومی
            target_inbound_ids = enabled_ids
        else:
            # fallback: اینباند 1
            target_inbound_ids = [1]

    # ── خواندن limit_ip از پلن ──────────────────────────────
    # limit_ip = محدودیت تعداد IP همزمان در پنل سنایی (limitIp)
    plan_limit_ip = 0
    if plan_id:
        plan_obj_for_ip = _plan_obj_cache or await get_plan(session, plan_id)
        if plan_obj_for_ip:
            plan_limit_ip = plan_obj_for_ip.limit_ip or 0  # type: ignore[union-attr]

    logger.info(
        f"اینباندهای هدف برای اشتراک: {target_inbound_ids} | "
        f"traffic_gb={traffic_gb} | expire_days={expire_days} | limit_ip={plan_limit_ip}"
    )

    # ── ایجاد client در پنل — در همه اینباندهای فعال ──
    async with XUIClient(
        panel_url=settings.panel_url,
        username=settings.panel_username,
        password=settings.panel_password,
        api_path=settings.panel_api_path,
        sub_port=settings.sub_port,
    ) as xui:
        # همگام‌سازی شمارنده با پنل (از تکراری بودن email جلوگیری می‌کند)
        await _sync_counter_with_panel(session, xui)

        # اینباند اول برای دریافت sub_id و اطلاعات اصلی
        first_inbound_id = target_inbound_ids[0]
        inbound = await xui.get_inbound(first_inbound_id)
        if not inbound.enable:
            # اگه اینباند اول غیرفعال بود، اولی رو که فعاله پیدا کن
            for iid in target_inbound_ids[1:]:
                ib = await xui.get_inbound(iid)
                if ib.enable:
                    first_inbound_id = iid
                    break
            else:
                raise XUIError("هیچ اینباند فعالی پیدا نشد.")

        # ── تولید email و retry در صورت تکراری بودن ──────────────
        # حداکثر MAX_RETRY بار تلاش می‌کنیم تا email آزاد پیدا شود
        MAX_RETRY = 10
        client_info = None
        email = ""
        for attempt in range(MAX_RETRY):
            email = await _next_email(session, is_gift=is_gift)
            logger.info(f"ایجاد اشتراک: user_id={user_id}, inbounds={target_inbound_ids}, email={email} (تلاش {attempt+1})")
            try:
                client_info = await xui.add_client(
                    inbound_id=first_inbound_id,
                    email=email,
                    traffic_gb=traffic_gb,
                    expire_days=expire_days,
                    tg_id=telegram_id,
                    limit_ip=plan_limit_ip,
                )
                break  # موفق شد
            except XUIError as e:
                err_str = str(e).lower()
                if "already in use" in err_str or "email" in err_str:
                    logger.warning(f"email '{email}' تکراری است، تلاش بعدی...")
                    continue  # ایمیل بعدی را امتحان کن
                raise  # خطای دیگری است، رای داده بشه

        if client_info is None:
            raise XUIError(f"پس از {MAX_RETRY} تلاش نتوانستیم email آزادی پیدا کنیم.")

        # اضافه کردن همون کلاینت (با همون sub_id و email) به بقیه اینباندها
        for extra_iid in target_inbound_ids[1:]:
            try:
                ib = await xui.get_inbound(extra_iid)
                if not ib.enable:
                    continue
                await xui.add_client(
                    inbound_id=extra_iid,
                    email=email,
                    traffic_gb=traffic_gb,
                    expire_days=expire_days,
                    tg_id=telegram_id,
                    sub_id=client_info.sub_id,  # همون sub_id تا sub_link یکی باشه
                    limit_ip=plan_limit_ip,
                )
                logger.info(f"کلاینت '{email}' به اینباند اضافی {extra_iid} اضافه شد.")
            except Exception as e:
                logger.warning(f"اضافه کردن به اینباند {extra_iid} ناموفق: {e}")

        # لینک sub برای import کل اشتراک (یه لینک = همه اینباندها)
        sub_link = xui.build_sub_link(client_info.sub_id)

        # دریافت کانفیگ‌های تکی
        config_links = await xui.get_client_links(email)
        if not config_links:
            config_links = await xui.get_sub_links(client_info.sub_id)

    # inbound_id اصلی برای ذخیره در DB
    inbound_id = first_inbound_id

    # ── محاسبه تاریخ انقضا ──────────────────
    expiry_date: Optional[datetime] = None
    if expire_days > 0:
        expiry_date = datetime.now(timezone.utc) + timedelta(days=expire_days)

    # ── ذخیره در دیتابیس ────────────────────
    # client_info.id از API جدید ممکن است خالی باشد (uuid فقط در /clients/list است)
    # sub_id همیشه موجود است و برای بازیابی لینک کافی است
    db_sub = await create_subscription(
        session=session,
        user_id=user_id,
        email=email,
        client_uuid=client_info.uuid or client_info.sub_id,
        sub_id=client_info.sub_id,
        inbound_id=inbound_id,
        traffic_limit_gb=traffic_gb,
        expiry_date=expiry_date,
        limit_ip=plan_limit_ip,
    )

    # ── تولید QR Code ────────────────────────
    qr_bytes = await generate_qr_code(sub_link)
    logger.success(f"اشتراک ایجاد شد: email={email}, link={sub_link}")

    return NewSubscriptionResult(
        subscription=db_sub,
        sub_link=sub_link,
        qr_bytes=qr_bytes,
        client_uuid=client_info.uuid or client_info.sub_id,
        email=email,
        config_links=config_links,
        limit_ip=plan_limit_ip,
    )


async def get_subscriptions_status(
    session: AsyncSession,
    user_id: int,
) -> list[Subscription]:
    """دریافت لیست اشتراک‌های فعال کاربر."""
    return await get_user_subscriptions(session, user_id, active_only=True)
