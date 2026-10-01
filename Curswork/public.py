import sqlite3
from flask import Blueprint, render_template, request, redirect, url_for, flash
from datetime import date, datetime

from db import get_db, log_audit
from utils import (
    current_user, get_favorite_ids, add_notification,
    get_film_rating, get_next_loyalty_level_info,
    create_loyalty_card, add_loyalty_history, fmt_date_ru,
)
from security import login_required, roles_required, client_required, require_role

public_bp = Blueprint('public', __name__)


@public_bp.route('/')
def index():
    user = current_user()
    if user and user['role'] in ('admin', 'cashier'):
        return redirect(url_for('personal'))
    conn = get_db()
    today = date.today().isoformat()
    now_time = datetime.now().strftime('%H:%M')

    # Все фильмы
    films = conn.execute('SELECT * FROM films').fetchall()

    # Топ 5 по купленным билетам
    top_films = conn.execute('''
        SELECT f.*, COUNT(b.id) as ticket_count
        FROM films f
        LEFT JOIN sessions s ON s.film_id = f.id
        LEFT JOIN bookings b ON b.session_id = s.id AND b.status = 'paid'
        GROUP BY f.id
        ORDER BY ticket_count DESC
        LIMIT 5
    ''').fetchall()

    # Сегодня в кино (только предстоящие)
    upcoming = conn.execute('''
        SELECT DISTINCT f.*, MIN(s.time) as next_time
        FROM sessions s
        JOIN films f ON s.film_id = f.id
        JOIN halls h ON s.hall_id = h.id
        WHERE s.date = ? AND s.time > ?
        GROUP BY f.id
        ORDER BY next_time
    ''', (today, now_time)).fetchall()

    promotions = conn.execute('SELECT * FROM promotions ORDER BY id').fetchall()

    favorite_ids = get_favorite_ids(conn, user['id']) if user else set()
    conn.close()
    return render_template('user/index.html',
                           films=films,
                           top_films=top_films,
                           upcoming=upcoming,
                           promotions=promotions,
                           user=user,
                           today_str=today,
                           favorite_ids=favorite_ids)


@public_bp.route('/films')
def all_films():
    user = current_user()
    conn = get_db()
    today = date.today().isoformat()
    now_time = datetime.now().strftime('%H:%M')

    films = conn.execute('''
        SELECT f.*,
               COUNT(DISTINCT b.id) as ticket_count,
               MIN(CASE WHEN s.date > ? OR (s.date = ? AND s.time > ?)
                        THEN s.date || ' ' || s.time ELSE NULL END) as next_session
        FROM films f
        LEFT JOIN sessions s ON s.film_id = f.id
        LEFT JOIN bookings b ON b.session_id = s.id AND b.status = 'paid'
        GROUP BY f.id
        ORDER BY f.title
    ''', (today, today, now_time)).fetchall()

    genres = conn.execute('SELECT DISTINCT genre FROM films WHERE genre IS NOT NULL ORDER BY genre').fetchall()
    promotions = conn.execute('SELECT * FROM promotions ORDER BY id').fetchall()
    favorite_ids = get_favorite_ids(conn, user['id']) if user else set()
    conn.close()
    return render_template('user/films.html', films=films, genres=genres, promotions=promotions,
                           user=user, favorite_ids=favorite_ids)


@public_bp.route('/promotions')
def promotions():
    conn = get_db()
    promos = conn.execute('SELECT * FROM promotions ORDER BY id').fetchall()
    conn.close()
    return render_template('user/promotions.html', promos=promos, user=current_user())


@public_bp.route('/loyalty')
def loyalty():
    user = current_user()
    levels = [
        {'name': 'Basic', 'limit': 'сразу после выпуска', 'cashback': 3},
        {'name': 'Silver', 'limit': 'от 5 000 ₽ покупок', 'cashback': 5},
        {'name': 'Gold', 'limit': 'от 15 000 ₽ покупок', 'cashback': 10},
        {'name': 'Platinum', 'limit': 'от 30 000 ₽ покупок', 'cashback': 15},
    ]

    card = None
    history = []
    next_level = None
    stats = {'paid_tickets': 0, 'paid_sum': 0}

    if user and user['role'] == 'client':
        conn = get_db()
        card = conn.execute('SELECT * FROM loyalty_cards WHERE user_id=?', (user['id'],)).fetchone()
        if card:
            history = conn.execute('''
                SELECT h.*,
                       f.title as film_title,
                       s.date as session_date,
                       s.time as session_time,
                       halls.name as hall_name,
                       seats.row_num,
                       seats.seat_num,
                       COALESCE(b.final_price, s.price) as ticket_price,
                       b.payment_method
                FROM bonus_history h
                LEFT JOIN bookings b ON b.id = h.booking_id
                LEFT JOIN sessions s ON s.id = b.session_id
                LEFT JOIN films f ON f.id = s.film_id
                LEFT JOIN halls ON halls.id = s.hall_id
                LEFT JOIN seats ON seats.id = b.seat_id
                WHERE h.user_id=?
                ORDER BY h.id DESC
                LIMIT 20
            ''', (user['id'],)).fetchall()
            next_level = get_next_loyalty_level_info(card['total_spent'])
        stats_row = conn.execute('''
            SELECT COUNT(*) as paid_tickets,
                   COALESCE(SUM(COALESCE(b.final_price, s.price)), 0) as paid_sum
            FROM bookings b
            JOIN sessions s ON s.id = b.session_id
            WHERE b.user_id=? AND b.status='paid' AND b.payment_method='bonus'
        ''', (user['id'],)).fetchone()
        stats = dict(stats_row)
        conn.close()

    return render_template('user/loyalty.html',
                           user=user,
                           card=card,
                           history=history,
                           levels=levels,
                           next_level=next_level,
                           stats=stats)


@public_bp.route('/loyalty/create', methods=['POST'])
@login_required
def loyalty_create():
    # A06-1 аудита: карта не выпускается по прямому POST — только после
    # подтверждения кода из письма. Это осознанный deny-by-default дизайн:
    # бесконтрольный выпуск карт позволял бы плодить аккаунты с бонусами.
    # Попытка фиксируется в журнале аудита.
    conn = get_db()
    user = current_user()
    log_audit(conn, user, 'loyalty_card_denied',
              'Попытка выпустить бонусную карту без подтверждения кода')
    conn.close()
    flash('Для выпуска бонусной карты подтвердите код из письма.', 'warning')
    return redirect(url_for('loyalty'))


@public_bp.route('/loyalty/topup', methods=['POST'])
@login_required
def loyalty_topup():
    # A06-1 аудита: самостоятельное пополнение бонусного счёта клиентом
    # отключено НАВСЕГДА (deny-by-default). Раньше клиент мог начислить себе
    # любое количество бонусов без оплаты и расплатиться ими за билеты —
    # это была дыра «бесплатные деньги». Реального платёжного шлюза в
    # проекте нет, поэтому эндпоинт всегда отвечает отказом, а каждая
    # попытка записывается в журнал аудита (видны злоупотребления).
    # Законное пополнение — только через кассу/админку:
    #   /admin/loyalty/topup/<user_id>  (roles_required('admin')).
    conn = get_db()
    user = current_user()
    log_audit(conn, user, 'loyalty_self_topup_denied',
              'Попытка самостоятельного пополнения бонусного счёта')
    conn.close()
    flash('Пополнение бонусного счёта проводится только на кассе.', 'warning')
    return redirect(url_for('loyalty'))


@public_bp.route('/admin/loyalty/topup/<int:user_id>', methods=['POST'])
@roles_required('admin')
def admin_loyalty_topup(user_id):
    """Пополнение бонусного счёта кассиром/админом — например, после оплаты наличными на кассе."""
    amount = request.form.get('amount', type=int)
    if not amount or amount <= 0 or amount > 100000:
        flash('Введите корректную сумму пополнения', 'danger')
        return redirect(url_for('personal') + '?tab=users')

    if current_user()['id'] == user_id:
        flash('Нельзя пополнять бонусный счёт самому себе.', 'warning')
        return redirect(url_for('personal') + '?tab=users')

    conn = get_db()
    target = conn.execute("SELECT id, role, email FROM users WHERE id=?", (user_id,)).fetchone()
    if not target or target['role'] != 'client':
        conn.close()
        flash('Пользователь не найден', 'danger')
        return redirect(url_for('personal') + '?tab=users')

    card = create_loyalty_card(conn, user_id)
    new_balance = int(card['bonus_balance'] or 0) + amount
    conn.execute('UPDATE loyalty_cards SET bonus_balance=? WHERE user_id=?', (new_balance, user_id))
    add_loyalty_history(conn, user_id, amount, f'Пополнение на кассе ({current_user()["name"]})', new_balance)
    # Каждое пополнение пишется в журнал аудита — чтобы «рисование» бонусов
    # было не анонимным, а привязанным к кассиру (A06-3 аудита).
    log_audit(conn, current_user(), 'loyalty_topup',
              f'Пополнение бонусов на {amount} для {target["email"]} (id {user_id})')
    conn.commit()
    conn.close()

    flash('Баланс бонусной карты пополнен', 'success')
    return redirect(url_for('personal') + '?tab=users')


@public_bp.route('/film/<int:film_id>')
def film(film_id):
    conn = get_db()
    f = conn.execute('SELECT * FROM films WHERE id=?', (film_id,)).fetchone()
    if not f:
        conn.close()
        return redirect(url_for('index'))
    today = date.today().isoformat()
    sessions = conn.execute('''
        SELECT s.*, h.name as hall_name
        FROM sessions s
        JOIN halls h ON s.hall_id = h.id
        WHERE s.film_id=? AND s.date >= ?
        ORDER BY s.date, s.time
    ''', (film_id, today)).fetchall()

    # Рейтинг и отзывы
    avg_rating, rating_count = get_film_rating(conn, film_id)
    reviews = conn.execute('''
        SELECT r.*, u.name as user_name
        FROM reviews r
        JOIN users u ON u.id = r.user_id
        WHERE r.film_id=?
        ORDER BY r.id DESC
        LIMIT 30
    ''', (film_id,)).fetchall()

    # Похожие фильмы по жанру (исключая сам фильм)
    similar = conn.execute('''
        SELECT * FROM films
        WHERE id != ? AND genre = ?
        ORDER BY id LIMIT 4
    ''', (film_id, f['genre'])).fetchall()

    # Проверка, есть ли фильм в избранном у текущего пользователя
    my_review = None
    is_favorite = False
    user = current_user()
    if user and user['role'] == 'client':
        my_review = conn.execute(
            'SELECT * FROM reviews WHERE user_id=? AND film_id=?',
            (user['id'], film_id)).fetchone()
        is_favorite = conn.execute(
            'SELECT id FROM favorites WHERE user_id=? AND film_id=?',
            (user['id'], film_id)).fetchone() is not None

    conn.close()
    return render_template('user/film.html', film=f, sessions=sessions,
                           reviews=reviews, my_review=my_review, is_favorite=is_favorite,
                           avg_rating=avg_rating, rating_count=rating_count,
                           similar=similar, user=current_user())


@public_bp.route('/session/<int:session_id>')
@login_required
def session_view(session_id):
    # A06-2 аудита: схема зала — только предстоящим сеансам. Сеансы «в прошлом»
    # не открываются даже для кассира: места там не бронируются, бонусы не
    # списываются, возврат бонусов по прошлым билетам тоже нереализован.
    # Раньше прошедший сеанс открывался и давал доступ к «занято»-карте.
    conn = get_db()
    sess = conn.execute('''
        SELECT s.*, f.title, f.duration, h.name as hall_name, h.rows, h.seats_per_row
        FROM sessions s
        JOIN films f ON s.film_id = f.id
        JOIN halls h ON s.hall_id = h.id
        WHERE s.id=?
    ''', (session_id,)).fetchone()
    if not sess:
        conn.close()
        return redirect(url_for('index'))
    now_date = date.today().isoformat()
    now_time = datetime.now().strftime('%H:%M')
    if sess['date'] < now_date or (sess['date'] == now_date and sess['time'] < now_time):
        conn.close()
        flash('Сеанс уже прошёл — схема зала не открывается.', 'warning')
        return redirect(url_for('film', film_id=sess['film_id']))
    require_role('client', 'cashier', 'admin')

    # Занятые места (только активные брони; отменённые освобождают место)
    booked_seat_ids = [r['seat_id'] for r in
                       conn.execute("SELECT seat_id FROM bookings WHERE session_id=? AND status != 'cancelled'",
                                    (session_id,)).fetchall()]

    # Все места зала
    seats = conn.execute('SELECT * FROM seats WHERE hall_id=? ORDER BY row_num, seat_num',
                         (sess['hall_id'],)).fetchall()
    conn.close()

    # Группируем по рядам
    rows = {}
    for seat in seats:
        r = seat['row_num']
        if r not in rows:
            rows[r] = []
        rows[r].append({'id': seat['id'], 'num': seat['seat_num'],
                        'booked': seat['id'] in booked_seat_ids,
                        'seat_type': seat['seat_type'] or 'standard'})

    return render_template('user/session.html', sess=sess, rows=rows, user=current_user())


@public_bp.route('/book', methods=['POST'])
@login_required
@client_required
def book():
    session_id = request.form.get('session_id', type=int)
    seat_id = request.form.get('seat_id', type=int)
    user = current_user()

    conn = get_db()
    # A01-1 аудита: бронировать можно только будущие сеансы. Раньше можно было
    # создать бронь на прошедший сеанс — запись оставалась в БД и засорляла
    # лимиты/метрики.
    sess = conn.execute('SELECT date, time, hall_id FROM sessions WHERE id=?', (session_id,)).fetchone()
    if not sess:
        flash('Сеанс не найден.', 'danger')
        conn.close()
        return redirect(url_for('index'))

    # A06-9 аудита: место обязано принадлежать залу ЭТОГО сеанса. Раньше можно
    # было прислать seat_id из чужого зала — запись создавалась, засоряла БД
    # и ломала логику кассира (row_num/seat_num из чужого зала).
    seat = conn.execute('SELECT id FROM seats WHERE id=? AND hall_id=?',
                        (seat_id, sess['hall_id'])).fetchone()
    if not seat:
        flash('Место не принадлежит залу этого сеанса.', 'danger')
        conn.close()
        return redirect(url_for('session_view', session_id=session_id))
    now_date = date.today().isoformat()
    now_time = datetime.now().strftime('%H:%M')
    if sess['date'] < now_date or (sess['date'] == now_date and sess['time'] < now_time):
        flash('Нельзя забронировать место на прошедший сеанс.', 'danger')
        conn.close()
        return redirect(url_for('session_view', session_id=session_id))

    # Проверяем, не занято ли уже (только активные брони)
    existing = conn.execute(
        "SELECT id FROM bookings WHERE session_id=? AND seat_id=? AND status != 'cancelled'",
        (session_id, seat_id)).fetchone()
    if existing:
        flash('Это место уже занято. Выберите другое.', 'danger')
        conn.close()
        return redirect(url_for('session_view', session_id=session_id))

    try:
        cur = conn.execute('INSERT INTO bookings (user_id, session_id, seat_id, booked_at, status) VALUES (?,?,?,?,?)',
                           (user['id'], session_id, seat_id, datetime.now().isoformat(), 'booked'))
    except sqlite3.IntegrityError:
        # Гонка: два параллельных запроса прошли проверку «место свободно»
        # одновременно — уникальный индекс idx_unique_active_seat ловит вторую
        # бронь (A10-1 аудита). Вместо 500-й — обычное сообщение.
        flash('Это место уже занято. Выберите другое.', 'danger')
        conn.close()
        return redirect(url_for('session_view', session_id=session_id))
    conn.commit()

    log_audit(conn, user, 'booking_created',
              f'Бронь места (место {seat_id}, сеанс {session_id})')

    # Уведомление о бронировании
    row = conn.execute('''
        SELECT f.title, s.date, s.time, h.name as hall_name
        FROM sessions s
        JOIN films f ON f.id = s.film_id
        JOIN halls h ON h.id = s.hall_id
        WHERE s.id=?
    ''', (session_id,)).fetchone()
    if row:
        add_notification(
            conn, user['id'], 'booking',
            f'Билет на «{row["title"]}» забронирован',
            f'{fmt_date_ru(row["date"])} в {row["time"]} • зал {row["hall_name"]}. Не забудьте оплатить в личном кабинете.',
            f'/session/{session_id}',
            'session', session_id
        )
    conn.close()
    flash('Бронирование успешно оформлено!', 'success')
    return redirect(url_for('cabinet'))


@public_bp.route('/terms')
def terms():
    return render_template('user/legal.html', type='terms', user=current_user())


@public_bp.route('/privacy')
def privacy():
    return render_template('user/legal.html', type='privacy', user=current_user())


# короткие endpoint'ы для url_for в шаблонах (см. register_blueprints в main.py)
ALL_PUBLIC_ENDPOINTS = [
    ('index', index),
    ('all_films', all_films),
    ('promotions', promotions),
    ('loyalty', loyalty),
    ('loyalty_create', loyalty_create),
    ('loyalty_topup', loyalty_topup),
    ('admin_loyalty_topup', admin_loyalty_topup),
    ('film', film),
    ('session_view', session_view),
    ('book', book),
    ('terms', terms),
    ('privacy', privacy),
]