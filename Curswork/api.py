import json as _json
from flask import Blueprint, request, jsonify
from datetime import date, datetime, timedelta

from db import get_db, log_audit
from utils import (
    current_user, generate_session_reminders,
    get_film_rating,
)
from security import login_required, client_required

api_bp = Blueprint('api', __name__)

ALL_API_ENDPOINTS = []  # (короткое_имя, view) заполняется внизу файла


@api_bp.route('/api/session_seats/<int:session_id>')
def api_session_seats(session_id):
    conn = get_db()
    sess = conn.execute('''
        SELECT s.*, f.title, f.poster, h.name as hall_name, h.rows, h.seats_per_row, h.capacity
        FROM sessions s
        JOIN films f ON s.film_id = f.id
        JOIN halls h ON s.hall_id = h.id
        WHERE s.id=?
    ''', (session_id,)).fetchone()

    # A10-9 аудита: несуществующий сеанс раньше падал с 500
    # (AttributeError: NoneType.hall_id). Теперь — 404.
    if not sess:
        conn.close()
        return {'error': 'not found'}, 404

    booked_seat_ids = [r['seat_id'] for r in
        conn.execute("SELECT seat_id FROM bookings WHERE session_id=? AND status != 'cancelled'",
                     (session_id,)).fetchall()]

    seats = conn.execute('SELECT * FROM seats WHERE hall_id=? ORDER BY row_num, seat_num',
                         (sess['hall_id'],)).fetchall()
    conn.close()

    rows = {}
    for seat in seats:
        r = seat['row_num']
        if r not in rows:
            rows[r] = []
        rows[r].append({
            'id': seat['id'],
            'num': seat['seat_num'],
            'booked': seat['id'] in booked_seat_ids
        })

    return {
        'title': sess['title'],
        'poster': sess['poster'],
        'hall_name': sess['hall_name'],
        'date': sess['date'],
        'time': sess['time'],
        'price': sess['price'],
        'rows': {str(k): v for k, v in rows.items()}
    }


@api_bp.route('/api/notifications')
@login_required
def api_notifications():
    """Список уведомлений текущего клиента + счётчик непрочитанных."""
    user = current_user()
    if user and user['role'] in ('admin', 'cashier'):
        return jsonify({'items': [], 'unread': 0})

    conn = get_db()
    generate_session_reminders(conn, user['id'])
    raw = conn.execute('''
        SELECT * FROM notifications
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT 20
    ''', (user['id'],)).fetchall()
    unread = conn.execute('''
        SELECT COUNT(*) as c FROM notifications
        WHERE user_id=? AND read=0
    ''', (user['id'],)).fetchone()['c']

    items = []
    for n in raw:
        # Для уведомлений о билетах (ref_type='session') подбираем последнюю
        # неотменённую бронь пользователя на этот сеанс — по ней откроем
        # модалку билета как в личном кабинете (api_ticket).
        booking_id = None
        if n['ref_type'] == 'session' and n['ref_id']:
            b = conn.execute('''
                SELECT id FROM bookings
                WHERE user_id=? AND session_id=? AND status != 'cancelled'
                ORDER BY id DESC LIMIT 1
            ''', (user['id'], n['ref_id'])).fetchone()
            if b:
                booking_id = b['id']
        # minutes_until для тоста «скоро сеанс»: раньше сервер поле не слал,
        # и тост не показывался никогда. Безопасно парсим из payload.
        minutes_until = None
        if n['type'] == 'session_reminder' and n['payload']:
            try:
                minutes_until = int(float(_json.loads(n['payload']).get('minutes_until')))
            except Exception:
                minutes_until = None
        items.append({
            'id': n['id'],
            'type': n['type'],
            'title': n['title'],
            'body': n['body'] or '',
            'link': n['link'] or '',
            'created_at': n['created_at'],
            'read': n['read'],
            'booking_id': booking_id,
            'minutes_until': minutes_until
        })
    conn.close()
    return jsonify({'items': items, 'unread': unread})


@api_bp.route('/api/review', methods=['POST'])
@login_required
@client_required
def api_review():
    user = current_user()
    film_id = request.form.get('film_id', type=int)
    rating = request.form.get('rating', type=int)
    comment = request.form.get('comment', '').strip()

    if not film_id:
        return jsonify({'ok': False, 'error': 'Не указан фильм'}), 400
    if rating is None or rating < 1 or rating > 5:
        return jsonify({'ok': False, 'error': 'Оценка от 1 до 5'}), 400
    if len(comment) > 1000:
        return jsonify({'ok': False, 'error': 'Комментарий слишком длинный'}), 400

    conn = get_db()
    exists = conn.execute(
        'SELECT id FROM reviews WHERE user_id=? AND film_id=?',
        (user['id'], film_id)).fetchone()
    if exists:
        conn.execute(
            'UPDATE reviews SET rating=?, comment=? WHERE id=?',
            (rating, comment, exists['id']))
    else:
        conn.execute(
            'INSERT INTO reviews (user_id, film_id, rating, comment, created_at) VALUES (?,?,?,?,?)',
            (user['id'], film_id, rating, comment, datetime.now().isoformat()))
    conn.commit()
    avg, cnt = get_film_rating(conn, film_id)
    conn.close()
    return jsonify({'ok': True, 'rating': avg, 'rating_count': cnt})


@api_bp.route('/api/favorite/<int:film_id>', methods=['POST'])
@login_required
@client_required
def api_favorite(film_id):
    user = current_user()
    conn = get_db()
    exists = conn.execute(
        'SELECT id FROM favorites WHERE user_id=? AND film_id=?',
        (user['id'], film_id)).fetchone()
    if exists:
        conn.execute('DELETE FROM favorites WHERE id=?', (exists['id'],))
        log_audit(conn, user, 'favorite_remove', f'Убран из избранного фильм id={film_id}')
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'favorite': False})
    conn.execute(
        'INSERT INTO favorites (user_id, film_id, created_at) VALUES (?,?,?)',
        (user['id'], film_id, datetime.now().isoformat()))
    log_audit(conn, user, 'favorite_add', f'Добавлен в избранное фильм id={film_id}')
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'favorite': True})


@api_bp.route('/api/notifications/read', methods=['POST'])
@login_required
def api_notifications_read():
    data = request.get_json(silent=True) or {}
    user = current_user()
    conn = get_db()
    if data.get('all'):
        conn.execute('UPDATE notifications SET read=1 WHERE user_id=?', (user['id'],))
    else:
        ids = data.get('ids') or []
        # A05-1 аудита: построение SQL через f-строку здесь БЕЗОПАСНО — в
        # f-строку попадают только символы-плейсхолдеры '?,?,?…' (по одному
        # на элемент списка), а сами значения передаются отдельными
        # bind-параметрами во втором аргументе execute(). Подставить SQL в
        # значение нельзя, даже если оно содержит "1; DROP TABLE users;".
        # Ниже добавлена защита по краям:
        #   * int() в try/except: нечисловой id отбрасывается, а не роняет
        #     запрос в HTTP 500 (раньше "ids":["abc"] давал 500);
        #   * потолок в 100 id: IN (?,?,…) не упирается в лимит числа
        #     переменных SQLite (SQLITE_MAX_VARIABLE_NUMBER).
        clean_ids = []
        for i in ids:
            try:
                clean_ids.append(int(i))
            except (TypeError, ValueError):
                continue
            if len(clean_ids) >= 100:
                break
        if clean_ids:
            marks = ','.join('?' for _ in clean_ids)
            conn.execute(f'UPDATE notifications SET read=1 WHERE user_id=? AND id IN ({marks})',
                         [user['id']] + clean_ids)
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@api_bp.route('/api/film_sessions/<int:film_id>')
@login_required
def api_film_sessions(film_id):
    today = date.today().isoformat()
    now_time = datetime.now().strftime('%H:%M')
    conn = get_db()
    sessions = conn.execute('''
        SELECT s.id, s.date, s.time, s.price, h.name as hall_name
        FROM sessions s
        JOIN halls h ON h.id = s.hall_id
        WHERE s.film_id = ?
        AND (s.date > ? OR (s.date = ? AND s.time > ?))
        ORDER BY s.date, s.time
    ''', (film_id, today, today, now_time)).fetchall()
    conn.close()
    return {'sessions': [dict(s) for s in sessions]}


@api_bp.route('/api/film/<int:film_id>')
def api_film(film_id):
    today = date.today()
    week_later = today + timedelta(days=7)
    now_time = datetime.now().strftime('%H:%M')
    today_iso = today.isoformat()

    conn = get_db()
    film = conn.execute('SELECT * FROM films WHERE id=?', (film_id,)).fetchone()
    if not film:
        conn.close()
        return {'error': 'not found'}, 404

    sessions = conn.execute('''
        SELECT s.id, s.date, s.time, s.price, h.name as hall_name, h.format
        FROM sessions s
        JOIN halls h ON h.id = s.hall_id
        WHERE s.film_id = ?
        AND (s.date > ? OR (s.date = ? AND s.time > ?))
        AND s.date <= ?
        ORDER BY s.date, s.time
    ''', (film_id, today_iso, today_iso, now_time, week_later.isoformat())).fetchall()

    avg_rating, rating_count = get_film_rating(conn, film_id)

    # Избранное текущего пользователя
    is_favorite = False
    user = current_user()
    if user and user['role'] == 'client':
        is_favorite = conn.execute(
            'SELECT id FROM favorites WHERE user_id=? AND film_id=?',
            (user['id'], film_id)).fetchone() is not None
    conn.close()

    # Группируем по датам
    days_ru = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
    months_ru = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']
    dates = {}
    for s in sessions:
        d = s['date']
        if d not in dates:
            dt = date.fromisoformat(d)
            dates[d] = {
                'label': f"{dt.day} {months_ru[dt.month-1]}",
                'weekday': days_ru[dt.weekday()],
                'sessions': []
            }
        dates[d]['sessions'].append({
            'id': s['id'],
            'time': s['time'],
            'date': s['date'],
            'price': s['price'],
            'hall_name': s['hall_name'],
            'format': s['format']
        })

    return {
        'id': film['id'],
        'title': film['title'],
        'description': film['description'] or '',
        'genre': film['genre'] or '',
        'duration': film['duration'] or 0,
        'poster': film['poster'] or '',
        'rating': avg_rating,
        'rating_count': rating_count,
        'is_favorite': is_favorite,
        'dates': dates
    }


# короткие endpoint'ы для url_for в шаблонах (см. register_blueprints в main.py)
ALL_API_ENDPOINTS = [
    ('api_session_seats', api_session_seats),
    ('api_notifications', api_notifications),
    ('api_review', api_review),
    ('api_favorite', api_favorite),
    ('api_notifications_read', api_notifications_read),
    ('api_film_sessions', api_film_sessions),
    ('api_film', api_film),
]