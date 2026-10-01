from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify
from datetime import date, datetime, timedelta
import sqlite3

from db import get_db, log_audit
from utils import (
    current_user, make_ticket_code, fmt_date_ru, spend_bonuses_for_booking,
    award_bonus_for_booking, add_notification,
)
from security import roles_required, login_required, client_required
from mailer import send_paid_tickets_email

staff_bp = Blueprint('staff', __name__)

ALL_STAFF_ENDPOINTS = []  # (короткое_имя, view) заполняется внизу файла


@staff_bp.route('/personal')
def personal():
    if 'user_id' not in session:
        return redirect(url_for('index'))

    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()

    if not user or user['role'] == 'client':
        conn.close()
        return redirect(url_for('index'))

    data = {}
    data['today'] = date.today().isoformat()
    data['now_time'] = datetime.now().strftime('%H:%M')

    if user['role'] in ('admin', 'cashier'):
        data['films'] = conn.execute('SELECT * FROM films ORDER BY id').fetchall()
        data['halls'] = conn.execute('SELECT * FROM halls ORDER BY id').fetchall()
        data['promotions'] = conn.execute('SELECT * FROM promotions ORDER BY id').fetchall()

    if user['role'] == 'admin':
        data['users'] = conn.execute('''
            SELECT u.id, u.name, u.email, u.phone, u.role, u.is_banned,
                   lc.card_number as loyalty_card_number,
                   lc.level as loyalty_level,
                   lc.bonus_balance as loyalty_bonus_balance,
                   lc.total_spent as loyalty_total_spent
            FROM users u
            LEFT JOIN loyalty_cards lc ON lc.user_id = u.id
            ORDER BY u.id
        ''').fetchall()
        data['stats'] = {
            'films': conn.execute('SELECT COUNT(*) as c FROM films').fetchone()['c'],
            'halls': conn.execute('SELECT COUNT(*) as c FROM halls').fetchone()['c'],
            'sessions': conn.execute('SELECT COUNT(*) as c FROM sessions').fetchone()['c'],
            'bookings': conn.execute('SELECT COUNT(*) as c FROM bookings').fetchone()['c'],
            'users': conn.execute("SELECT COUNT(*) as c FROM users WHERE role='client'").fetchone()['c'],
        }

    data['sessions'] = conn.execute('''
        SELECT s.*, f.title, h.name as hall_name,
               CASE WHEN s.date < ? OR (s.date = ? AND s.time < ?) THEN 1 ELSE 0 END as is_past
        FROM sessions s
        JOIN films f ON f.id = s.film_id
        JOIN halls h ON h.id = s.hall_id
        ORDER BY is_past ASC, s.date ASC, s.time ASC
    ''', (data['today'], data['today'], data['now_time'])).fetchall()

    # ── Функции поиска/фильтрации на кассе ──
    # A05-2 аудита: search_q уходит в SQL ТОЛЬКО bind-параметром (?) в
    # `LIKE ? ESCAPE '\'` — значение не попадает в текст запроса, поэтому
    # кавычки и конструкции вида ' OR '1'='1 безвредны, инъекции нет.
    # Символы % и _ экранируются отдельно (ESCAPE) — это про wildcard-маски
    # LIKE, а не про безопасность SQL. Длина режется до 100 символов, чтобы
    # тяжёлый LIKE по трём полям не вырождался в полнотекстовый скан.
    search_q = (request.args.get('q') or '').strip().lower()[:100]
    filter_status = request.args.get('status') or ''
    if search_q or filter_status:
        # Поиск по коду билета, имени или email
        base = '''
            SELECT b.id, b.user_id, b.session_id, b.status,
                   b.promo_id, b.final_price,
                   st.row_num, st.seat_num,
                   u.email as user_email, u.name as user_name,
                   s.date, s.time, s.price as session_price,
                   f.title, f.id as film_id,
                   p.title as promo_title, p.discount as promo_discount,
                   COALESCE(b.custom_code,
                            upper(substr(f.title,1,1)) || f.id || '-' || b.id) as custom_code,
                   ('Ряд ' || st.row_num || ', место ' || st.seat_num) as seats_text
            FROM bookings b
            JOIN users u ON b.user_id = u.id
            JOIN sessions s ON b.session_id = s.id
            JOIN films f ON s.film_id = f.id
            JOIN seats st ON b.seat_id = st.id
            LEFT JOIN promotions p ON b.promo_id = p.id
            WHERE 1=1
        '''
        params = []
        if search_q:
            base += ' AND (LOWER(u.name) LIKE ? ESCAPE \'\\\' OR LOWER(u.email) LIKE ? ESCAPE \'\\\' OR LOWER(COALESCE(b.custom_code,"")) LIKE ? ESCAPE \'\\\')'
            # A05-14 аудита: % и _ в LIKE — это wildcards, а не литералы.
            # Раньше кассир мог ввести % и получить «любую строку» (обходил
            # логический фильтр по коду билета, засорял БД широким поиском).
            # Экранируем \, % и _ перед подстановкой.
            safe_q = search_q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            like = f'%{safe_q}%'
            params += [like, like, like]
        if filter_status:
            base += ' AND b.status=?'
            params.append(filter_status)
        base += ' ORDER BY b.id DESC'
        data['bookings'] = conn.execute(base, params).fetchall()
        search_mode = True
    else:
        search_mode = False

    # 1. Автоматическая отмена просроченных броней (за 30 минут до сеанса)
    deadline_time = (datetime.now() + timedelta(minutes=30)).strftime('%H:%M')
    current_date = date.today().strftime('%Y-%m-%d')

    conn.execute('''
        UPDATE bookings
        SET status = 'cancelled'
        WHERE status = 'booked'
          AND session_id IN (
              SELECT id FROM sessions
              WHERE date < ? OR (date = ? AND time <= ?)
          )
    ''', (current_date, current_date, deadline_time))
    conn.commit()

    # 2. Получаем данные из БД с правильным JOIN таблицы seats
    if search_mode:
        raw_bookings = []  # в режиме поиска data['bookings'] уже заполнен выше
    elif user['role'] == 'admin':
        raw_bookings = conn.execute('''
            SELECT b.id, b.user_id, b.session_id, b.status,
                   b.promo_id, b.final_price,
                   st.row_num, st.seat_num,
                   u.email as user_email, u.name as user_name,
                   s.date, s.time, s.price as session_price,
                   f.title, f.id as film_id,
                   p.title as promo_title, p.discount as promo_discount
            FROM bookings b
            JOIN users u ON b.user_id = u.id
            JOIN sessions s ON b.session_id = s.id
            JOIN films f ON s.film_id = f.id
            JOIN seats st ON b.seat_id = st.id
            LEFT JOIN promotions p ON b.promo_id = p.id
            ORDER BY b.id DESC
        ''').fetchall()
    elif user['role'] == 'cashier' and not search_mode:
        raw_bookings = conn.execute('''
            SELECT b.id, b.user_id, b.session_id, b.status,
                   b.promo_id, b.final_price,
                   st.row_num, st.seat_num,
                   u.email as user_email, u.name as user_name,
                   s.date, s.time, s.price as session_price,
                   f.title, f.id as film_id,
                   p.title as promo_title, p.discount as promo_discount
            FROM bookings b
            JOIN users u ON b.user_id = u.id
            JOIN sessions s ON b.session_id = s.id
            JOIN films f ON s.film_id = f.id
            JOIN seats st ON b.seat_id = st.id
            LEFT JOIN promotions p ON b.promo_id = p.id
            WHERE b.status = 'booked'
            ORDER BY s.date ASC, s.time ASC
        ''').fetchall()
    else:
        raw_bookings = []

    # 3. Обработка данных на лету и генерация уникального кода
    bookings_list = []
    for b in raw_bookings:
        b_dict = dict(b)
        b_dict['can_return'] = (b_dict['status'] == 'paid' and
                                (b_dict['date'] > current_date or
                                 (b_dict['date'] == current_date and b_dict['time'] > deadline_time)))

        # Первая буква названия фильма
        first_letter = b_dict['title'][0].upper() if b_dict['title'] else 'Б'
        film_id = b_dict['film_id']
        booking_id = b_dict['id']

        # Собираем короткий кастомный код билета (например: Х3-12)
        b_dict['custom_code'] = f"{first_letter}{film_id}-{booking_id}"

        # Склеиваем ряд и место из привязанной таблицы seats
        b_dict['seats_text'] = f"Ряд {b_dict['row_num']}, место {b_dict['seat_num']}"

        # Защита для шаблона: у старых/неполных выборок поля акции и цены могут отсутствовать.
        if 'session_price' not in b_dict:
            b_dict['session_price'] = b_dict.get('price', 0) or 0
        if 'promo_title' not in b_dict:
            b_dict['promo_title'] = None
        if 'promo_discount' not in b_dict:
            b_dict['promo_discount'] = 0
        if 'final_price' not in b_dict or b_dict.get('final_price') is None:
            b_dict['final_price'] = b_dict.get('session_price', 0) or 0

        bookings_list.append(b_dict)

    if not search_mode:
        # В режиме поиска data['bookings'] уже заполнен запросом выше;
        # обратная сборка через bookings_list нужна только для полного списка.
        data['bookings'] = bookings_list

    conn.close()
    return render_template('admin/personal.html', user=user, **data)


@staff_bp.route('/confirm_payment/<int:booking_id>', methods=['POST'])
def confirm_payment(booking_id):
    if 'user_id' not in session:
        return redirect(url_for('index'))

    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()

    if not user or user['role'] not in ('admin', 'cashier'):
        conn.close()
        flash('У вас нет прав для подтверждения оплаты.', 'danger')
        return redirect(url_for('personal'))

    recipient = conn.execute('''
        SELECT u.email, u.id as user_id, b.custom_code, f.title as film_title, f.id as film_id, s.id as session_id
        FROM bookings b
        JOIN users u ON u.id = b.user_id
        JOIN sessions s ON s.id = b.session_id
        JOIN films f ON f.id = s.film_id
        WHERE b.id=?
    ''', (booking_id,)).fetchone()

    # Обновляем статус билета на "оплачен"
    conn.execute("UPDATE bookings SET status = 'paid', payment_method = COALESCE(payment_method, 'bank') WHERE id = ?", (booking_id,))
    if recipient and not recipient['custom_code']:
        conn.execute('UPDATE bookings SET custom_code=? WHERE id=?',
                     (make_ticket_code(recipient['film_title'], recipient['film_id'], booking_id), booking_id))
    award_bonus_for_booking(conn, booking_id)
    log_audit(conn, current_user(), 'payment_confirm', f'Подтверждена оплата брони №{booking_id}')
    conn.commit()

    if recipient:
        # sqlite3.Row не имеет метода .get — обращаемся через индексацию
        session_link = '/session/' + str(recipient['session_id']) if recipient['session_id'] else '/cabinet'
        add_notification(
            conn, recipient['user_id'], 'payment',
            f'Билет на «{recipient["film_title"]}» оплачен',
            'Оплата подтверждена кассиром. Билет отправлен на почту.',
            session_link,
            'session', recipient['session_id']
        )
    conn.close()

    if recipient:
        send_paid_tickets_email(recipient['email'], [booking_id])

    return redirect(url_for('personal') + '?tab=bookings')


@staff_bp.route('/admin/sell_ticket', methods=['POST'])
@roles_required('admin', 'cashier')
def sell_ticket():
    session_id = request.form.get('session_id', type=int)
    client_email = request.form.get('client_email', '').strip().lower()
    seat_ids = request.form.getlist('seat_ids')
    promo_id = request.form.get('promo_id', type=int)
    payment_method = request.form.get('payment_method', '').strip()

    if not session_id or not seat_ids:
        return jsonify({'error': 'Не выбраны сеанс или места'}), 400
    if payment_method not in ('bank', 'bonus'):
        return jsonify({'error': 'Выберите способ оплаты'}), 400

    try:
        seat_ids = [int(s) for s in seat_ids]
    except ValueError:
        return jsonify({'error': 'Некорректные места'}), 400

    conn = get_db()
    client = conn.execute('SELECT id FROM users WHERE email=?', (client_email,)).fetchone()
    if not client:
        conn.close()
        return jsonify({'error': 'Клиент не найден'}), 404
    if client['id'] == current_user()['id']:
        conn.close()
        return jsonify({'error': 'Нельзя продавать билеты самому себе'}), 400

    sess = conn.execute('''
        SELECT s.id, s.price, s.date, s.time, s.hall_id, f.id as film_id, f.title as film_title
        FROM sessions s
        JOIN films f ON f.id = s.film_id
        WHERE s.id=?
    ''', (session_id,)).fetchone()
    if not sess:
        conn.close()
        return jsonify({'error': 'Сеанс не найден'}), 404

    now_date = date.today().isoformat()
    now_time = datetime.now().strftime('%H:%M')
    if sess['date'] < now_date or (sess['date'] == now_date and sess['time'] < now_time):
        conn.close()
        return jsonify({'error': 'Сеанс уже прошёл'}), 400

    # Проверяем, что все выбранные места действительно принадлежат залу этого сеанса
    valid_seat_ids = {r['id'] for r in conn.execute(
        'SELECT id FROM seats WHERE hall_id=?', (sess['hall_id'],)).fetchall()}
    if not set(seat_ids).issubset(valid_seat_ids) or len(seat_ids) != len(set(seat_ids)):
        conn.close()
        return jsonify({'error': 'Некорректный выбор мест'}), 400

    base_price = sess['price']

    discount = 0
    if promo_id:
        promo = conn.execute('SELECT discount FROM promotions WHERE id=?', (promo_id,)).fetchone()
        if promo:
            discount = max(0, min(100, int(promo['discount'] or 0)))
        else:
            promo_id = None

    final_price = round(base_price * (1 - discount / 100), 2)

    if payment_method == 'bonus':
        card = conn.execute('SELECT * FROM loyalty_cards WHERE user_id=?', (client['id'],)).fetchone()
        total_to_spend = int(round(final_price)) * len(seat_ids)
        if not card:
            conn.close()
            return jsonify({'error': 'У клиента нет бонусной карты'}), 400
        if int(card['bonus_balance'] or 0) < total_to_spend:
            conn.close()
            return jsonify({'error': 'Недостаточно бонусов на карте клиента'}), 400

    paid_booking_ids = []

    for seat_id in seat_ids:
        try:
            # Уникальный индекс (session_id, seat_id) для не-отменённых броней —
            # последняя линия обороны от гонки, даже если два запроса прошли
            # проверку "место свободно" одновременно.
            cur = conn.execute('''INSERT INTO bookings
                (user_id, session_id, seat_id, booked_at, status, custom_code, promo_id, final_price, payment_method)
                VALUES (?,?,?,?,?,?,?,?,?)''',
                (client['id'], session_id, seat_id,
                 datetime.now().isoformat(), 'paid', None, promo_id, final_price, payment_method))
        except sqlite3.IntegrityError:
            # A10-3 аудита: при гонке не просто откатываем — ещё и освобождаем
            # уже созданные в этом цикле брони, чтобы не оставить «сирот».
            conn.rollback()
            conn.close()
            return jsonify({'error': 'Одно из мест уже занято'}), 409

        booking_id = cur.lastrowid
        custom_code = make_ticket_code(sess['film_title'], sess['film_id'], booking_id)
        conn.execute('UPDATE bookings SET custom_code=? WHERE id=?', (custom_code, booking_id))

        paid_booking_ids.append(booking_id)

        if payment_method == 'bonus':
            ok, err = spend_bonuses_for_booking(conn, client['id'], booking_id, final_price)
            if not ok:
                conn.rollback()
                conn.close()
                return jsonify({'error': err}), 400
            award_bonus_for_booking(conn, booking_id)

    conn.commit()
    log_audit(conn, current_user(), 'sell_ticket',
              f'Продажа {len(paid_booking_ids)} билета(ов) клиенту {client_email} (сеанс №{session_id})')

    # Уведомление клиенту о покупке билетов на кассе
    add_notification(
        conn, client['id'], 'payment',
        f'Билеты на «{sess["film_title"]}» куплены',
        f'{fmt_date_ru(sess["date"])} в {sess["time"]} будет сеанс «{sess["film_title"]}». '
        f'Продано {len(paid_booking_ids)} шт.',
        f'/session/{session_id}',
        'session', session_id
    )
    conn.close()
    email_sent = send_paid_tickets_email(client_email, paid_booking_ids)
    return jsonify({
        'ok': True,
        'message': ('Билеты оплачены и отправлены на указанную почту' if email_sent
                    else 'Билеты проданы. Письмо не удалось отправить — билеты доступны в кабинете клиента'),
        'email_sent': email_sent
    })


@staff_bp.route('/client/book', methods=['POST'])
@login_required
@client_required
def client_book():
    session_id = request.form.get('session_id', type=int)
    seat_ids = request.form.getlist('seat_ids')
    action = request.form.get('action', 'book')
    promo_id = request.form.get('promo_id', type=int)
    payment_method = request.form.get('payment_method', '').strip()
    user = current_user()

    if not session_id or not seat_ids:
        return jsonify({'error': 'Не выбраны сеанс или места'}), 400
    if action == 'pay' and payment_method not in ('bank', 'bonus'):
        return jsonify({'error': 'Выберите способ оплаты'}), 400

    try:
        seat_ids = [int(s) for s in seat_ids]
    except ValueError:
        return jsonify({'error': 'Некорректные места'}), 400

    conn = get_db()
    sess = conn.execute('''
        SELECT s.id, s.price, s.date, s.time, s.hall_id, f.id as film_id, f.title as film_title
        FROM sessions s
        JOIN films f ON f.id = s.film_id
        WHERE s.id=?
    ''', (session_id,)).fetchone()
    if not sess:
        conn.close()
        return jsonify({'error': 'Сеанс не найден'}), 404

    now_date = date.today().isoformat()
    now_time = datetime.now().strftime('%H:%M')
    if sess['date'] < now_date or (sess['date'] == now_date and sess['time'] < now_time):
        conn.close()
        return jsonify({'error': 'Сеанс уже прошёл'}), 400

    valid_seat_ids = {r['id'] for r in conn.execute(
        'SELECT id FROM seats WHERE hall_id=?', (sess['hall_id'],)).fetchall()}
    if not set(seat_ids).issubset(valid_seat_ids) or len(seat_ids) != len(set(seat_ids)):
        conn.close()
        return jsonify({'error': 'Некорректный выбор мест'}), 400

    base_price = sess['price']

    discount = 0
    if promo_id:
        promo = conn.execute('SELECT discount FROM promotions WHERE id=?', (promo_id,)).fetchone()
        if promo:
            discount = max(0, min(100, int(promo['discount'] or 0)))
        else:
            promo_id = None

    final_price = round(base_price * (1 - discount / 100), 2)
    status = 'paid' if action == 'pay' else 'booked'

    if status == 'paid' and payment_method == 'bonus':
        card = conn.execute('SELECT * FROM loyalty_cards WHERE user_id=?', (user['id'],)).fetchone()
        total_to_spend = int(round(final_price)) * len(seat_ids)
        if not card:
            conn.close()
            return jsonify({'error': 'У вас нет бонусной карты'}), 400
        if int(card['bonus_balance'] or 0) < total_to_spend:
            conn.close()
            return jsonify({'error': 'Недостаточно бонусов на карте'}), 400

    paid_booking_ids = []

    for seat_id in seat_ids:
        try:
            cur = conn.execute('''INSERT INTO bookings
                (user_id, session_id, seat_id, booked_at, status, custom_code, promo_id, final_price, payment_method)
                VALUES (?,?,?,?,?,?,?,?,?)''',
                (user['id'], session_id, seat_id,
                 datetime.now().isoformat(), status, None, promo_id, final_price,
                 payment_method if status == 'paid' else None))
        except sqlite3.IntegrityError:
            conn.rollback()
            conn.close()
            return jsonify({'error': 'Одно из мест уже занято'}), 409

        booking_id = cur.lastrowid
        custom_code = make_ticket_code(sess['film_title'], sess['film_id'], booking_id)
        conn.execute('UPDATE bookings SET custom_code=? WHERE id=?', (custom_code, booking_id))

        if status == 'paid':
            paid_booking_ids.append(booking_id)

        if status == 'paid' and payment_method == 'bonus':
            ok, err = spend_bonuses_for_booking(conn, user['id'], booking_id, final_price)
            if not ok:
                conn.rollback()
                conn.close()
                return jsonify({'error': err}), 400
            award_bonus_for_booking(conn, booking_id)

    found_row = conn.execute('''
        SELECT f.title
        FROM sessions s
        JOIN films f ON f.id = s.film_id
        WHERE s.id=?
    ''', (session_id,)).fetchone()
    film_title = found_row['title'] if found_row else 'фильм'

    date_text = fmt_date_ru(sess['date'])
    if status == 'paid':
        add_notification(
            conn, user['id'], 'payment',
            f'Билеты на «{film_title}» оплачены',
            f'{date_text} в {sess["time"]} будет сеанс «{film_title}». Электронные билеты '
            f'можете увидеть у себя на почте или в личном кабинете',
            '/cabinet',
            'session', session_id
        )
    else:
        add_notification(
            conn, user['id'], 'booking',
            f'Места на «{film_title}» забронированы',
            f'{date_text} в {sess["time"]} будет сеанс «{film_title}». '
            f'Не забудьте оплатить бронь в личном кабинете',
            '/cabinet',
            'session', session_id
        )

    conn.commit()
    conn.close()
    if paid_booking_ids:
        email_sent = send_paid_tickets_email(user['email'], paid_booking_ids)
        return jsonify({
            'ok': True,
            'message': ('Билеты оплачены и отправлены на вашу почту' if email_sent
                        else 'Билеты оплачены. Письмо не удалось отправить — билеты доступны в личном кабинете'),
            'email_sent': email_sent
        })
    return jsonify({'ok': True, 'message': 'Места забронированы', 'email_sent': False})


# короткие endpoint'ы для url_for в шаблонах (см. register_blueprints в main.py)
ALL_STAFF_ENDPOINTS = [
    ('personal', personal),
    ('confirm_payment', confirm_payment),
    ('sell_ticket', sell_ticket),
    ('client_book', client_book),
]