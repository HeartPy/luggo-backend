"""
領収書PDFの生成

表記言語は旅行者の表示言語（ja / en / zh-Hans / zh-Hant）に対応する。
"""
import io
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from django.utils import timezone
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from .emails import build_issuer_snapshot
from .models import LuggageBooking


logger = logging.getLogger(__name__)

_FONT_DIR = Path(__file__).resolve().parent / 'fonts'

_FONT_FILES: dict[str, str] = {
    'NotoSansJP': 'NotoSansJP.ttf',
    'NotoSansSC': 'NotoSansSC.ttf',
    'NotoSansTC': 'NotoSansTC.ttf',
}

_FONT_JA = 'NotoSansJP'

# 表示言語ごとのフォント
_FONTS_BY_LANG: dict[str, str] = {
    'ja': _FONT_JA,
    'en': _FONT_JA,
    'zh-Hans': 'NotoSansSC',
    'zh-Hant': 'NotoSansTC',
}

# 発行事業者名を取得できない場合のフォールバック
_DEFAULT_ISSUER = 'LugGo'
# 消費税率（荷物配送は軽減税率対象外のため標準税率10%）
_TAX_RATE_PERCENT = 10

# 表示言語ごとの領収書ラベル（{rate} は消費税率）
_TEXTS: dict[str, dict[str, str]] = {
    'ja': {
        'title': '領収書',
        'issue_date': '発行日',
        'paid_date': '取引日',
        'receipt_no': '領収書番号',
        'addressee_suffix': ' 様',
        'tax_included': '（税込）',
        'note': '但 荷物配送料として',
        'received': '上記正に領収いたしました。',
        'breakdown_title': '税率・消費税額の内訳',
        'net_label': '{rate}%対象（税抜）',
        'tax_label': '消費税（{rate}%）',
        'total_label': '税込合計',
        'registration_no': '登録番号',
        'system_note': '本領収書はシステムにより発行されています。',
    },
    'en': {
        'title': 'RECEIPT',
        'issue_date': 'Issue date',
        'paid_date': 'Transaction date',
        'receipt_no': 'Receipt No.',
        'addressee_suffix': '',
        'tax_included': ' (tax included)',
        'note': 'For luggage delivery service',
        'received': 'We hereby acknowledge receipt of the above amount.',
        'breakdown_title': 'Tax breakdown',
        'net_label': 'Subject to {rate}% tax (excl. tax)',
        'tax_label': 'Consumption tax ({rate}%)',
        'total_label': 'Total (tax included)',
        'registration_no': 'Registration No.',
        'system_note': 'This receipt was issued automatically by the system.',
    },
    'zh-Hans': {
        'title': '收据',
        'issue_date': '开具日期',
        'paid_date': '交易日期',
        'receipt_no': '收据编号',
        'addressee_suffix': '',
        'tax_included': '（含税）',
        'note': '款项内容：行李配送服务费',
        'received': '兹确认已如数收讫上述款项。',
        'breakdown_title': '税率与消费税明细',
        'net_label': '适用{rate}%税率（不含税）',
        'tax_label': '消费税（{rate}%）',
        'total_label': '含税合计',
        'registration_no': '登记编号',
        'system_note': '本收据由系统自动开具。',
    },
    'zh-Hant': {
        'title': '收據',
        'issue_date': '開立日期',
        'paid_date': '交易日期',
        'receipt_no': '收據編號',
        'addressee_suffix': '',
        'tax_included': '（含稅）',
        'note': '款項內容：行李配送服務費',
        'received': '茲確認已如數收訖上述款項。',
        'breakdown_title': '稅率與消費稅明細',
        'net_label': '適用{rate}%稅率（未稅）',
        'tax_label': '消費稅（{rate}%）',
        'total_label': '含稅合計',
        'registration_no': '登錄編號',
        'system_note': '本收據由系統自動開立。',
    },
}

_EN_MONTH_NAMES = (
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
)


def _resolve_receipt_lang(booking: LuggageBooking, lang: Optional[str]) -> str:
    """表記言語を決定（指定が無効なら予約時の言語→日本語の順でフォールバック）"""
    if lang in _TEXTS:
        return lang
    stored = getattr(booking, 'customer_language', '') or ''
    if stored in _TEXTS:
        return stored
    return 'ja'


def _format_receipt_date(value: Optional[datetime], lang: str) -> str:
    """日付を表記言語に応じて整形"""
    if not value:
        return '―'
    if lang == 'en':
        return f'{_EN_MONTH_NAMES[value.month - 1]} {value.day}, {value.year}'
    return f'{value.year}年{value.month}月{value.day}日'


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


def _ensure_font(font_name: str) -> None:
    """TTFフォントを登録（登録済みなら何もしない）"""
    try:
        pdfmetrics.getFont(font_name)
    except KeyError:
        path = _FONT_DIR / _FONT_FILES[font_name]
        pdfmetrics.registerFont(TTFont(font_name, str(path)))


def _format_yen(amount: int) -> str:
    return f'¥{amount:,}'


def _resolve_issuer(booking: LuggageBooking, lang: str) -> tuple[str, str, str]:
    """
    領収書の発行者名・住所・適格請求書登録番号を決定する

    決済確定時に保存した発行者スナップショットを優先する。日本語以外では英語表記を
    優先し、無ければ日本語名へフォールバックする。
    必要なスナップショットが無い場合のみ Stripe から取得を試みる。
    """
    issuer_ja = (booking.issuer_name or '').strip()
    issuer_en = (booking.issuer_name_en or '').strip()
    issuer_address = (booking.issuer_address or '').strip()
    invoice_number = (booking.issuer_invoice_number or '').strip()

    needs_name = not issuer_ja or (lang != 'ja' and not issuer_en)
    if needs_name or not issuer_address:
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
        issuer_ja = issuer_ja or (snapshot.get('issuer_name') or '').strip()
        issuer_en = issuer_en or (snapshot.get('issuer_name_en') or '').strip()
        issuer_address = issuer_address or (snapshot.get('issuer_address') or '').strip()
        invoice_number = invoice_number or (snapshot.get('issuer_invoice_number') or '').strip()

    issuer = issuer_ja if lang == 'ja' else (issuer_en or issuer_ja)
    return issuer or _DEFAULT_ISSUER, issuer_address, invoice_number


def build_receipt_pdf(booking: LuggageBooking, lang: Optional[str] = None) -> bytes:
    """
    予約から領収書PDFのバイト列を生成

    `lang` 未指定時は予約時の表示言語（customer_language）で表記する。
    """
    lang = _resolve_receipt_lang(booking, lang)
    font = _FONTS_BY_LANG[lang]
    texts = _TEXTS[lang]

    _ensure_font(font)
    _ensure_font(_FONT_JA)

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    page_width, page_height = A4

    issued_on = timezone.localtime(timezone.now())
    issued_label = _format_receipt_date(issued_on, lang)
    amount_label = f'{_format_yen(booking.total_amount)}-'

    # 取引日（決済日）は予約作成日時＝決済成立時点を用いる
    paid_on = timezone.localtime(booking.created_at) if booking.created_at else None
    paid_label = _format_receipt_date(paid_on, lang)

    issuer, issuer_address, invoice_number = _resolve_issuer(booking, lang)
    issuer_address_lines = issuer_address.splitlines()

    # 発行日・取引日・領収書番号（右上）
    right_x = page_width - 25 * mm
    pdf.setFont(font, 10)
    pdf.drawRightString(
        right_x, page_height - 25 * mm, f'{texts["issue_date"]}: {issued_label}'
    )
    pdf.drawRightString(
        right_x, page_height - 31 * mm, f'{texts["paid_date"]}: {paid_label}'
    )
    pdf.drawRightString(
        right_x, page_height - 37 * mm, f'{texts["receipt_no"]}: {booking.id}'
    )

    # タイトル（番号ブロックの下に十分な間隔をあけて配置）
    pdf.setFont(font, 28)
    pdf.drawCentredString(page_width / 2, page_height - 54 * mm, texts['title'])

    # 宛名（左・下線付き）
    addressee = (booking.customer_name or '').strip()
    if addressee:
        addressee_label = f'{addressee}{texts["addressee_suffix"]}'
        name_y = page_height - 68 * mm
        pdf.setFont(font, 14)
        pdf.drawString(40 * mm, name_y, addressee_label)
        text_width = pdf.stringWidth(addressee_label, font, 14)
        pdf.setLineWidth(0.5)
        pdf.line(40 * mm, name_y - 2 * mm, 40 * mm + text_width, name_y - 2 * mm)

    # 金額（中央・下線付き）
    amount_y = page_height - 90 * mm
    pdf.setFont(font, 26)
    pdf.drawCentredString(
        page_width / 2, amount_y, f'{amount_label}{texts["tax_included"]}'
    )
    pdf.setLineWidth(1)
    pdf.line(40 * mm, amount_y - 6 * mm, page_width - 40 * mm, amount_y - 6 * mm)

    # 但し書き
    pdf.setFont(font, 12)
    pdf.drawString(40 * mm, amount_y - 22 * mm, texts['note'])
    pdf.drawString(40 * mm, amount_y - 32 * mm, texts['received'])

    # 税率・消費税額の区分表示は、適格請求書発行事業者（登録番号あり）の領収書のみ表示
    if invoice_number:
        net_amount, tax_amount = _tax_breakdown(booking.total_amount)
        left_x = 40 * mm
        value_x = 120 * mm
        breakdown_y = amount_y - 46 * mm
        pdf.setFont(font, 11)
        pdf.drawString(left_x, breakdown_y, texts['breakdown_title'])
        pdf.setFont(font, 10)
        breakdown_rows = [
            (
                texts['net_label'].format(rate=_TAX_RATE_PERCENT),
                _format_yen(net_amount),
            ),
            (
                texts['tax_label'].format(rate=_TAX_RATE_PERCENT),
                _format_yen(tax_amount),
            ),
            (texts['total_label'], _format_yen(booking.total_amount)),
        ]
        row_y = breakdown_y
        for label, value in breakdown_rows:
            row_y -= 7 * mm
            pdf.drawString(left_x + 4 * mm, row_y, label)
            pdf.drawRightString(value_x, row_y, value)

    # 発行事業者（右下）。住所は日本語データのため日本語フォントで描画
    line_height = 6 * mm
    # 発行者名の下に並ぶ行数（住所＋登録番号）に応じて開始位置を上げ、
    # 注記が用紙下端に収まるようにする
    lower_line_count = len(issuer_address_lines) + (1 if invoice_number else 0)
    issuer_y = 45 * mm + line_height * lower_line_count

    pdf.setFont(_FONT_JA, 12)
    pdf.drawRightString(right_x, issuer_y, issuer)

    current_y = issuer_y
    pdf.setFont(_FONT_JA, 10)
    for line in issuer_address_lines:
        current_y -= line_height
        pdf.drawRightString(right_x, current_y, line)

    if invoice_number:
        current_y -= line_height
        pdf.setFont(font, 10)
        pdf.drawRightString(
            right_x, current_y, f'{texts["registration_no"]}: {invoice_number}'
        )

    pdf.setFont(font, 9)
    pdf.drawRightString(right_x, current_y - 8 * mm, texts['system_note'])

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def receipt_filename(booking: LuggageBooking) -> str:
    """ダウンロード時のファイル名（一意キー＝予約IDを使用）"""
    return f'receipt-{booking.id}.pdf'
