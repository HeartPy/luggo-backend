"""
領収書PDFの生成

reportlab に同梱される日本語CIDフォント（HeiseiKakuGo-W5）を使用するため、
外部フォントファイルの同梱は不要。
"""
import io
from django.utils import timezone
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from .emails import _business_signature
from .models import LuggageBooking


_FONT_NAME = 'HeiseiKakuGo-W5'

# 領収書の但し書き
_RECEIPT_NOTE = '荷物配送料として'
# 発行事業者名を取得できない場合のフォールバック
_DEFAULT_ISSUER = 'LugGo'


def _ensure_font() -> None:
    """日本語フォントを登録（登録済みなら何もしない）"""
    try:
        pdfmetrics.getFont(_FONT_NAME)
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont(_FONT_NAME))


def build_receipt_pdf(booking: LuggageBooking) -> bytes:
    """予約から領収書PDFのバイト列を生成"""
    _ensure_font()

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    page_width, page_height = A4

    issued_on = timezone.localtime(timezone.now())
    issued_label = f'{issued_on.year}年{issued_on.month}月{issued_on.day}日'
    amount_label = f'¥{booking.total_amount:,}-'

    # 発行事業者の名称・住所（住所は Stripe Connect アカウントから補完される）
    signature = _business_signature(booking.business_owner)
    issuer = (signature.get('business_name') or '').strip() or _DEFAULT_ISSUER
    issuer_address_lines = (signature.get('business_address') or '').splitlines()

    # 発行日・領収書番号（右上）
    right_x = page_width - 25 * mm
    pdf.setFont(_FONT_NAME, 10)
    pdf.drawRightString(right_x, page_height - 25 * mm, f'発行日: {issued_label}')
    pdf.drawRightString(
        right_x, page_height - 31 * mm, f'領収書番号: {booking.id}'
    )

    # タイトル（番号ブロックの下に十分な間隔をあけて配置）
    pdf.setFont(_FONT_NAME, 28)
    pdf.drawCentredString(page_width / 2, page_height - 50 * mm, '領収書')

    # 金額（中央・下線付き）
    amount_y = page_height - 80 * mm
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

    # 発行事業者（右下）
    line_height = 6 * mm
    # 住所の行数に応じて開始位置を上げ、注記が用紙下端に収まるようにする
    issuer_y = 45 * mm + line_height * len(issuer_address_lines)

    pdf.setFont(_FONT_NAME, 12)
    pdf.drawRightString(right_x, issuer_y, issuer)

    current_y = issuer_y
    pdf.setFont(_FONT_NAME, 10)
    for line in issuer_address_lines:
        current_y -= line_height
        pdf.drawRightString(right_x, current_y, line)

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
