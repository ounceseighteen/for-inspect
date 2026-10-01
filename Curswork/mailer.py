from flask import current_app
from flask_mail import Message
from markupsafe import escape
import os
import logging

from app import mail
from db import get_db

logger = logging.getLogger(__name__)

try:
    import qrcode  # для QR-кода в письме с билетом (см. send_paid_tickets_email)
    QR_AVAILABLE = True
except ImportError:
    QR_AVAILABLE = False
    logger.warning('Не установлен пакет qrcode — QR-код в письмах с билетами '
                   'генерироваться не будет. Установите: pip install qrcode')


def send_confirmation_email(email, subject, intro_text, code):
    msg = Message(subject, recipients=[email])
    msg.html = f"""
        <div style="background-color:#0d0f14; padding:40px; font-family:sans-serif; color:#e8eaf0; border-radius:10px;">
            <div style="text-align:center; margin-bottom:20px;">
                <h2 style="color:#e8a020; margin:0;">МИР КИНО</h2>
            </div>
            <div style="background-color:#161a23; padding:30px; border-radius:8px; border:1px solid #2a2f3e;">
                <p style="font-size:16px;">Здравствуйте!</p>
                <p style="font-size:14px; color:#7a8399;">{intro_text}</p>
                <div style="font-size:32px; font-weight:bold; color:#e8a020; text-align:center; letter-spacing:10px; margin:30px 0;">
                    {code}
                </div>
                <p style="font-size:12px; color:#4a5066; text-align:center;">Код действует 5 минут. Если вы не выполняли это действие, просто проигнорируйте письмо.</p>
            </div>
            <div style="text-align:center; margin-top:20px; font-size:12px; color:#4a5066;">
                © 2026 Кинотеатр «Мир Кино»
            </div>
        </div>
    """
    mail.send(msg)


def send_paid_tickets_email(recipient_email, booking_ids):
    """
    Отправляет одно письмо по одному или нескольким ОПЛАЧЕННЫМ билетам.
    Возвращает True, если письмо успешно передано SMTP-серверу.
    """
    from datetime import datetime
    from utils import make_ticket_code

    if not recipient_email or not booking_ids:
        return False

    booking_ids = [int(x) for x in booking_ids if x]
    if not booking_ids:
        return False

    conn = get_db()
    placeholders = ','.join('?' for _ in booking_ids)
    rows = conn.execute(f"""
        SELECT b.id, b.custom_code, b.final_price, b.payment_method,
               u.name as user_name, u.email as user_email,
               f.title, f.id as film_id, f.poster, s.date, s.time, h.name as hall_name,
               seats.row_num, seats.seat_num, p.title as promo_title, p.discount as promo_discount
        FROM bookings b
        JOIN users u ON u.id = b.user_id
        JOIN sessions s ON s.id = b.session_id
        JOIN films f ON f.id = s.film_id
        JOIN halls h ON h.id = s.hall_id
        JOIN seats ON seats.id = b.seat_id
        LEFT JOIN promotions p ON p.id = b.promo_id
        WHERE b.id IN ({placeholders}) AND b.status='paid'
        ORDER BY seats.row_num, seats.seat_num
    """, booking_ids).fetchall()
    conn.close()

    if not rows:
        return False

    first = rows[0]
    poster_filename = first['poster'] or ''
    poster_path = os.path.join(current_app.root_path, 'static', 'posters', poster_filename) if poster_filename else ''
    has_poster = bool(poster_filename and os.path.exists(poster_path))

    try:
        date_text = datetime.strptime(first['date'], '%Y-%m-%d').strftime('%d.%m.%Y')
    except Exception:
        date_text = first['date']

    seats_text = ', '.join([f"ряд {r['row_num']}, место {r['seat_num']}" for r in rows])
    codes_text = '<br>'.join([escape(make_ticket_code(r['title'], r['film_id'], r['id'])) for r in rows])
    total_price = sum(float(r['final_price'] or 0) for r in rows)
    payment_text = 'Бонусная карта' if first['payment_method'] == 'bonus' else 'Банковская карта'

    # ── QR-код билета: код билета + фильм + сеанс. Печатается сервером и
    # встраивается в письмо КАК НАСТОЯЩЕЕ ВЛОЖЕНИЕ (cid:), а не data-URI:
    # почтовые веб-клиенты (Яндекс, Mail.ru, Gmail в вебе и частично мобильные)
    # вырезают data:image/... и вместо QR показывает «битую» картинку или
    # пустой блок. С cid-вложением QR рендерится везде, где рендерится постер.
    qr_html = ''
    qr_png = None
    if QR_AVAILABLE:
        try:
            import io as _io
            qr = qrcode.QRCode(box_size=6, border=2)
            qr.add_data(f"МИР КИНО | {first['title']} | {date_text} {first['time']} | {seats_text} | "
                        f"{','.join(make_ticket_code(r['title'], r['film_id'], r['id']) for r in rows)}")
            qr.make(fit=True)
            img = qr.make_image(fill_color='#11141c', back_color='white')
            buf = _io.BytesIO()
            img.save(buf, format='PNG')
            qr_png = buf.getvalue()
            qr_html = (f'<div style="margin:16px 0 4px;">'
                       f'<img src="cid:ticket_qr" alt="QR-код билета" width="148" height="148" '
                       f'style="display:block; margin-left:auto; margin-right:auto; '
                       f'width:148px; height:148px; border-radius:10px; '
                       f'background:#fff; padding:6px;">'
                       f'<div style="text-align:center; font-size:11px; color:#7a8399; margin-top:8px;">'
                       f'Покажите этот QR-код на входе</div></div>')
        except Exception as _e:
            logger.error('Ошибка генерации QR: %s', _e)

    # Название фильма/зала/акции задаёт админ через форму — экранируем на всякий
    # случай, чтобы в письме нельзя было исполнить произвольный HTML/JS.
    safe_title = escape(first['title'])
    safe_hall_name = escape(first['hall_name'])

    promo_html = ''
    if first['promo_title']:
        promo_html = f"""
            <tr><td style="padding:7px 0; color:#7a8399;">Акция</td><td style="padding:7px 0; color:#e8eaf0; font-weight:700;">{escape(first['promo_title'])} −{int(first['promo_discount'] or 0)}%</td></tr>
        """

    poster_html = """
        <div style="width:320px; height:500px; border-radius:12px; background:#1e2330; display:flex; align-items:center; justify-content:center; color:#e8a020; font-size:44px; font-weight:800;">🎬</div>
    """
    if has_poster:
        poster_html = '<img src="cid:ticket_poster" alt="Афиша" style="width:320px; height:500px; object-fit:cover; border-radius:12px; display:block;">'

    subject = f"Ваш билет в Мир Кино — {first['title']}"  # заголовок письма — plain text, escape не нужен
    if len(rows) > 1:
        subject = f"Ваши билеты в Мир Кино — {first['title']} и ещё {len(rows)-1}"
    msg = Message(subject, recipients=[recipient_email])
    msg.html = f"""
        <div style="margin:0; padding:32px; background:#0d0f14; font-family:Arial, sans-serif; color:#e8eaf0;">
            <div style="max-width:720px; margin:0 auto;">
                <div style="text-align:center; margin-bottom:22px;">
                    <div style="font-size:24px; font-weight:900; color:#e8a020; letter-spacing:1px;">МИР КИНО</div>
                </div>

                <div style="background:#161a23; border:1px solid #2a2f3e; border-radius:16px; padding:22px;">
                    <div style="display:flex; gap:18px; align-items:flex-start;">
                        <div style="flex:0 0 320px;">{poster_html}</div>
                        <div style="flex:1; min-width:0; margin-left:22px;">
                            <h2 style="margin:0 0 14px; color:#e8eaf0; font-size:24px; line-height:1.25;">{safe_title}</h2>
                            <table style="width:100%; border-collapse:collapse; font-size:15px;">
                                <tr><td style="padding:7px 0; color:#7a8399; width:130px;">Дата</td><td style="padding:7px 0; color:#e8eaf0; font-weight:700;">{date_text}</td></tr>
                                <tr><td style="padding:7px 0; color:#7a8399;">Время</td><td style="padding:7px 0; color:#e8eaf0; font-weight:700;">{escape(first['time'])}</td></tr>
                                <tr><td style="padding:7px 0; color:#7a8399;">Зал</td><td style="padding:7px 0; color:#e8eaf0; font-weight:700;">{safe_hall_name}</td></tr>
                                <tr><td style="padding:7px 0; color:#7a8399;">Места</td><td style="padding:7px 0; color:#e8eaf0; font-weight:700;">{seats_text}</td></tr>
                                <tr><td style="padding:7px 0; color:#7a8399;">Оплата</td><td style="padding:7px 0; color:#e8eaf0; font-weight:700;">{payment_text}</td></tr>
                                {promo_html}
                                <tr><td style="padding:7px 0; color:#7a8399;">Сумма</td><td style="padding:7px 0; color:#e8a020; font-weight:900; font-size:18px;">{int(round(total_price))} ₽</td></tr>
                            </table>

                            {qr_html}
                        </div>
                    </div>

                    <div style="margin-top:20px; padding:14px; background:#0d0f14; border:1px solid #2a2f3e; border-radius:12px; color:#e8eaf0; font-size:14px; line-height:1.7;">
                        <div style="color:#7a8399; margin-bottom:6px;">Коды билетов:</div>
                        {codes_text}
                    </div>
                </div>

                <div style="text-align:center; margin-top:18px; color:#4a5066; font-size:12px;">
                    Покажите письмо или QR-код при входе в зал.<br>
                    © 2026 Кинотеатр «Мир Кино»
                </div>
            </div>
        </div>
    """

    if has_poster:
        with open(poster_path, 'rb') as img:
            ext = os.path.splitext(poster_filename)[1].lower().lstrip('.') or 'jpg'
            mimetype = 'image/png' if ext == 'png' else 'image/jpeg'
            msg.attach(
                filename=poster_filename,
                content_type=mimetype,
                data=img.read(),
                disposition='inline',
                headers={'Content-ID': '<ticket_poster>'}
            )

    if qr_png:
        try:
            msg.attach(
                filename='ticket_qr.png',
                content_type='image/png',
                data=qr_png,
                disposition='inline',
                headers={'Content-ID': '<ticket_qr>'}
            )
        except Exception as _e:
            logger.error('Ошибка вложения QR: %s', _e)

    try:
        mail.send(msg)
        return True
    except Exception as e:
        logger.error('Ошибка отправки письма с билетами (получатель: %s): %s', recipient_email, e)
        return False


def send_recovery_email(email, code):
    msg = Message("Код для восстановления пароля - Мир Кино",
                  recipients=[email])
    msg.html = f"""
        <div style="background-color: #0d0f14; padding: 40px; font-family: sans-serif; color: #e8eaf0; border-radius: 10px;">
            <div style="text-align: center; margin-bottom: 20px;">
                <h2 style="color: #e8a020; margin: 0;">МИР КИНО</h2>
            </div>
            <div style="background-color: #161a23; padding: 30px; border-radius: 8px; border: 1px solid #2a2f3e;">
                <p style="font-size: 16px;">Здравствуйте!</p>
                <p style="font-size: 14px; color: #7a8399;">Вы запросили восстановление пароля. Используйте код ниже для подтверждения:</p>
                <div style="font-size: 32px; font-weight: bold; color: #e8a020; text-align: center; letter-spacing: 10px; margin: 30px 0;">
                    {code}
                </div>
                <p style="font-size: 12px; color: #4a5066; text-align: center;">Если вы не запрашивали этот код, просто проигнорируйте письмо.</p>
            </div>
            <div style="text-align: center; margin-top: 20px; font-size: 12px; color: #4a5066;">
                © 2026 Кинотеатр «Мир Кино»
            </div>
        </div>
    """
    mail.send(msg)