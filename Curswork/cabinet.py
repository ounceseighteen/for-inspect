import logging
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify
from datetime import date, datetime
from markupsafe import escape

logger = logging.getLogger(__name__)

import config  # A06-3 аудита: REFUND_HOURS_BEFORE берём из настроек, а не из локальной константы

from db import get_db, log_audit, hash_password, verify_password
from utils import (
    current_user, add_notification, fmt_date_ru, make_ticket_code,
    create_verification_code, check_verification_code, is_valid_email,
    spend_bonuses_for_booking, award_bonus_for_booking, get_loyalty_cashback,
    add_loyalty_history,
)
from security import login_required
from app import limiter, mail
from mailer import send_paid_tickets_email, send_confirmation_email

cabinet_bp = Blueprint('cabinet', __name__)

# A06-3 аудита: правило возврата берётся из config.REFUND_HOURS_BEFORE
# (переменная REFUND_HOURS_BEFORE в .env). Раньше здесь была своя константа 6,
# и изменение настройки не влияло на логику возврата.
REFUND_HOURS_BEFORE = config.REFUND_HOURS_BEFORE

ALL_CABINET_ENDPOINTS = []  # (короткое_имя, view) заполняется внизу файла


@cabinet_bp.route('/cabinet')
@login_required
def cabinet():
    user = current_user()
    if user['role'] in ('admin', 'cashier'):
        return redirect(url_for('personal'))

    conn = get_db()
    now_dt = datetime.now()

    loyalty_card = conn.execute("""
        SELECT * FROM loyalty_cards WHERE user_id=?
    """, (user['id'],)).fetchone()

    raw_bookings = conn.execute("""
        SELECT b.*, f.title, s.date, s.time, h.name as hall_name,
               se.row_num, se.seat_num, s.price as session_price,
               COALESCE(b.final_price, s.price) as display_price,
               p.title as promo_title,
               p.discount as promo_discount
        FROM bookings b
        JOIN sessions s ON b.session_id = s.id
        JOIN films f ON s.film_id = f.id
        JOIN halls h ON s.hall_id = h.id
        JOIN seats se ON b.seat_id = se.id
        LEFT JOIN promotions p ON p.id = b.promo_id
        WHERE b.user_id=? AND b.status != 'cancelled'
        ORDER BY s.date ASC, s.time ASC, b.id ASC
    """, (user['id'],)).fetchall()

    bookings = []
    for b in raw_bookings:
        item = dict(b)
        try:
            session_dt = datetime.strptime(f"{item['date']} {item['time']}", "%Y-%m-%d %H:%M")
        except Exception:
            session_dt = now_dt

        item['is_past'] = session_dt < now_dt
        item['date_text'] = datetime.strptime(item['date'], "%Y-%m-%d").strftime("%d.%m.%Y") if item.get('date') else ''
        item['price'] = item.get('display_price') or item.get('session_price') or 0

        if item['status'] == 'paid' and item['is_past']:
            item['status_label'] = 'Посещён'
            item['status_color'] = 'var(--text-muted)'
            item['can_cancel'] = False
            item['can_pay'] = False
        elif item['status'] == 'paid':
            item['status_label'] = 'Куплен'
            item['status_color'] = 'var(--success)'
            item['can_cancel'] = False
            item['can_pay'] = False
        elif item['status'] == 'booked' and item['is_past']:
            item['status_label'] = 'Просрочен'
            item['status_color'] = 'var(--danger)'
            item['can_cancel'] = False
            item['can_pay'] = False
        else:
            item['status_label'] = 'Забронирован'
            item['status_color'] = 'var(--accent)'
            item['can_cancel'] = True
            item['can_pay'] = True

        if item.get('payment_method') == 'bonus':
            item['payment_text'] = 'Бонусная карта'
        elif item.get('payment_method') == 'bank':
            item['payment_text'] = 'Банковская карта'
        else:
            item['payment_text'] = 'Не оплачен'

        bookings.append(item)

    # Сортировка: сначала предстоящие (раньше — выше), затем прошедшие/посещённые
    # всегда в самом низу (даже ниже предстоящих через неделю).
    bookings.sort(key=lambda x: (1 if x['is_past'] else 0, x.get('date') or '', x.get('time') or ''))

    stats_row = conn.execute("""
        SELECT COUNT(*) as bought_tickets,
               COALESCE(SUM(COALESCE(b.final_price, s.price)), 0) as total_spent
        FROM bookings b
        JOIN sessions s ON s.id = b.session_id
        WHERE b.user_id=? AND b.status='paid'
    """, (user['id'],)).fetchone()

    visited_row = conn.execute("""
        SELECT COUNT(*) as visited_films
        FROM bookings b
        JOIN sessions s ON s.id = b.session_id
        WHERE b.user_id=?
          AND b.status='paid'
          AND (s.date < ? OR (s.date = ? AND s.time < ?))
    """, (user['id'], date.today().isoformat(), date.today().isoformat(), datetime.now().strftime('%H:%M'))).fetchone()

    cabinet_stats = {
        'bought_tickets': stats_row['bought_tickets'] if stats_row else 0,
        'visited_films': visited_row['visited_films'] if visited_row else 0,
        'total_spent': stats_row['total_spent'] if stats_row else 0
    }

    promotions = conn.execute('SELECT * FROM promotions ORDER BY id').fetchall()

    # ── Избранное «Хочу посмотреть» ──
    favorites = conn.execute('''
        SELECT f.*, r.avg_rating,
               (SELECT MIN(s.date || 'T' || s.time) FROM sessions s
                WHERE s.film_id = f.id AND (s.date > ? OR (s.date = ? AND s.time > ?)))
               as next_session
        FROM favorites fv
        JOIN films f ON f.id = fv.film_id
        LEFT JOIN (SELECT film_id, ROUND(AVG(rating), 1) as avg_rating FROM reviews GROUP BY film_id) r
               ON r.film_id = f.id
        WHERE fv.user_id=?
        ORDER BY fv.created_at DESC
    ''', (date.today().isoformat(), date.today().isoformat(), datetime.now().strftime('%H:%M'), user['id'])).fetchall()

    conn.close()
    return render_template('user/cabinet.html',
                           user=user,
                           bookings=bookings,
                           loyalty_card=loyalty_card,
                           cabinet_stats=cabinet_stats,
                           promotions=promotions,
                           favorites=favorites)


# ═══════════════════════════════════════════════════════════════
# Профиль: имя, телефон, email (со сменой почты), пароль
# ═══════════════════════════════════════════════════════════════


@cabinet_bp.route('/update_profile', methods=['POST'])
@login_required
@limiter.limit('10 per minute')
def update_profile():
    """
    Обновление имени и телефона. Email НЕ меняется здесь: смена почты теперь
    требует подтверждения по новой почте (см. send_email_change_code /
    verify_email_change_code), иначе можно было бы занять чужой адрес.
    """
    import re
    user_id = session['user_id']
    new_name = request.form.get('name', '').strip()

    if new_name and len(new_name) > 100:
        return {"status": "error", "message": "Слишком длинное имя"}, 400

    conn = get_db()
    cursor = conn.cursor()

    if new_name:
        cursor.execute("UPDATE users SET name = ? WHERE id = ?", (new_name, user_id))
        session['user_name'] = new_name

    phone = request.form.get('phone', '').strip()
    if phone:
        # Допустимые: +7..., 8..., с пробелами/скобками/дефисами, минимум 10 цифр
        digits = re.sub(r'\D', '', phone)
        if len(digits) < 10 or len(digits) > 12:
            conn.close()
            return {"status": "error", "message": "Некорректный номер телефона"}, 400
        cursor.execute("UPDATE users SET phone = ? WHERE id = ?", (phone, user_id))

    conn.commit()
    conn.close()
    return {"status": "ok", "message": "Данные сохранены"}


@cabinet_bp.route('/send_email_change_code', methods=['POST'])
@login_required
@limiter.limit('5 per minute')
def send_email_change_code():
    """
    Шаг 1 смены email: код подтверждения уходит на УЖЕ ПРИВЯЗАННУЮ почту.
    Если злоумышленник завладел сессией, но не почтой — он не сможет
    подтвердить смену, потому что код придёт владельцу аккаунта.
    """
    user = current_user()
    new_email = request.form.get('new_email', '').strip().lower()

    if not is_valid_email(new_email):
        return {"status": "error", "message": "Некорректный формат почты"}, 400
    if new_email == user['email']:
        return {"status": "error", "message": "Новый email совпадает с текущим"}, 400

    conn = get_db()
    existing = conn.execute('SELECT id FROM users WHERE email=? AND id!=?',
                            (new_email, user['id'])).fetchone()
    if existing:
        conn.close()
        return {"status": "error", "message": "Email уже занят другим пользователем"}, 400

    # Код хранится с привязкой к ТЕКУЩЕЙ почте владельца аккаунта.
    code = create_verification_code(conn, 'email_change', user['email'])
    conn.close()

    session['pending_email_change'] = new_email

    try:
        send_confirmation_email(
            user['email'],
            "Подтверждение смены email - Мир Кино",
            "Вы (или кто-то, кто имеет доступ к аккаунту) запросили смену "
            "адреса электронной почты. Если это были вы — введите код для "
            "подтверждения, иначе проигнорируйте это письмо:",
            code
        )
        return {"status": "success", "message": "Код отправлен на вашу текущую почту"}
    except Exception as e:
        logger.error('Ошибка SMTP при смене email: %s', e)
        session.pop('pending_email_change', None)
        return {"status": "error", "message": "Ошибка при отправке письма. Проверьте настройки почты."}, 500


@cabinet_bp.route('/verify_email_change_code', methods=['POST'])
@login_required
@limiter.limit('10 per minute')
def verify_email_change_code():
    """Шаг 2 смены email: проверяет код, отправленный на ТЕКУЩУЮ почту."""
    user = current_user()
    new_email = session.get('pending_email_change')

    if not new_email:
        return {"status": "error", "message": "Сначала запросите код на ваш текущий адрес"}, 400

    input_code = request.form.get('code', '').strip()
    conn = get_db()

    # Код привязан к текущей почте владельца (user['email']), поэтому ввести
    # код может только тот, у кого есть доступ к текущему ящику.
    ok, message, _ = check_verification_code(conn, 'email_change', user['email'], input_code)
    if not ok:
        conn.close()
        return {"status": "error", "message": message}, 400

    existing = conn.execute('SELECT id FROM users WHERE email=? AND id!=?',
                            (new_email, user['id'])).fetchone()
    if existing:
        conn.execute('DELETE FROM verification_codes WHERE purpose=? AND email=?',
                     ('email_change', user['email']))
        conn.commit()
        conn.close()
        session.pop('pending_email_change', None)
        return {"status": "error", "message": "Email уже занят другим пользователем"}, 400

    conn.execute('UPDATE users SET email=? WHERE id=?', (new_email, user['id']))
    conn.commit()
    conn.close()

    session.pop('pending_email_change', None)

    # Уведомление на старый адрес о том, что контактная почта изменилась
    old_email = user['email']
    if old_email:
        try:
            notify = mail.Message("Смена email в «Мир Кино»", recipients=[old_email])
            notify.html = f"""
                <div style="background-color:#0d0f14; padding:40px; font-family:sans-serif; color:#e8eaf0; border-radius:10px;">
                    <div style="text-align:center; margin-bottom:20px;">
                        <h2 style="color:#e8a020; margin:0;">МИР КИНО</h2>
                    </div>
                    <div style="background-color:#161a23; padding:30px; border-radius:8px; border:1px solid #2a2f3e;">
                        <p style="font-size:16px;">Здравствуйте!</p>
                        <p style="font-size:14px; color:#7a8399;">
                            Контактный email вашего аккаунта изменён на <b>{escape(new_email)}</b>.
                            Если вы этого не делали — срочно смените пароль и напишите администратору.
                        </p>
                    </div>
                    <div style="text-align:center; margin-top:20px; font-size:12px; color:#4a5066;">
                        © 2026 Кинотеатр «Мир Кино»
                    </div>
                </div>
            """
            mail.send(notify)
        except Exception as e:
            logger.error('Ошибка SMTP при уведомлении о смене email: %s', e)

    return {"status": "success", "message": "Email обновлён", "new_email": new_email}


@cabinet_bp.route('/update_password', methods=['POST'])
@login_required
@limiter.limit('5 per minute')
def update_password():
    user_id = session['user_id']
    old_password = request.form.get('old_password', '')
    new_password = request.form.get('new_password', '')
    confirm_password = request.form.get('confirm_password', '')

    if not old_password or not new_password or not confirm_password:
        return {"status": "error", "message": "Заполните все поля"}, 400

    if new_password != confirm_password:
        return {"status": "error", "message": "Пароли не совпадают"}, 400

    if len(new_password) < 8:
        return {"status": "error", "message": "Минимум 8 символов"}, 400

    conn = get_db()
    user = conn.execute("SELECT password FROM users WHERE id = ?", (user_id,)).fetchone()

    if not verify_password(user['password'], old_password):
        conn.close()
        return {"status": "error", "message": "Неверный текущий пароль"}, 400

    conn.execute("UPDATE users SET password=?, password_changed_at=? WHERE id=?",
                 (hash_password(new_password), datetime.now().isoformat(), user_id))
    conn.commit()
    conn.close()

    # Завершаем текущую сессию тоже — пользователь войдёт заново с новым паролем.
    # Это защищает от ситуации, когда пароль меняют именно потому, что старая
    # сессия могла быть скомпрометирована.
    session.clear()
    return {"status": "ok", "message": "Пароль изменён. Войдите заново.", "logout": True}


# ═══════════════════════════════════════════════════════════════
# Бронирования и возвраты
# ═══════════════════════════════════════════════════════════════


@cabinet_bp.route('/cancel_booking/<int:booking_id>', methods=['POST'])
@login_required
def cancel_booking(booking_id):
    user = current_user()
    conn = get_db()

    # Кассир может отменять брони ТОЛЬКО свои (не чужие брони).
    # A01-4 аудита: раньше админ мог отменить ЛЮБУЮ бронь по id без
    # уведомления владельца — «тихая» отмена чужого билета. Теперь админ
    # тоже ограничен своими бронями. Для сервисной отмены чужих броней
    # есть отдельный admin-эндпоинт (при необходимости — добавить) или
    # прямое вмешательство в БД с записью в audit_log.
    if user['role'] == 'admin':
        booking = conn.execute(
            'SELECT * FROM bookings WHERE id=? AND user_id=?',
            (booking_id, user['id'])
        ).fetchone()
    elif user['role'] == 'cashier':
        # Кассир может отменять только свои брони (как клиент) или брони по своим сеансам
        booking = conn.execute(
            'SELECT * FROM bookings WHERE id=? AND user_id=?',
            (booking_id, user['id'])
        ).fetchone()
    else:
        # Клиент — только своё
        booking = conn.execute(
            'SELECT * FROM bookings WHERE id=? AND user_id=?',
            (booking_id, user['id'])
        ).fetchone()

    # Отменять можно только бронь в статусе "booked" (не оплаченную и не
    # отменённую повторно): раньше клиент мог отменить уже купленный билет
    # без какого-либо возврата денег/бонусов, что было и багом, и дырой.
    if booking and booking['status'] == 'booked':
        conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (booking_id,))
        conn.commit()
        log_audit(conn, user, 'booking_cancelled', f'Отмена брони №{booking_id}')
    elif booking:
        flash('Оплаченную бронь нельзя отменить самостоятельно — обратитесь к администратору.', 'warning')
    else:
        flash('Бронирование не найдено или у вас нет прав на его отмену.', 'danger')

    conn.close()

    # Редирект обратно туда откуда пришли
    if user['role'] in ('admin', 'cashier'):
        return redirect(url_for('personal') + '?tab=bookings')
    return redirect(url_for('cabinet'))


def refund_booking(conn, booking, user):
    """
    Возврат ОПЛАЧЕННОГО билета клиентом.
    Возврат возможен только до установленного рубежа до начала сеанса.
    Бонусы, потраченные на оплату, возвращаются на карту (с отменой кешбэка,
    чтобы нельзя было накрутить кешбэк возвратами).
    Возвращает (ok, message).
    """
    try:
        session_dt = datetime.strptime(f"{booking['date']} {booking['time']}", "%Y-%m-%d %H:%M")
    except Exception:
        session_dt = datetime.now()

    left_minutes = (session_dt - datetime.now()).total_seconds() / 60
    if left_minutes <= 0:
        return False, 'Сеанс уже прошёл — возврат невозможен.'
    if left_minutes < REFUND_HOURS_BEFORE * 60:
        h = int(left_minutes // 60)
        m = int(left_minutes % 60)
        return False, f'Возврат возможен не позднее чем за {REFUND_HOURS_BEFORE} ч до сеанса. Осталось {h} ч {m} мин.'

    conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (booking['id'],))

    # Возврат потраченных бонусов на карту клиента.
    # sqlite3.Row не имеет метода .get() — обращаемся по индексу и явно
    # проверяем, что колонка вообще есть в выборке (на случай старых SELECT'ов).
    keys = booking.keys() if hasattr(booking, 'keys') else []
    payment_method = booking['payment_method'] if 'payment_method' in keys else None
    final_price_raw = booking['final_price'] if 'final_price' in keys else None
    if payment_method == 'bonus' and int(final_price_raw or 0) > 0:
        card = conn.execute('SELECT * FROM loyalty_cards WHERE user_id=?', (user['id'],)).fetchone()
        if card:
            amount = int(round(booking['final_price']))
            new_balance = int(card['bonus_balance'] or 0) + amount
            conn.execute('UPDATE loyalty_cards SET bonus_balance=? WHERE user_id=?', (new_balance, user['id']))
            add_loyalty_history(conn, user['id'], amount, 'Возврат билета', new_balance, booking['id'])

            # Отменяем кешбэк, начисленный за этот билет (если был)
            conn.execute("""
                DELETE FROM bonus_history
                WHERE user_id=? AND booking_id=? AND operation='Кешбэк за билет'
            """, (user['id'], booking['id']))
            conn.execute("""
                UPDATE loyalty_cards
                SET bonus_balance = MAX(0, bonus_balance - ?)
                WHERE user_id=?
            """, (int(round(booking['final_price'] * get_loyalty_cashback(card['level']) / 100)), user['id']))

            conn.execute("""
                            UPDATE loyalty_cards
                            SET total_spent = MAX(0, total_spent - ?)
                            WHERE user_id=?
                        """, (float(final_price_raw or 0), user['id']))

    conn.commit()
    return True, 'Билет возвращён.'


@cabinet_bp.route('/refund/<int:booking_id>', methods=['POST'])
@login_required
def refund_ticket(booking_id):
    """Возврат оплаченного билета клиентом (см. refund_booking)."""
    user = current_user()
    if user['role'] != 'client':
        flash('Возврат билетов доступен только клиентам.', 'warning')
        return redirect(url_for('personal'))

    conn = get_db()
    booking = conn.execute("""
        SELECT b.*, s.date, s.time, f.title
        FROM bookings b
        JOIN sessions s ON s.id = b.session_id
        JOIN films f ON f.id = s.film_id
        WHERE b.id=? AND b.user_id=?
    """, (booking_id, user['id'])).fetchone()

    if not booking:
        conn.close()
        flash('Билет не найден.', 'danger')
        return redirect(url_for('cabinet'))

    if booking['status'] != 'paid':
        conn.close()
        flash('Вернуть можно только оплаченный билет.', 'warning')
        return redirect(url_for('cabinet'))

    ok, msg = refund_booking(conn, booking, user)

    if not ok:
        conn.close()
        flash(msg, 'danger')
    else:
        log_audit(conn, user, 'ticket_refund',
                  f'Возврат билета №{booking_id} на «{booking["title"]}»')
        add_notification(
            conn, user['id'], 'refund',
            f'Билет на «{booking["title"]}» возвращён',
            'Мы вернули бонусы (если оплачивали бонусами). Будем рады видеть вас снова!',
            '/cabinet'
        )
        conn.close()
        flash(msg, 'success')
    return redirect(url_for('cabinet'))


@cabinet_bp.route('/client/pay_booking/<int:booking_id>', methods=['POST'])
@login_required
def client_pay_booking(booking_id):
    user = current_user()
    payment_method = request.form.get('payment_method', '').strip()
    promo_id = request.form.get('promo_id', type=int)

    if payment_method not in ('bank', 'bonus'):
        return jsonify({'error': 'Выберите способ оплаты'}), 400

    conn = get_db()
    booking = conn.execute("""
        SELECT b.*, s.price as session_price, s.date, s.time, f.title
        FROM bookings b
        JOIN sessions s ON s.id = b.session_id
        JOIN films f ON f.id = s.film_id
        WHERE b.id=? AND b.user_id=?
    """, (booking_id, user['id'])).fetchone()

    if not booking:
        conn.close()
        return jsonify({'error': 'Бронирование не найдено'}), 404

    if booking['status'] != 'booked':
        conn.close()
        return jsonify({'error': 'Этот билет уже нельзя оплатить'}), 400

    now_date = date.today().isoformat()
    now_time = datetime.now().strftime('%H:%M')
    if booking['date'] < now_date or (booking['date'] == now_date and booking['time'] < now_time):
        conn.close()
        return jsonify({'error': 'Сеанс уже прошёл'}), 400

    base_price = float(booking['session_price'] or booking['final_price'] or 0)
    discount = 0
    if promo_id:
        promo = conn.execute('SELECT discount FROM promotions WHERE id=?', (promo_id,)).fetchone()
        if promo:
            discount = max(0, min(100, int(promo['discount'] or 0)))
        else:
            promo_id = None

    final_price = round(base_price * (1 - discount / 100), 2)

    if payment_method == 'bonus':
        card = conn.execute('SELECT * FROM loyalty_cards WHERE user_id=?', (user['id'],)).fetchone()
        if not card:
            conn.close()
            return jsonify({'error': 'У вас нет бонусной карты'}), 400
        if int(card['bonus_balance'] or 0) < int(round(final_price)):
            conn.close()
            return jsonify({'error': 'Недостаточно бонусов на карте'}), 400

    conn.execute("""
        UPDATE bookings
        SET status='paid', payment_method=?, promo_id=?, final_price=?
        WHERE id=?
    """, (payment_method, promo_id, final_price, booking_id))

    if payment_method == 'bonus':
        ok, err = spend_bonuses_for_booking(conn, user['id'], booking_id, final_price)
        if not ok:
            conn.close()
            return jsonify({'error': err}), 400
        award_bonus_for_booking(conn, booking_id)

    conn.commit()

    # Уведомление об оплате
    film_title = booking['title'] if booking['title'] else 'Билет'
    add_notification(
        conn, user['id'], 'payment',
        f'Билет на «{film_title}» оплачен',
        f'{fmt_date_ru(booking["date"])} в {booking["time"]} будет сеанс «{film_title}». '
        f'Электронный билет можете увидеть у себя на почте или в личном кабинете',
        '/cabinet'
    )
    conn.close()
    # Честное сообщение: если SMTP недоступен, письмо не ушло, но билет
    # оплачен и доступен в кабинете (A10-4 аудита — раньше всегда обещали
    # «отправлены на вашу почту»).
    email_sent = send_paid_tickets_email(user['email'], [booking_id])
    return jsonify({
        'ok': True,
        'message': ('Билеты оплачены и отправлены на вашу почту' if email_sent
                    else 'Билеты оплачены. Письмо не удалось отправить — билет доступен в личном кабинете'),
        'email_sent': email_sent
    })


@cabinet_bp.route('/api/ticket/<int:booking_id>')
@login_required
def api_ticket(booking_id):
    """
    Данные одного билета/брони для модального окна в личном кабинете.
    Для оплаченного билета возвращает QR-код картинкой (base64).
    """
    user = current_user()
    conn = get_db()
    # A01-3 аудита: ВСЕ пользователи (включая админов/кассиров) видят только
    # свои билеты. Раньше для staff был фильтр «бронь на сеанс в зале, где у
    # кассира когда-либо была своя бронь» — он пропускал чужие билеты
    # (фильм, зал, ряд, место, код, QR) и позволял подделывать коды.
    row = conn.execute("""
        SELECT b.id, b.session_id, b.seat_id, b.status, b.custom_code, b.final_price, b.payment_method,
               b.promo_id, f.title, f.id as film_id, f.poster,
               s.date, s.time, h.name as hall_name,
               se.row_num, se.seat_num, s.price as session_price,
               p.title as promo_title, p.discount as promo_discount
        FROM bookings b
        JOIN sessions s ON b.session_id = s.id
        JOIN films f ON s.film_id = f.id
        JOIN halls h ON s.hall_id = h.id
        JOIN seats se ON b.seat_id = se.id
        LEFT JOIN promotions p ON b.promo_id = p.id
        WHERE b.id=? AND b.user_id=?
    """, (booking_id, user['id'])).fetchone()

    if not row:
        conn.close()
        return jsonify({'error': 'Билет не найден'}), 404

    status = row['status']
    now_date = date.today().isoformat()
    now_time = datetime.now().strftime('%H:%M')
    is_past = (row['date'] < now_date) or (row['date'] == now_date and row['time'] < now_time)

    result = {
        'id': row['id'],
        'session_id': row['session_id'],
        'seat_id': row['seat_id'],
        'status': status,
        'title': row['title'],
        'film_id': row['film_id'],
        'poster': row['poster'] or '',
        'date_text': fmt_date_ru(row['date']),
        'time': row['time'],
        'hall_name': row['hall_name'],
        'row_num': row['row_num'],
        'seat_num': row['seat_num'],
        'code': row['custom_code'] or make_ticket_code(row['title'], row['film_id'], row['id']),
        'price': int(round(float(row['final_price'] if row['final_price'] is not None else row['session_price'] or 0))),
        'payment_method': row['payment_method'] or '',
        'promo_title': row['promo_title'],
        'promo_discount': row['promo_discount'],
        'is_past': is_past,
        'can_pay': status == 'booked' and not is_past,
        'can_cancel': status == 'booked' and not is_past,
        'can_return': (status == 'paid' and not is_past),
    }

    # QR только для оплаченных предстоящих билетов — как в письме
    try:
        import qrcode as _qrcode
        QR_AVAILABLE = True
    except ImportError:
        QR_AVAILABLE = False
    if status in ('paid',) and not is_past and QR_AVAILABLE:
        try:
            import io as _io
            import base64 as _b64
            seats_text = f"ряд {row['row_num']}, место {row['seat_num']}"
            qr = _qrcode.QRCode(box_size=8, border=2)
            qr.add_data(f"МИР КИНО | {row['title']} | {fmt_date_ru(row['date'])} {row['time']} | "
                        f"{seats_text} | {result['code']}")
            qr.make(fit=True)
            img = qr.make_image(fill_color='#11141c', back_color='white')
            buf = _io.BytesIO()
            img.save(buf, format='PNG')
            result['qr'] = _b64.b64encode(buf.getvalue()).decode('ascii')
        except Exception as e:
            logger.error('Ошибка генерации QR билета: %s', e)

    conn.close()
    return jsonify(result)


# короткие endpoint'ы для url_for в шаблонах (см. register_blueprints в main.py)
ALL_CABINET_ENDPOINTS = [
    ('cabinet', cabinet),
    ('update_profile', update_profile),
    ('send_email_change_code', send_email_change_code),
    ('verify_email_change_code', verify_email_change_code),
    ('update_password', update_password),
    ('cancel_booking', cancel_booking),
    ('refund_ticket', refund_ticket),
    ('client_pay_booking', client_pay_booking),
    ('api_ticket', api_ticket),
]