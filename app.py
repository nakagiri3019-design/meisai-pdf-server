"""
入出金明細PDF生成サーバー（Flask + ReportLab）
住信SBIデモのブラウザから明細データを受け取り、
テンプレートPDFに明朝フォントで上書きしてPDFを返す。

エンドポイント:
  GET  /health        … 死活監視
  POST /generate      … 明細PDF生成（JSON受け取り → PDF返す）
"""
import io
import math
from datetime import date, datetime

from flask import Flask, request, send_file, jsonify

from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdfcanvas

app = Flask(__name__)


# ---- CORS（ブラウザの別オリジンからのアクセスを許可） ----
@app.after_request
def add_cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return resp


@app.route('/generate', methods=['OPTIONS'])
def generate_preflight():
    return ('', 204)

# ---- フォント登録（明朝・TrueType変換済みサブセット） ----
pdfmetrics.registerFont(TTFont('M', 'mincho.ttf'))

TEMPLATE_PATH = 'template.pdf'
PAGE_W, PAGE_H = 595, 842

# ---- テンプレート実測レイアウト定数 ----
VLINE_X   = [89.28, 290.34, 384.12, 478.26]
TBL_LEFT  = 15.12
TBL_RIGHT = 575.28
DATE_CX   = (TBL_LEFT + VLINE_X[0]) / 2
DESC_X0   = VLINE_X[0] + 1.5
OUT_X1    = VLINE_X[2] - 1.5
INN_X1    = VLINE_X[3] - 1.5
BAL_X1    = TBL_RIGHT  - 1.5
FIRST_ROW_TOP = 265.64
ROW_H = 11.7
FS_DATE, FS_DESC, FS_NUM = 9.5, 9.3, 8.3

# テンプレートの明細枠は 11.7pt 間隔で 46 行ぶん引いてある。
# 先頭の1行は「日付／内容／お支払金額／預入金額／残高」の見出し行なので、
# 1ページに描ける明細行は残り 45 行。これを超えると紙面外に消える。
ROWS_PER_PAGE = 45

# テンプレートには「1 / 1」がページ番号として印字済み。
# 複数ページのときだけ白で塗りつぶして正しい番号を描き直す。
PAGENO_CX     = 293.3
PAGENO_BASE_Y = 9.71
PAGENO_FS     = 9.0
PAGENO_BOX    = (282.0, 6.5, 306.0, 19.0)   # x0, y0, x1, y1


def pdf_y(py):
    return PAGE_H - py


def clean_amt(amt):
    """'+180,000' / '-180,000' → '180,000'。入金/出金の符号を除去"""
    if amt is None:
        return '0'
    s = str(amt).lstrip('+-').strip()
    return s if s else '0'

def normalize_date(s):
    """ '2026年6月16日' → '2026年06月16日' （月日をゼロ埋め） """
    import re
    if not s:
        return s
    m = re.match(r'(\d{4})年(\d{1,2})月(\d{1,2})日', s)
    if m:
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        return f'{y}年{mo:02d}月{d:02d}日'
    return s



def build_pdf(payload):
    """
    payload 例:
    {
      "created": "2026-06-19",        # 作成日（省略時は当日）
      "period_start": "2026-06-01",   # 対象期間 開始
      "period_end": "2026-06-19",     # 対象期間 終了
      "rows": [
        {"date":"2026年06月16日","desc":"ＡＴＭ手数料","out":"110","in":"0","bal":"6,324"},
        ...
      ]
    }
    """
    # 日付類
    def parse(d, default):
        if not d:
            return default
        try:
            return datetime.strptime(d, '%Y-%m-%d').date()
        except Exception:
            return default

    today = date.today()
    created      = parse(payload.get('created'), today)
    period_start = parse(payload.get('period_start'), today)
    period_end   = parse(payload.get('period_end'), today)
    rows = payload.get('rows', [])

    def jdate(d):
        # 本物（4月）の表記に合わせる:
        #   年の後・月の後にスペース、月日が1桁なら数字前にスペースで桁揃え
        #   例: 2026 年  4 月  1 日 / 2026 年  4 月 16 日
        mo = f'{d.month:>2}'   # 1桁なら前にスペース（ ' 4' / '12'）
        dd = f'{d.day:>2}'     # 1桁なら前にスペース（ ' 1' / '16'）
        return f'{d.year} 年 {mo} 月 {dd} 日'

    # 「以下余白」も枠を1行分使うので、行数+1 でページ数を決める
    total_pages = max(1, math.ceil((len(rows) + 1) / ROWS_PER_PAGE))

    # ---- オーバーレイ生成（全ページ分） ----
    packet = io.BytesIO()
    c = pdfcanvas.Canvas(packet, pagesize=(PAGE_W, PAGE_H))
    period_str = f'{jdate(period_start)} ～ {jdate(period_end)}'

    for pno in range(total_pages):
        chunk = rows[pno * ROWS_PER_PAGE:(pno + 1) * ROWS_PER_PAGE]
        c.setFillColor(colors.black)

        # ヘッダー（作成日・対象期間） ※ラベルはテンプレ印字済み、後ろに続ける
        # 背景テンプレを全ページに敷くので、各ページの先頭に出す
        c.setFont('M', 9.0)
        c.drawString(463.0, pdf_y(60.5) + 1.5, jdate(created))
        c.drawString(75.0, pdf_y(200.7) + 1.5, period_str)

        # 明細行
        for i, row in enumerate(chunk):
            ty = pdf_y(FIRST_ROW_TOP + (i + 1) * ROW_H) + 2.6
            c.setFont('M', FS_DATE)
            c.drawCentredString(DATE_CX, ty, normalize_date(row.get('date', '')))
            c.setFont('M', FS_DESC)
            c.drawString(DESC_X0, ty, row.get('desc', ''))
            c.setFont('M', FS_NUM)
            c.drawRightString(OUT_X1, ty, clean_amt(row.get('out', '0')))
            c.drawRightString(INN_X1, ty, clean_amt(row.get('in', '0')))
            c.drawRightString(BAL_X1, ty, row.get('bal', ''))

        # 以下余白（最終ページのみ）
        if pno == total_pages - 1:
            ty = pdf_y(FIRST_ROW_TOP + (len(chunk) + 1) * ROW_H) + 2.6
            c.setFont('M', FS_DESC)
            c.drawRightString(VLINE_X[1] - 1.5, ty, '以下余白')

        # ページ番号（複数ページのときだけ書き換える）
        if total_pages > 1:
            x0, y0, x1, y1 = PAGENO_BOX
            c.setFillColor(colors.white)
            c.rect(x0, y0, x1 - x0, y1 - y0, stroke=0, fill=1)
            c.setFillColor(colors.black)
            c.setFont('M', PAGENO_FS)
            c.drawCentredString(PAGENO_CX, PAGENO_BASE_Y, f'{pno + 1} / {total_pages}')

        c.showPage()

    c.save()
    packet.seek(0)

    # ---- テンプレートに合成（ページ毎に背景を敷く） ----
    overlay = PdfReader(packet)
    with open(TEMPLATE_PATH, 'rb') as f:
        tpl_bytes = f.read()

    writer = PdfWriter()
    for pno in range(total_pages):
        # merge_page は元ページを書き換えるので、ページ毎に読み直す
        page = PdfReader(io.BytesIO(tpl_bytes)).pages[0]
        page.merge_page(overlay.pages[pno], over=True)
        writer.add_page(page)

    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return out


@app.route('/health')
def health():
    return jsonify(status='ok')


@app.route('/generate', methods=['POST'])
def generate():
    try:
        payload = request.get_json(force=True)
        pdf_io = build_pdf(payload)
        fname = f"meisai_{date.today().strftime('%Y%m')}.pdf"
        return send_file(pdf_io, mimetype='application/pdf',
                         as_attachment=True, download_name=fname)
    except Exception as e:
        app.logger.exception('PDF generation failed')
        return jsonify(error=str(e)), 500


if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 8400))
    app.run(host='0.0.0.0', port=port)
