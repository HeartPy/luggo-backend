"""事業者の銀行口座への実際の入金を基準に、請求書と支払明細を作成・送信"""

import io
import logging
import re
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import stripe
from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from bookings.models import LuggageBooking
from project.email import email_signature, operations_recipients, send_email
from .models import BusinessProfile, PayoutDocumentDelivery
from .utils import _render_email, owner_email_addressee_context


logger = logging.getLogger(__name__)
stripe.api_key = settings.STRIPE_SECRET_KEY

_FONT_NAME = 'NotoSansJP'
_FONT_PATH = (
    Path(__file__).resolve().parent.parent / 'bookings' / 'fonts' / 'NotoSansJP.ttf'
)
_DEFAULT_SEAL_PATH = (
    Path(__file__).resolve().parent / 'assets' / 'platform_seal.png'
)
_SEAL_DISPLAY_SIZE = 22 * mm
_TAX_RATE = 10
_PROCESSING_TIMEOUT = timedelta(hours=1)
_PAYOUT_DATE_SHIFT_DAYS = 7


def _get(value: Any, key: str, default: Any = None) -> Any:
    if value is None:
        return default
    if hasattr(value, 'get'):
        return value.get(key, default)
    return getattr(value, key, default)


def _iter_api_list(result: Any) -> Iterable[Any]:
    """Stripe の一覧結果を1件ずつ回せる形にする"""
    iterator = getattr(result, 'auto_paging_iter', None)
    if callable(iterator):
        return iterator()
    return _get(result, 'data', []) or []


def _stripe_datetime(timestamp: Any) -> datetime:
    """Stripe の Unix 秒を UTC の datetime に変換"""
    return datetime.fromtimestamp(int(timestamp), tz=dt_timezone.utc)


def _stripe_local_date(timestamp: Any) -> date:
    """Stripe の Unix 秒を日本時間の日付に変換"""
    return timezone.localtime(_stripe_datetime(timestamp)).date()


def previous_month_payout_date(reference_date: Optional[date] = None) -> date:
    """基準日の前月25日を返す。毎月1日の定期実行で利用。"""
    current = reference_date or timezone.localdate()
    first_this_month = current.replace(day=1)
    last_previous_month = first_this_month - timedelta(days=1)
    return last_previous_month.replace(day=25)


def _target_created_range(target_date: date) -> tuple[int, int]:
    """基準日から休日ずれを含むPayout作成日時の範囲を返す"""
    start = timezone.make_aware(datetime.combine(target_date, time.min))
    end = start + timedelta(days=_PAYOUT_DATE_SHIFT_DAYS + 1)
    return int(start.timestamp()), int(end.timestamp())


def list_paid_payouts(
    profile: BusinessProfile,
    target_date: date,
) -> list[Any]:
    """基準日から7日後までに作成された、支払い済みの自動入金（Payout）を返す"""
    start, end = _target_created_range(target_date)
    last_target_date = target_date + timedelta(days=_PAYOUT_DATE_SHIFT_DAYS)
    result = stripe.Payout.list(
        stripe_account=profile.stripe_account_id,
        created={'gte': start, 'lt': end},
        status='paid',
        limit=100,
    )
    payouts: list[Any] = []
    for payout in _iter_api_list(result):
        created_on = _stripe_local_date(_get(payout, 'created'))
        is_in_range = target_date <= created_on <= last_target_date
        is_automatic = _get(payout, 'automatic', True)
        if is_in_range and is_automatic:
            payouts.append(payout)
    return payouts


def _balance_transactions(profile: BusinessProfile, payout_id: str) -> list[Any]:
    """入金（Payout）の内訳となる残高取引をStripeから取得"""
    result = stripe.BalanceTransaction.list(
        stripe_account=profile.stripe_account_id,
        payout=payout_id,
        limit=100,
    )
    return list(_iter_api_list(result))


def _source_id(transaction: Any) -> str:
    source = _get(transaction, 'source')
    if isinstance(source, str):
        return source
    return str(_get(source, 'id', '') or '')


def _valid_registration_number() -> str:
    number = str(
        getattr(settings, 'PLATFORM_INVOICE_REGISTRATION_NUMBER', '') or ''
    ).strip()
    if number and not re.fullmatch(r'T\d{13}', number):
        logger.warning(
            '運営の適格請求書発行事業者登録番号が不正なため番号なしで発行します'
        )
        return ''
    return number


def _arrival_date(payout: Any) -> Optional[date]:
    """Payoutのarrival_date（Unix秒）を日本時間の日付に変換"""
    value = _get(payout, 'arrival_date')
    if value in (None, ''):
        return None
    try:
        return _stripe_local_date(value)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _line_item(booking: LuggageBooking, transfer_amount: int) -> dict[str, Any]:
    """支払明細の売上・手数料・振込額などを組み立て"""
    if booking.transferred_gross_amount is not None:
        gross = int(booking.transferred_gross_amount)
    else:
        total = int(booking.total_amount or 0)
        gross = total - min(total, int(booking.refunded_amount or 0))

    if booking.transferred_platform_fee is not None:
        fee = int(booking.transferred_platform_fee)
    else:
        fee = max(gross - transfer_amount, 0)

    delivered_on = (
        timezone.localtime(booking.delivered_at).date().isoformat()
        if booking.delivered_at
        else ''
    )
    return {
        'booking_id': str(booking.id),
        'booking_number': booking.booking_number,
        'stripe_transfer_id': booking.stripe_transfer_id,
        'delivered_on': delivered_on,
        'gross_sales': gross,
        'platform_fee': fee,
        'transfer_amount': transfer_amount,
    }


def _notify_payout_adjustment(
    record: PayoutDocumentDelivery,
) -> bool:
    """Payoutと予約Transferの照合差額を運営へ通知"""
    if record.adjustment_amount == 0:
        return False

    recipients = operations_recipients()
    if not recipients:
        logger.error(
            'OPERATIONS_NOTIFICATION_EMAIL が未設定のため'
            'Payout差額アラートを送信できません: payout_id=%s',
            record.stripe_payout_id,
        )
        return False

    subject = (
        f'【要確認】入金額と予約明細の合計が一致しません'
        f'（{record.stripe_payout_id}）'
    )
    text = (
        'LugGo 運営の皆さまへ\n'
        '\n'
        '事業者口座への入金額と、予約に紐づく振込合計に差額があります。\n'
        '通常は一致する想定のため、Stripe の残高取引と予約データを確認してください。\n'
        '\n'
        '----------------------------------------\n'
        f'事業者名           : {record.business_profile.company_name}\n'
        f'事業者ID           : {record.business_profile_id}\n'
        f'StripeアカウントID : {record.business_profile.stripe_account_id}\n'
        f'Stripe Payout ID   : {record.stripe_payout_id}\n'
        '----------------------------------------\n'
        f'口座への入金額     : ¥{record.payout_amount:,}\n'
        f'予約に紐づく振込合計 : ¥{record.matched_transfer_amount:,}\n'
        f'差額               : ¥{record.adjustment_amount:,}\n'
        f'紐づけできた予約数 : {len(record.line_items)}件\n'
        '----------------------------------------\n'
        '\n'
        'このメールは支払明細の作成時に、入金額と予約明細の合計が一致しなかった'
        '場合に自動送信されています。\n'
    )
    return send_email(subject=subject, text=text, to=recipients)


def create_delivery_record(
    profile: BusinessProfile,
    payout: Any,
) -> PayoutDocumentDelivery:
    """Payoutの残高取引を予約Transferへ照合し、送付内容をスナップショット"""
    payout_id = str(_get(payout, 'id', '') or '')
    existing = PayoutDocumentDelivery.objects.filter(
        stripe_payout_id=payout_id
    ).first()
    if existing:
        return existing

    transactions = _balance_transactions(profile, payout_id)
    transfer_amounts: dict[str, int] = {}
    for balance_transaction in transactions:
        source_id = _source_id(balance_transaction)
        if not source_id.startswith('tr_'):
            continue
        transfer_amounts[source_id] = (
            transfer_amounts.get(source_id, 0)
            + int(_get(balance_transaction, 'net', 0) or 0)
        )

    bookings = {
        booking.stripe_transfer_id: booking
        for booking in LuggageBooking.objects.filter(
            business_owner=profile,
            stripe_transfer_id__in=transfer_amounts.keys(),
        )
    }
    line_items = [
        _line_item(bookings[transfer_id], amount)
        for transfer_id, amount in transfer_amounts.items()
        if transfer_id in bookings
    ]
    line_items.sort(
        key=lambda item: (item['delivered_on'], item['booking_number'])
    )

    payout_amount = int(_get(payout, 'amount', 0) or 0)
    matched_transfer_amount = sum(
        int(item['transfer_amount']) for item in line_items
    )
    gross_sales = sum(int(item['gross_sales']) for item in line_items)
    platform_fee_amount = sum(int(item['platform_fee']) for item in line_items)

    record = PayoutDocumentDelivery.objects.create(
        business_profile=profile,
        stripe_payout_id=payout_id,
        payout_created_at=_stripe_datetime(_get(payout, 'created')),
        arrival_date=_arrival_date(payout),
        payout_amount=payout_amount,
        currency=str(_get(payout, 'currency', 'jpy') or 'jpy').lower(),
        gross_sales=gross_sales,
        platform_fee=platform_fee_amount,
        matched_transfer_amount=matched_transfer_amount,
        adjustment_amount=payout_amount - matched_transfer_amount,
        line_items=line_items,
        issuer_name=str(
            getattr(
                settings,
                'PLATFORM_INVOICE_ISSUER_NAME',
                'LugGo（ラグゴー）運営事務局',
            )
            or 'LugGo（ラグゴー）運営事務局'
        ).strip(),
        issuer_address=str(
            getattr(settings, 'PLATFORM_INVOICE_ISSUER_ADDRESS', '') or ''
        ).strip(),
        issuer_registration_number=_valid_registration_number(),
        recipient_email=profile.company_email,
    )
    _notify_payout_adjustment(record)
    return record


def _ensure_font() -> None:
    """TTFフォントを登録（登録済みなら何もしない）"""
    try:
        pdfmetrics.getFont(_FONT_NAME)
    except KeyError:
        pdfmetrics.registerFont(TTFont(_FONT_NAME, str(_FONT_PATH)))


def _yen(amount: int) -> str:
    sign = '-' if amount < 0 else ''
    return f'{sign}¥{abs(amount):,}'


def _date_label(value: Optional[date]) -> str:
    if not value:
        return '―'
    return f'{value.year}年{value.month}月{value.day}日'


def _settlement_date(record: PayoutDocumentDelivery) -> date:
    """入金日（なければPayout作成日）を返す"""
    if record.arrival_date is not None:
        return record.arrival_date
    return timezone.localtime(record.payout_created_at).date()


def _tax_breakdown(tax_inclusive_amount: int) -> tuple[int, int]:
    """税込金額を税抜と消費税に分ける"""
    subtotal = tax_inclusive_amount * 100 // (100 + _TAX_RATE)
    return subtotal, tax_inclusive_amount - subtotal


def _platform_seal_path() -> Optional[Path]:
    """電子印鑑画像のパスを返す"""
    configured = str(
        getattr(settings, 'PLATFORM_INVOICE_SEAL_PATH', '') or ''
    ).strip()
    path = Path(configured) if configured else _DEFAULT_SEAL_PATH
    if not path.is_file():
        return None
    return path


def _draw_platform_seal(
    pdf: canvas.Canvas,
    *,
    right: float,
    seal_bottom_y: float,
) -> None:
    """発行者テキストの直上に電子印鑑を描画"""
    seal_path = _platform_seal_path()
    if seal_path is None:
        return
    try:
        seal_x = right - _SEAL_DISPLAY_SIZE
        seal_y = seal_bottom_y
        pdf.drawImage(
            str(seal_path),
            seal_x,
            seal_y,
            width=_SEAL_DISPLAY_SIZE,
            height=_SEAL_DISPLAY_SIZE,
            mask='auto',
            preserveAspectRatio=True,
        )
    except Exception:
        logger.exception('電子印鑑の描画に失敗しました: path=%s', seal_path)


def build_fee_invoice_pdf(record: PayoutDocumentDelivery) -> bytes:
    """プラットフォーム手数料の請求書PDFを生成"""
    _ensure_font()
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    right = width - 25 * mm
    issue_date = timezone.localdate()
    settlement_date = _settlement_date(record)
    has_registration = bool(record.issuer_registration_number)
    has_seal = _platform_seal_path() is not None

    pdf.setFont(_FONT_NAME, 10)
    pdf.drawRightString(right, height - 22 * mm, f'発行日: {_date_label(issue_date)}')
    pdf.drawRightString(
        right, height - 29 * mm, f'請求書番号: {record.stripe_payout_id}'
    )
    pdf.setFont(_FONT_NAME, 25)
    pdf.drawCentredString(
        width / 2,
        height - 48 * mm,
        '適格請求書' if has_registration else '請求書',
    )

    pdf.setFont(_FONT_NAME, 14)
    pdf.drawString(
        25 * mm,
        height - 68 * mm,
        f'{record.business_profile.company_name} 御中',
    )
    pdf.setFont(_FONT_NAME, 11)
    pdf.drawString(
        25 * mm,
        height - 82 * mm,
        f'{_date_label(settlement_date)}支払分のプラットフォーム手数料',
    )
    pdf.setFont(_FONT_NAME, 22)
    pdf.drawString(25 * mm, height - 98 * mm, f'請求額（税込） {_yen(record.platform_fee)}')
    pdf.line(25 * mm, height - 102 * mm, width - 25 * mm, height - 102 * mm)

    subtotal, tax_amount = _tax_breakdown(record.platform_fee)
    rows = [
        ('プラットフォーム手数料（10%対象・税抜）', subtotal),
        (f'消費税（{_TAX_RATE}%）', tax_amount),
        ('税込合計', record.platform_fee),
    ]
    y = height - 122 * mm
    pdf.setFont(_FONT_NAME, 10)
    for label, amount in rows:
        pdf.drawString(30 * mm, y, label)
        pdf.drawRightString(130 * mm, y, _yen(amount))
        y -= 8 * mm

    # 印鑑の下に余白を空けて発行者テキストを配置する
    seal_gap = 10 * mm
    issuer_y = 55 * mm
    if has_seal:
        seal_bottom_y = issuer_y + seal_gap
        _draw_platform_seal(pdf, right=right, seal_bottom_y=seal_bottom_y)

    pdf.setFont(_FONT_NAME, 12)
    pdf.drawRightString(right, issuer_y, record.issuer_name)
    pdf.setFont(_FONT_NAME, 9)
    for address_line in record.issuer_address.splitlines():
        issuer_y -= 6 * mm
        pdf.drawRightString(right, issuer_y, address_line)
    if has_registration:
        issuer_y -= 6 * mm
        pdf.drawRightString(
            right,
            issuer_y,
            f'登録番号: {record.issuer_registration_number}',
        )
    pdf.setFont(_FONT_NAME, 8)
    pdf.drawRightString(
        right,
        25 * mm,
        '本書はシステムにより発行されています。',
    )
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _statement_header(
    pdf: canvas.Canvas,
    record: PayoutDocumentDelivery,
    page_number: int,
) -> float:
    width, height = A4
    right = width - 20 * mm
    pdf.setFont(_FONT_NAME, 9)
    pdf.drawRightString(right, height - 17 * mm, f'ページ {page_number}')
    pdf.setFont(_FONT_NAME, 22)
    pdf.drawCentredString(width / 2, height - 30 * mm, '支払明細')
    pdf.setFont(_FONT_NAME, 11)
    pdf.drawString(20 * mm, height - 44 * mm, f'{record.business_profile.company_name} 御中')
    pdf.setFont(_FONT_NAME, 9)
    pdf.drawString(20 * mm, height - 54 * mm, f'Payout ID: {record.stripe_payout_id}')
    pdf.drawString(
        20 * mm,
        height - 61 * mm,
        f'入金日: {_date_label(record.arrival_date)}',
    )
    return height - 76 * mm


def build_payment_statement_pdf(record: PayoutDocumentDelivery) -> bytes:
    """口座への入金額と、その内訳となる予約ごとの支払明細PDFを生成"""
    _ensure_font()
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    page = 1
    y = _statement_header(pdf, record, page)
    columns = (20 * mm, 55 * mm, 115 * mm, 145 * mm, 180 * mm)

    def draw_columns_header(current_y: float) -> float:
        pdf.setFont(_FONT_NAME, 8)
        labels = ('配達完了日', '予約番号', '売上', '手数料', '振込対象')
        for x, label in zip(columns, labels):
            if label in ('売上', '手数料', '振込対象'):
                pdf.drawRightString(x, current_y, label)
            else:
                pdf.drawString(x, current_y, label)
        pdf.line(20 * mm, current_y - 2 * mm, 190 * mm, current_y - 2 * mm)
        return current_y - 8 * mm

    y = draw_columns_header(y)
    pdf.setFont(_FONT_NAME, 8)
    for item in record.line_items:
        if y < 45 * mm:
            pdf.showPage()
            page += 1
            y = draw_columns_header(_statement_header(pdf, record, page))
            pdf.setFont(_FONT_NAME, 8)
        pdf.drawString(columns[0], y, item.get('delivered_on') or '―')
        pdf.drawString(columns[1], y, str(item.get('booking_number') or ''))
        pdf.drawRightString(columns[2], y, _yen(int(item.get('gross_sales', 0))))
        pdf.drawRightString(columns[3], y, _yen(int(item.get('platform_fee', 0))))
        pdf.drawRightString(columns[4], y, _yen(int(item.get('transfer_amount', 0))))
        y -= 7 * mm

    if y < 75 * mm:
        pdf.showPage()
        page += 1
        y = _statement_header(pdf, record, page)

    y -= 3 * mm
    pdf.line(90 * mm, y, 190 * mm, y)
    summary = [
        ('予約売上合計', record.gross_sales),
        ('プラットフォーム手数料', -record.platform_fee),
        ('お振込み金額', record.payout_amount),
    ]
    pdf.setFont(_FONT_NAME, 9)
    for label, amount in summary:
        y -= 7 * mm
        pdf.drawString(100 * mm, y, label)
        pdf.drawRightString(185 * mm, y, _yen(amount))

    pdf.setFont(_FONT_NAME, 8)
    pdf.drawString(
        20 * mm,
        20 * mm,
        'お振込み金額は、実際にご登録口座へ振り込まれた金額を記載しています。',
    )
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _filename_date(record: PayoutDocumentDelivery) -> str:
    """ファイル名用の日付"""
    return f'{_settlement_date(record):%Y%m%d}'


def _filename_company_name(profile: BusinessProfile) -> str:
    """ファイル名に使えるよう事業者名を整形"""
    name = re.sub(r'[\\/:*?"<>|\s]+', '_', (profile.company_name or '').strip())
    name = re.sub(r'_+', '_', name).strip('._')
    return (name or '事業者')[:40]


def payout_document_filenames(
    record: PayoutDocumentDelivery,
) -> tuple[str, str]:
    """請求書・支払明細の添付ファイル名を返す"""
    company = _filename_company_name(record.business_profile)
    date_part = _filename_date(record)
    payout_id = record.stripe_payout_id
    return (
        f'請求書_{company}_{date_part}_{payout_id}.pdf',
        f'支払明細_{company}_{date_part}_{payout_id}.pdf',
    )


def send_delivery_record(record: PayoutDocumentDelivery) -> bool:
    """同じ書類を二重送信しないよう処理中マークを付けてから、請求書と支払明細をメール送信"""
    if record.sent_at:
        return False

    now = timezone.now()
    stale_before = now - _PROCESSING_TIMEOUT
    claimed = PayoutDocumentDelivery.objects.filter(
        pk=record.pk,
        sent_at__isnull=True,
    ).filter(
        Q(processing_started_at__isnull=True)
        | Q(processing_started_at__lt=stale_before)
    ).update(
        processing_started_at=now,
        send_attempts=record.send_attempts + 1,
        last_err='',
    )
    if not claimed:
        return False

    record.refresh_from_db()
    settlement_date = _settlement_date(record)
    invoice = build_fee_invoice_pdf(record)
    statement = build_payment_statement_pdf(record)
    invoice_filename, statement_filename = payout_document_filenames(record)
    profile = record.business_profile
    context = {
        'payout_date': f'{settlement_date:%Y年%m月%d日}',
        'payout_amount': _yen(record.payout_amount),
        'signature': email_signature(),
    }
    context.update(owner_email_addressee_context(profile))
    subject, body = _render_email('monthly_payout_documents', context)
    try:
        sent = send_email(
            subject=subject,
            text=body,
            to=record.recipient_email,
            attachments=[
                (invoice_filename, invoice, 'application/pdf'),
                (statement_filename, statement, 'application/pdf'),
            ],
        )
    except Exception as e:
        logger.exception(
            '請求書・支払明細のメール送信中に例外が発生しました payout_id=%s',
            record.stripe_payout_id,
        )
        sent = False
        err = str(e)
    else:
        err = '' if sent else 'メール送信処理が失敗を返しました'

    record.processing_started_at = None
    if sent:
        record.sent_at = timezone.now()
        record.last_err = ''
        update_fields = ['processing_started_at', 'sent_at', 'last_err', 'updated_at']
    else:
        record.last_err = err
        update_fields = ['processing_started_at', 'last_err', 'updated_at']
    record.save(update_fields=update_fields)
    return sent


def process_profile_payouts(
    profile: BusinessProfile,
    target_date: date,
    *,
    send: bool = True,
) -> tuple[int, int]:
    """対象事業者の入金（Payout）を記録し、未送信なら請求書・支払明細をメール送信"""
    payouts = list_paid_payouts(profile, target_date)
    sent_count = 0
    for payout in payouts:
        record = create_delivery_record(profile, payout)
        if send and send_delivery_record(record):
            sent_count += 1
    return len(payouts), sent_count
