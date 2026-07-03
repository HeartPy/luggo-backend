"""
領収書PDFの生成

reportlab に同梱される日本語CIDフォント（HeiseiKakuGo-W5）を使用するため、
外部フォントファイルの同梱は不要。
"""
import io
import logging
from django.utils import timezone
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from .emails import build_issuer_snapshot
from .models import LuggageBooking


logger = logging.getLogger(__name__)

_FONT_NAME = 'HeiseiKakuGo-W5'

# 領収書の但し書き
_RECEIPT_NOTE = '荷物配送料として'
# 発行事業者名を取得できない場合のフォールバック
_DEFAULT_ISSUER = 'LugGo'
# 消費税率（荷物配送は軽減税率対象外のため標準税率10%）
_TAX_RATE_PERCENT = 10


def _tax_breakdown(total_amount: int) -> tuple[int, int]:
    """
    税込金額から税抜金額と消費税額を算出

    total_amount は税込（10%）の整数円。税抜金額は端数切り捨てで算出し、
    消費税額は税込との差額とすることで、税抜＋税額＝税込を必ず満たす。
    """
    total = int(total_amount or 0)
    net = total * 100 // (100 + _TAX_RATE_PERCENT)
    tax = total - net
    return net, tax


def _ensure_font() -> None:
    """日本語フォントを登録（登録済みなら何もしない）"""
    try:
        pdfmetrics.getFont(_FONT_NAME)
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont(_FONT_NAME))


def _resolve_issuer(booking: LuggageBooking) -> tuple[str, str, str]:
    """
    領収書の発行者名・住所・適格請求書登録番号を決定する

    決済確定時に保存した発行者スナップショットを優先する。スナップショットが
    無い（この機能導入前の予約など）場合のみ、Stripe から取得を試みる。
    それでも取得できない場合はフォールバック名を用いる。
    """
    issuer = (booking.issuer_name or '').strip()
    issuer_address = (booking.issuer_address or '').strip()
    invoice_number = (booking.issuer_invoice_number or '').strip()

    if not issuer or not issuer_address:
        # スナップショット未保存の予約向けフォールバック
        logger.info(
            "領収書: 発行者スナップショットが不足しているためStripeから補完します: booking_id=%s",
            booking.id,
        )
        try:
            snapshot = build_issuer_snapshot(booking.business_owner)
        except Exception:
            logger.exception(
                "領収書: 発行者情報の補完に失敗: booking_id=%s", booking.id
            )
            snapshot = {}
        issuer = issuer or (snapshot.get('issuer_name') or '').strip()
        issuer_address = issuer_address or (snapshot.get('issuer_address') or '').strip()
        invoice_number = invoice_number or (snapshot.get('issuer_invoice_number') or '').strip()

    return issuer or _DEFAULT_ISSUER, issuer_address, invoice_number


def build_receipt_pdf(booking: LuggageBooking) -> bytes:
    """予約から領収書PDFのバイト列を生成"""
    _ensure_font()

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    page_width, page_height = A4

    issued_on = timezone.localtime(timezone.now())
    issued_label = f'{issued_on.year}年{issued_on.month}月{issued_on.day}日'
    amount_label = f'¥{booking.total_amount:,}-'

    # 取引日（決済日）は予約作成日時＝決済成立時点を用いる
    paid_on = timezone.localtime(booking.created_at) if booking.created_at else None
    paid_label = (
        f'{paid_on.year}年{paid_on.month}月{paid_on.day}日' if paid_on else '―'
    )

    issuer, issuer_address, invoice_number = _resolve_issuer(booking)
    issuer_address_lines = issuer_address.splitlines()

    # 発行日・取引日・領収書番号（右上）
    right_x = page_width - 25 * mm
    pdf.setFont(_FONT_NAME, 10)
    pdf.drawRightString(right_x, page_height - 25 * mm, f'発行日: {issued_label}')
    pdf.drawRightString(right_x, page_height - 31 * mm, f'取引日: {paid_label}')
    pdf.drawRightString(
        right_x, page_height - 37 * mm, f'領収書番号: {booking.id}'
    )

    # タイトル（番号ブロックの下に十分な間隔をあけて配置）
    pdf.setFont(_FONT_NAME, 28)
    pdf.drawCentredString(page_width / 2, page_height - 54 * mm, '領収書')

    # 宛名（左・下線付き）
    addressee = (booking.customer_name or '').strip()
    if addressee:
        name_y = page_height - 68 * mm
        pdf.setFont(_FONT_NAME, 14)
        pdf.drawString(40 * mm, name_y, f'{addressee} 様')
        text_width = pdf.stringWidth(f'{addressee} 様', _FONT_NAME, 14)
        pdf.setLineWidth(0.5)
        pdf.line(40 * mm, name_y - 2 * mm, 40 * mm + text_width, name_y - 2 * mm)

    # 金額（中央・下線付き）
    amount_y = page_height - 90 * mm
    pdf.setFont(_FONT_NAME, 26)
    pdf.drawCentredString(page_width / 2, amount_y, f'{amount_label}（税込）')
    pdf.setLineWidth(1)
    pdf.line(40 * mm, amount_y - 6 * mm, page_width - 40 * mm, amount_y - 6 * mm)

    # 但し書き
    pdf.setFont(_FONT_NAME, 12)
    pdf.drawString(40 * mm, amount_y - 22 * mm, f'但 {_RECEIPT_NOTE}')
    pdf.drawString(
        40 * mm, amount_y - 32 * mm, '上記正に領収いたしました。'
    )

    # 税率・消費税額の区分表示は、適格請求書発行事業者（登録番号あり）の領収書のみ表示
    if invoice_number:
        net_amount, tax_amount = _tax_breakdown(booking.total_amount)
        left_x = 40 * mm
        value_x = 120 * mm
        breakdown_y = amount_y - 46 * mm
        pdf.setFont(_FONT_NAME, 11)
        pdf.drawString(left_x, breakdown_y, '税率・消費税額の内訳')
        pdf.setFont(_FONT_NAME, 10)
        breakdown_rows = [
            (f'{_TAX_RATE_PERCENT}%対象（税抜）', f'¥{net_amount:,}'),
            (f'消費税（{_TAX_RATE_PERCENT}%）', f'¥{tax_amount:,}'),
            ('税込合計', f'¥{booking.total_amount:,}'),
        ]
        row_y = breakdown_y
        for label, value in breakdown_rows:
            row_y -= 7 * mm
            pdf.drawString(left_x + 4 * mm, row_y, label)
            pdf.drawRightString(value_x, row_y, value)

    # 発行事業者（右下）
    line_height = 6 * mm
    # 発行者名の下に並ぶ行数（住所＋登録番号）に応じて開始位置を上げ、
    # 注記が用紙下端に収まるようにする
    lower_line_count = len(issuer_address_lines) + (1 if invoice_number else 0)
    issuer_y = 45 * mm + line_height * lower_line_count

    pdf.setFont(_FONT_NAME, 12)
    pdf.drawRightString(right_x, issuer_y, issuer)

    current_y = issuer_y
    pdf.setFont(_FONT_NAME, 10)
    for line in issuer_address_lines:
        current_y -= line_height
        pdf.drawRightString(right_x, current_y, line)

    if invoice_number:
        current_y -= line_height
        pdf.drawRightString(right_x, current_y, f'登録番号: {invoice_number}')

    pdf.setFont(_FONT_NAME, 9)
    pdf.drawRightString(
        right_x, current_y - 8 * mm, '本領収書はシステムにより発行されています。'
    )

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def receipt_filename(booking: LuggageBooking) -> str:
    """ダウンロード時のファイル名（一意キー＝予約IDを使用）"""
    return f'receipt-{booking.id}.pdf'
