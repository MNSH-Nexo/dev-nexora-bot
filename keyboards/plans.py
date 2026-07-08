"""
keyboards/plans.py — Inline Keyboard برای نمایش پلن‌ها و خرید
پلن‌ها از دیتابیس خوانده می‌شوند (نه مستقیم از پنل)
"""

from __future__ import annotations

from typing import List

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database.models import Plan


_FARSI_USERS = {
    1: "یک کاربره",
    2: "دو کاربره",
    3: "سه کاربره",
    4: "چهار کاربره",
    5: "پنج کاربره",
}


def _fmt_usdt(price: float) -> str:
    """
    نمایش هوشمند قیمت دلاری — بدون صفر اضافه، بدون برش اشتباه.
    مثال‌ها:
      5.0   → "5"
      3.5   → "3.5"
      0.03  → "0.03"
      0.005 → "0.005"
    """
    if price == int(price):
        return f"{int(price)}"
    # حذف صفرهای انتهایی با g، سپس اطمینان از حداقل ۲ رقم اعشار برای اعداد خیلی کوچک
    formatted = f"{price:g}"
    # اگه اعشار نداره (مثل 1e-05) به شکل دسیمال نمایش بده
    if "e" in formatted or "E" in formatted:
        # پیدا کردن تعداد رقم معنادار لازم
        decimals = max(2, -int(f"{price:.0e}".split("e")[1]) + 1)
        formatted = f"{price:.{decimals}f}".rstrip("0")
        if formatted.endswith("."):
            formatted += "0"
    return formatted


def _plan_label(plan: Plan, rate: int = 0) -> str:
    """
    متن دکمه پلن — نام پلن + جزئیات کامل.
    اگر rate > 0 قیمت تومانی هم نمایش داده می‌شود.
    مثال:  📦 پلن برنزی  —  ۱۰ گیگ  ◈  ۳۰ روزه  ◈  ۳$ (۲۷۰،۰۰۰ت)
    """
    if plan.traffic_gb == 0:
        if plan.limit_ip and plan.limit_ip > 0:
            user_label = _FARSI_USERS.get(plan.limit_ip, f"{plan.limit_ip} کاربره")
            traffic_part = f"نامحدود — {user_label}"
            icon = "♾"
        else:
            traffic_part = "نامحدود"
            icon = "♾"
    else:
        traffic_part = f"{plan.traffic_gb} گیگ"
        icon = "📦"

    price_str = _fmt_usdt(plan.price_usdt)
    if rate > 0:
        toman = int(plan.price_usdt * rate)
        toman_str = f"{toman:,}".replace(",", "،")
        price_part = f"{price_str}$ ({toman_str}ت)"
    else:
        price_part = f"{price_str} دلار"
    return f"{icon} {plan.name}  —  {traffic_part}  ◈  {plan.duration_days} روزه  ◈  {price_part}"


def get_plans_keyboard(plans: List[Plan], rate: int = 0) -> InlineKeyboardMarkup:
    """
    Inline keyboard مرتب از لیست پلن‌ها.
    پلن‌های حجمی و نامحدود با جداکننده از هم تفکیک می‌شوند.
    rate: نرخ USDT به تومان — اگر صفر باشد فقط دلار نمایش داده می‌شود.
    """
    builder = InlineKeyboardBuilder()
    if not plans:
        builder.button(text="⚠️ در حال حاضر پلنی موجود نیست", callback_data="no_plans")
        builder.row(InlineKeyboardButton(text="🔙 بازگشت به منو", callback_data="back_main"))
        return builder.as_markup()

    # تفکیک پلن‌های حجمی از نامحدود
    limited = [p for p in plans if p.traffic_gb > 0]
    unlimited = [p for p in plans if p.traffic_gb == 0]

    if limited:
        builder.row(InlineKeyboardButton(
            text="📦  پلن‌های حجمی", callback_data="no_plans"
        ))
        for plan in limited:
            builder.row(InlineKeyboardButton(
                text=_plan_label(plan, rate), callback_data=f"plan:{plan.id}"
            ))

    if unlimited:
        builder.row(InlineKeyboardButton(
            text="♾  پلن‌های نامحدود", callback_data="no_plans"
        ))
        for plan in unlimited:
            builder.row(InlineKeyboardButton(
                text=_plan_label(plan, rate), callback_data=f"plan:{plan.id}"
            ))

    builder.row(InlineKeyboardButton(text="🔙 بازگشت به منو", callback_data="back_main"))
    return builder.as_markup()


def get_plan_confirm_keyboard(
    plan_id: int,
    discount_code: str = "",
    crypto_on: bool = True,
    card_on: bool = False,
    crypto_invoice: bool = False,
    crypto_gateway: str = "nowpayments",   # "nowpayments" | "maxelpay"
    amount: float = 0.0,
    plan_name: str = "",
) -> InlineKeyboardMarkup:
    """انتخاب روش پرداخت — فقط روش‌های فعال نمایش داده می‌شوند."""
    builder = InlineKeyboardBuilder()
    dc_suffix = f":{discount_code}" if discount_code else ""

    if crypto_on:
        if crypto_gateway == "maxelpay":
            # MaxelPay — hosted checkout
            builder.button(
                text="💜 پرداخت با ارز دیجیتال (MaxelPay)",
                callback_data=f"pay_maxel:{plan_id}:{amount:.2f}:{plan_name}{dc_suffix}",
            )
        elif crypto_invoice:
            # NOWPayments Invoice — انتخاب آزاد ارز
            builder.button(
                text="🌐 پرداخت با ارز دیجیتال (انتخاب ارز)",
                callback_data=f"pay_invoice:{plan_id}{dc_suffix}",
            )
        else:
            # NOWPayments Direct — فقط USDT TRC-20
            builder.button(
                text="🪙 پرداخت با کریپتو (USDT TRC-20)",
                callback_data=f"pay:{plan_id}{dc_suffix}",
            )
    if card_on:
        builder.button(text="💳 پرداخت کارت به کارت",
                       callback_data=f"card_pay:{plan_id}{dc_suffix}")
    if not crypto_on and not card_on:
        builder.button(text="⛔ هیچ روش پرداختی فعال نیست", callback_data="no_plans")

    builder.button(text="🏷 دارم کد تخفیف",    callback_data=f"discount:{plan_id}")
    builder.button(text="🔙 بازگشت به پلن‌ها", callback_data="show_plans")
    builder.adjust(1)
    return builder.as_markup()


def get_confirm_after_discount_keyboard(
    plan_id: int,
    code: str,
    crypto_on: bool = True,
    card_on: bool = False,
    crypto_invoice: bool = False,
    crypto_gateway: str = "nowpayments",
    amount: float = 0.0,
    plan_name: str = "",
) -> InlineKeyboardMarkup:
    """انتخاب روش پرداخت بعد از اعمال کد تخفیف."""
    builder = InlineKeyboardBuilder()
    if crypto_on:
        if crypto_gateway == "maxelpay":
            builder.button(
                text="💜 پرداخت با ارز دیجیتال (MaxelPay)",
                callback_data=f"pay_maxel:{plan_id}:{amount:.2f}:{plan_name}:{code}",
            )
        elif crypto_invoice:
            builder.button(
                text="🌐 پرداخت با ارز دیجیتال (انتخاب ارز)",
                callback_data=f"pay_invoice:{plan_id}:{code}",
            )
        else:
            builder.button(text="🪙 کریپتو (USDT)", callback_data=f"pay:{plan_id}:{code}")
    if card_on:
        builder.button(text="💳 کارت به کارت",  callback_data=f"card_pay:{plan_id}:{code}")
    builder.button(text="❌ انصراف",             callback_data="show_plans")
    builder.adjust(1)
    return builder.as_markup()


def get_subscription_detail_keyboard(sub_email: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔗 دریافت مجدد لینک", callback_data=f"resend_link:{sub_email}")
    builder.button(text="🔙 بازگشت", callback_data="my_subs")
    builder.adjust(1)
    return builder.as_markup()


def get_payment_status_keyboard(order_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 بررسی پرداخت", callback_data=f"check_payment:{order_id}")
    builder.button(text="❌ انصراف", callback_data="back_main")
    builder.adjust(1)
    return builder.as_markup()


# ── سازگاری با handler قدیمی ──────────────────
def get_plan_detail_keyboard(plan_id: int) -> InlineKeyboardMarkup:
    return get_plan_confirm_keyboard(plan_id)


def get_confirm_purchase_keyboard(plan_id: int) -> InlineKeyboardMarkup:
    return get_plan_confirm_keyboard(plan_id)
