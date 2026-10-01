import logging
from flask import Blueprint, render_template, redirect, url_for, request, flash
from datetime import date, datetime, timedelta
import sqlite3
import os

from db import get_db, log_audit
from utils import (
    current_user, notify_all_clients,
    save_uploaded_image,
)
from security import admin_required

logger = logging.getLogger(__name__)

admin_bp = Blueprint('admin', __name__)

ALL_ADMIN_ENDPOINTS = []  # (короткое_имя, view) заполняется внизу файла


@admin_bp.route('/admin/audit')
@admin_required
def admin_audit():
    """Журнал действий — кто и когда что делал в системе."""
    conn = get_db()
    rows = conn.execute('''
        SELECT a.*, u.name as uname FROM audit_log a
        LEFT JOIN users u ON u.id = a.user_id
        ORDER BY a.id DESC LIMIT 300
    ''').fetchall()
    conn.close()
    return render_template('admin/audit.html', rows=rows, user=current_user())


@admin_bp.route('/admin')
@admin_required
def admin():
    conn = get_db()
    stats = {
        'films': conn.execute('SELECT COUNT(*) as c FROM films').fetchone()['c'],
        'halls': conn.execute('SELECT COUNT(*) as c FROM halls').fetchone()['c'],
        'sessions': conn.execute('SELECT COUNT(*) as c FROM sessions').fetchone()['c'],
        'bookings': conn.execute('SELECT COUNT(*) as c FROM bookings').fetchone()['c'],
        'users': conn.execute("SELECT COUNT(*) as c FROM users WHERE role='client'").fetchone()['c'],
    }

    # ── Дополнительная статистика на дашборд ──
    today = date.today().isoformat()
    now_time = datetime.now().strftime('%H:%M')

    # Посещаемость ближайших 7 дней
    next7 = []
    for d in range(1, 8):
        day = (date.today() + timedelta(days=d)).isoformat()
        cnt = conn.execute('''
            SELECT COUNT(*) as c FROM bookings b
            JOIN sessions s ON s.id = b.session_id
            WHERE s.date=? AND b.status='paid'
        ''', (day,)).fetchone()['c']
        n = conn.execute('SELECT COUNT(*) as c FROM sessions WHERE date=?', (day,)).fetchone()['c']
        dt = date.fromisoformat(day)
        next7.append({
            'label': f"{dt.day}.{dt.month:02d}",
            'name': dt.strftime('%a'),
            'bookings': cnt,
            'sessions': n,
        })

    # Продажи по дням за последние 14 дней
    sales_14 = []
    for d in range(13, -1, -1):
        day = (date.today() - timedelta(days=d)).isoformat()
        cnt = conn.execute('''
            SELECT COUNT(*) as c FROM bookings b
            JOIN sessions s ON s.id = b.session_id
            WHERE s.date=? AND b.status='paid'
        ''', (day,)).fetchone()['c']
        dt = date.fromisoformat(day)
        sales_14.append({'label': f"{dt.day}.{dt.month:02d}", 'bookings': cnt})

    stats['today_bookings'] = conn.execute('''
        SELECT COUNT(*) as c FROM bookings b
        JOIN sessions s ON s.id = b.session_id
        WHERE s.date=? AND b.status='paid'
    ''', (today,)).fetchone()['c']

    stats['week_revenue'] = conn.execute('''
        SELECT COALESCE(SUM(COALESCE(b.final_price, s.price, 0)), 0) as s FROM bookings b
        JOIN sessions s ON s.id = b.session_id
        WHERE b.status='paid' AND s.date BETWEEN ? AND ?
    ''', (today, (date.today() + timedelta(days=6)).isoformat())).fetchone()['s']

    stats['fill_rate'] = round(100 * (
        conn.execute('SELECT COUNT(*) as c FROM bookings WHERE status != "cancelled"').fetchone()['c'] /
        max(1, conn.execute('SELECT COUNT(*) as c FROM sessions').fetchone()['c'])
    ))

    stats['cashiers'] = conn.execute("SELECT COUNT(*) as c FROM users WHERE role='cashier'").fetchone()['c']
    stats['audit_count'] = conn.execute('SELECT COUNT(*) as c FROM audit_log').fetchone()['c']

    # Топ фильмов по рейтингу зрителей + последние отзывы (для дашборда)
    top_rated = conn.execute('''
        SELECT f.title, ROUND(AVG(r.rating), 2) as avg_rating, COUNT(r.id) as review_count
        FROM films f
        JOIN reviews r ON r.film_id = f.id
        GROUP BY f.id
        HAVING COUNT(r.id) >= 1
        ORDER BY avg_rating DESC, review_count DESC
        LIMIT 5
    ''').fetchall()
    recent_reviews = conn.execute('''
        SELECT r.*, u.name as user_name, f.title as film_title
        FROM reviews r
        JOIN users u ON u.id = r.user_id
        JOIN films f ON f.id = r.film_id
        ORDER BY r.id DESC
        LIMIT 6
    ''').fetchall()

    # Последние события журнала
    audit_recent = conn.execute('''
        SELECT * FROM audit_log ORDER BY id DESC LIMIT 8
    ''').fetchall()

    # Топ-5 по кассовым сборам
    top_revenue = conn.execute('''
        SELECT f.title, COALESCE(SUM(COALESCE(b.final_price, s.price, 0)), 0) as revenue,
               COUNT(b.id) as tickets
        FROM films f
        LEFT JOIN sessions s ON s.film_id = f.id
        LEFT JOIN bookings b ON b.session_id = s.id AND b.status='paid'
        GROUP BY f.id
        ORDER BY revenue DESC
        LIMIT 5
    ''').fetchall()

    conn.close()
    return render_template('admin/index.html', user=current_user(), stats=stats,
                           next7=next7, sales_14=sales_14,
                           top_revenue=top_revenue, audit_recent=audit_recent,
                           top_rated=top_rated, recent_reviews=recent_reviews)


# ═══════════════════════════════════════════════════════════════
# Акции
# ═══════════════════════════════════════════════════════════════


@admin_bp.route('/admin/promotions/add', methods=['POST'])
@admin_required
def admin_promotion_add():
    title = request.form.get('title', '').strip()
    description = request.form.get('description', '').strip()
    discount = request.form.get('discount', 0)
    if not title:
        return redirect(url_for('personal') + '?tab=promotions')

    discount = max(0, min(100, int(discount or 0)))

    conn = get_db()
    conn.execute('INSERT INTO promotions (title, description, discount) VALUES (?,?,?)',
                 (title, description, discount))
    promo_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

    filename, error = save_uploaded_image(request.files.get('image'), 'promotions', 'promo')
    if error:
        flash(error, 'danger')
    elif filename:
        conn.execute('UPDATE promotions SET image=? WHERE id=?', (filename, promo_id))

    conn.commit()

    # Рассылаем уведомление о новой акции всем клиентам
    notify_all_clients(
        conn, 'promo',
        f'Новая акция: {title}',
        f'Скидка до {discount}%! {description}'.strip(),
        '/promotions'
    )

    conn.close()
    return redirect(url_for('personal') + '?tab=promotions')


@admin_bp.route('/admin/promotions/delete/<int:promo_id>', methods=['POST'])
@admin_required
def admin_promotion_delete(promo_id):
    conn = get_db()
    promo = conn.execute('SELECT image FROM promotions WHERE id=?', (promo_id,)).fetchone()
    if promo and promo['image']:
        path = os.path.join('static', 'promotions', promo['image'])
        if os.path.exists(path):
            os.remove(path)
    conn.execute('DELETE FROM promotions WHERE id=?', (promo_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('personal') + '?tab=promotions')


@admin_bp.route('/admin/promotions/image/<int:promo_id>', methods=['POST'])
@admin_required
def admin_promotion_image(promo_id):
    filename, error = save_uploaded_image(request.files.get('image'), 'promotions', 'promo')
    if error:
        flash(error, 'danger')
    elif filename:
        conn = get_db()
        conn.execute('UPDATE promotions SET image=? WHERE id=?', (filename, promo_id))
        conn.commit()
        conn.close()
    return redirect(url_for('personal') + '?tab=promotions')


@admin_bp.route('/admin/promotions/desc/<int:promo_id>', methods=['POST'])
@admin_required
def admin_promotion_desc(promo_id):
    description = request.form.get('description', '').strip()
    conn = get_db()
    conn.execute('UPDATE promotions SET description=? WHERE id=?', (description, promo_id))
    conn.commit()
    conn.close()
    return redirect(url_for('personal') + '?tab=promotions')


@admin_bp.route('/admin/promotions/edit/<int:promo_id>', methods=['POST'])
@admin_required
def admin_promotion_edit(promo_id):
    title = request.form.get('title', '').strip()
    discount = request.form.get('discount', 0)
    if not title:
        return redirect(url_for('personal') + '?tab=promotions')
    conn = get_db()
    conn.execute('UPDATE promotions SET title=?, discount=? WHERE id=?', (title, discount, promo_id))
    conn.commit()
    conn.close()
    return redirect(url_for('personal') + '?tab=promotions')


# ═══════════════════════════════════════════════════════════════
# Фильмы
# ═══════════════════════════════════════════════════════════════


@admin_bp.route('/admin/films')
@admin_required
def admin_films():
    conn = get_db()
    films = conn.execute('SELECT * FROM films').fetchall()
    conn.close()
    return render_template('admin/films.html', films=films, user=current_user())


@admin_bp.route('/admin/films/poster/<int:film_id>', methods=['POST'])
@admin_required
def admin_film_poster(film_id):
    filename, error = save_uploaded_image(request.files.get('poster'), 'posters', 'film')
    if error:
        flash(error, 'danger')
    elif filename:
        conn = get_db()
        conn.execute('UPDATE films SET poster=? WHERE id=?', (filename, film_id))
        conn.commit()
        conn.close()
    return redirect(url_for('personal') + '?tab=films')


@admin_bp.route('/admin/films/desc/<int:film_id>', methods=['POST'])
@admin_required
def admin_film_desc(film_id):
    description = request.form.get('description', '').strip()
    conn = get_db()
    conn.execute('UPDATE films SET description=? WHERE id=?', (description, film_id))
    conn.commit()
    conn.close()
    return redirect(url_for('personal') + '?tab=films')


@admin_bp.route('/admin/films/add', methods=['POST'])
@admin_required
def admin_film_add():
    title = request.form.get('title', '').strip()
    description = request.form.get('description', '').strip()
    genre = request.form.get('genre', '').strip()
    duration = request.form.get('duration', '').strip()

    if not title or not duration or not duration.isdigit():
        flash('Заполните название и корректную длительность', 'danger')
        return redirect(url_for('personal') + '?tab=films')

    poster_filename, error = save_uploaded_image(request.files.get('poster'), 'posters', 'film')
    if error:
        flash(error, 'danger')
        poster_filename = ''

    conn = get_db()
    conn.execute('INSERT INTO films (title, description, genre, duration, poster) VALUES (?,?,?,?,?)',
                 (title, description, genre, int(duration), poster_filename or ''))
    conn.commit()
    conn.close()
    return redirect(url_for('personal') + '?tab=films')


@admin_bp.route('/admin/films/edit/<int:film_id>', methods=['GET', 'POST'])
@admin_required
def admin_film_edit(film_id):
    conn = get_db()
    film = conn.execute('SELECT * FROM films WHERE id=?', (film_id,)).fetchone()
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        genre = request.form.get('genre', '').strip()
        duration = request.form.get('duration', '').strip()
        poster = request.form.get('poster', '').strip()

        if not title or not duration or not duration.isdigit():
            conn.close()
            flash('Заполните название и корректную длительность (целое число минут)', 'danger')
            return redirect(url_for('personal') + '?tab=films')

        duration = int(duration)
        if duration < 1 or duration > 600:
            conn.close()
            flash('Длительность должна быть от 1 до 600 минут', 'danger')
            return redirect(url_for('personal') + '?tab=films')

        # poster приходит из формы как имя файла — не даём подставить произвольный
        # путь/URL. Разрешаем либо пусто, либо имя без разделителей пути.
        if poster and (os.sep in poster or '/' in poster or '..' in poster):
            conn.close()
            flash('Некорректное имя постера', 'danger')
            return redirect(url_for('personal') + '?tab=films')

        conn.execute('UPDATE films SET title=?, description=?, genre=?, duration=?, poster=? WHERE id=?',
                     (title, description, genre, duration, poster, film_id))
        conn.commit()
        conn.close()
        flash('Фильм обновлён.', 'success')
        return redirect(url_for('personal') + '?tab=films')
    conn.close()
    return render_template('admin/film_form.html', film=film, user=current_user())


@admin_bp.route('/admin/films/delete/<int:film_id>', methods=['POST'])
@admin_required
def admin_film_delete(film_id):
    conn = get_db()
    # У фильма могут быть сеансы/брони/отзывы/избранное — удаляем их тоже,
    # иначе FOREIGN KEY constraint failed.
    try:
        conn.execute('''
            DELETE FROM bonus_history WHERE booking_id IN (
                SELECT b.id FROM bookings b
                JOIN sessions s ON s.id = b.session_id
                WHERE s.film_id=?
            )
        ''', (film_id,))
        conn.execute('''
            DELETE FROM bookings WHERE session_id IN (
                SELECT id FROM sessions WHERE film_id=?
            )
        ''', (film_id,))
        conn.execute('DELETE FROM sessions WHERE film_id=?', (film_id,))
        conn.execute('DELETE FROM reviews WHERE film_id=?', (film_id,))
        conn.execute('DELETE FROM favorites WHERE film_id=?', (film_id,))
        conn.execute('DELETE FROM films WHERE id=?', (film_id,))
        conn.commit()
        log_audit(conn, current_user(), 'film_delete', f'Удалён фильм id={film_id}')
        flash('Фильм удалён вместе с сеансами и бронями.', 'success')
    except sqlite3.Error as e:
        conn.rollback()
        logger.exception('Не удалось удалить фильм id=%s', film_id)
        flash('Не удалось удалить фильм: внутренняя ошибка. Подробности в журнале.', 'danger')
    finally:
        conn.close()
    return redirect(url_for('personal') + '?tab=films')


@admin_bp.route('/admin/films/inline-edit/<int:film_id>', methods=['POST'])
@admin_required
def admin_film_inline_edit(film_id):
    title = request.form.get('title', '').strip()
    genre = request.form.get('genre', '').strip()
    duration = request.form.get('duration', '').strip()
    if not title or not duration:
        flash('Заполните название и длительность', 'danger')
        return redirect(url_for('personal') + '?tab=films')
    # A10-12 аудита: int('abc') раньше давал ValueError → 500. Проверяем
    # строку на цифры до преобразования.
    if not duration.isdigit():
        flash('Длительность должна быть целым числом минут', 'danger')
        return redirect(url_for('personal') + '?tab=films')
    duration_int = int(duration)
    if duration_int < 1 or duration_int > 600:
        flash('Длительность должна быть от 1 до 600 минут', 'danger')
        return redirect(url_for('personal') + '?tab=films')
    conn = get_db()
    conn.execute('UPDATE films SET title=?, genre=?, duration=? WHERE id=?',
                 (title, genre, duration_int, film_id))
    conn.commit()
    conn.close()
    return redirect(url_for('personal') + '?tab=films')


# ═══════════════════════════════════════════════════════════════
# Залы
# ═══════════════════════════════════════════════════════════════


@admin_bp.route('/admin/halls')
@admin_required
def admin_halls():
    conn = get_db()
    halls = conn.execute('SELECT * FROM halls').fetchall()
    conn.close()
    return render_template('admin/halls.html', halls=halls, user=current_user())


@admin_bp.route('/admin/halls/add', methods=['POST'])
@admin_required
def admin_hall_add():
    name = request.form.get('name', '').strip()
    rows = request.form.get('rows', '').strip()
    seats_per_row = request.form.get('seats_per_row', '').strip()
    format = request.form.get('format', '2D')

    if not name or not rows or not seats_per_row:
        return redirect(url_for('personal') + '?tab=halls')

    # A10-3 аудита: раньше int('abc') давал ValueError → 500. Теперь —
    # дружелюбное сообщение и редирект.
    if not rows.isdigit() or not seats_per_row.isdigit():
        flash('Ряды и места в ряду должны быть целыми числами', 'danger')
        return redirect(url_for('personal') + '?tab=halls')
    rows = int(rows)
    seats_per_row = int(seats_per_row)
    if rows < 1 or rows > 50 or seats_per_row < 1 or seats_per_row > 50:
        flash('Недопустимые значения: ряды и места в ряду от 1 до 50', 'danger')
        return redirect(url_for('personal') + '?tab=halls')
    capacity = rows * seats_per_row

    conn = get_db()
    conn.execute('INSERT INTO halls (name, capacity, rows, seats_per_row, format) VALUES (?,?,?,?,?)',
                 (name, capacity, rows, seats_per_row, format))
    hall_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    for row in range(1, rows + 1):
        for seat in range(1, seats_per_row + 1):
            conn.execute('INSERT INTO seats (hall_id, row_num, seat_num) VALUES (?,?,?)',
                         (hall_id, row, seat))
    conn.commit()
    conn.close()
    return redirect(url_for('personal') + '?tab=halls')


@admin_bp.route('/admin/halls/delete/<int:hall_id>', methods=['POST'])
@admin_required
def admin_hall_delete(hall_id):
    conn = get_db()
    # Удаляем в правильном порядке из-за внешних ключей: сначала самые
    # "вложенные" строки, потом зал. Иначе SQLite даёт
    # FOREIGN KEY constraint failed (bookings -> sessions -> halls).
    try:
        # Бронь идёт на сеансы зала и/или места зала.
        conn.execute('''
            DELETE FROM bonus_history WHERE booking_id IN (
                SELECT b.id FROM bookings b
                LEFT JOIN sessions s ON s.id = b.session_id
                LEFT JOIN seats se ON se.id = b.seat_id
                WHERE s.hall_id=? OR se.hall_id=?
            )
        ''', (hall_id, hall_id))
        conn.execute('''
            DELETE FROM bookings WHERE session_id IN (
                SELECT id FROM sessions WHERE hall_id=?
            ) OR seat_id IN (
                SELECT id FROM seats WHERE hall_id=?
            )
        ''', (hall_id, hall_id))
        conn.execute('DELETE FROM sessions WHERE hall_id=?', (hall_id,))
        conn.execute('DELETE FROM seats WHERE hall_id=?', (hall_id,))
        conn.execute('DELETE FROM halls WHERE id=?', (hall_id,))
        conn.commit()
        log_audit(conn, current_user(), 'hall_delete', f'Удалён зал id={hall_id}')
        flash('Зал удалён вместе с сеансами и бронями.', 'success')
    except sqlite3.Error as e:
        conn.rollback()
        logger.exception('Не удалось удалить зал id=%s', hall_id)
        flash('Не удалось удалить зал: внутренняя ошибка. Подробности в журнале.', 'danger')
    finally:
        conn.close()
    return redirect(url_for('personal') + '?tab=halls')


@admin_bp.route('/admin/halls/edit/<int:hall_id>', methods=['POST'])
@admin_required
def admin_hall_edit(hall_id):
    name = request.form.get('name', '').strip()
    status = request.form.get('status', 'open')
    capacity = request.form.get('capacity', '').strip()
    format = request.form.get('format', '2D')
    if not name or not capacity:
        return redirect(url_for('personal') + '?tab=halls')

    # A10-4 аудита: capacity=abc раньше давало ValueError → 500.
    if not capacity.isdigit() or int(capacity) < 1:
        flash('Вместимость должна быть целым числом больше нуля', 'danger')
        return redirect(url_for('personal') + '?tab=halls')
    capacity = int(capacity)
    if capacity > 5000:
        flash('Вместимость не может превышать 5000 мест', 'danger')
        return redirect(url_for('personal') + '?tab=halls')
    conn = get_db()  # СНАЧАЛА открываем соединение

    old = conn.execute('SELECT capacity, rows, seats_per_row, status FROM halls WHERE id=?', (hall_id,)).fetchone()

    if status == 'closed' and old['status'] != 'closed':
        today = date.today().isoformat()
        now_time = datetime.now().strftime('%H:%M')
        upcoming_sessions = conn.execute('''
                SELECT id FROM sessions
                WHERE hall_id=? AND (date > ? OR (date = ? AND time > ?))
            ''', (hall_id, today, today, now_time)).fetchall()
        for s in upcoming_sessions:
            conn.execute("UPDATE bookings SET status='cancelled' WHERE session_id=? AND status != 'cancelled'",
                         (s['id'],))
            conn.execute('DELETE FROM sessions WHERE id=?', (s['id'],))

    if old['capacity'] != capacity:
        # ВНИМАНИЕ (A06-5 аудита): пересоздание мест удаляло ВСЕ брони зала,
        # включая оплаченные билеты, без уведомления и возврата. Теперь
        # меняем вместимость только если активных броней (booked/paid) нет —
        # иначе тихо теряются деньги клиента.
        active = conn.execute('''
            SELECT COUNT(*) as c FROM bookings b
            JOIN seats s ON s.id = b.seat_id
            WHERE s.hall_id=? AND b.status IN ('booked', 'paid')
        ''', (hall_id,)).fetchone()['c']
        if active:
            conn.close()
            flash(f'Нельзя изменить вместимость: в зале {active} активных бронеров. '
                  'Сначала отмените или продайте их брони, либо закройте зал.', 'warning')
            return redirect(url_for('personal') + '?tab=halls')

        import math
        rows = math.ceil(math.sqrt(capacity * 2 / 3))
        seats_per_row = math.ceil(capacity / rows)

        conn.execute('DELETE FROM bookings WHERE seat_id IN (SELECT id FROM seats WHERE hall_id=?)', (hall_id,))
        conn.execute('DELETE FROM seats WHERE hall_id=?', (hall_id,))

        seats_created = 0
        for row in range(1, rows + 1):
            seats_in_row = seats_per_row if seats_created + seats_per_row <= capacity else capacity - seats_created
            for seat in range(1, seats_in_row + 1):
                conn.execute('INSERT INTO seats (hall_id, row_num, seat_num) VALUES (?,?,?)',
                             (hall_id, row, seat))
            seats_created += seats_in_row

        conn.execute('UPDATE halls SET name=?, status=?, capacity=?, format=?, rows=?, seats_per_row=? WHERE id=?',
                     (name, status, capacity, format, rows, seats_per_row, hall_id))
    else:
        conn.execute('UPDATE halls SET name=?, status=?, format=? WHERE id=?',
                     (name, status, format, hall_id))

    conn.commit()
    conn.close()
    return redirect(url_for('personal') + '?tab=halls')


# ═══════════════════════════════════════════════════════════════
# Сеансы
# ═══════════════════════════════════════════════════════════════


@admin_bp.route('/admin/sessions')
@admin_required
def admin_sessions():
    conn = get_db()
    sessions = conn.execute('''
        SELECT s.*, f.title, h.name as hall_name
        FROM sessions s
        JOIN films f ON s.film_id = f.id
        JOIN halls h ON s.hall_id = h.id
        ORDER BY s.date, s.time
    ''').fetchall()
    films = conn.execute('SELECT * FROM films ORDER BY title').fetchall()
    halls = conn.execute("SELECT * FROM halls WHERE status != 'closed' ORDER BY name").fetchall()
    conn.close()
    return render_template('admin/sessions.html',
                           sessions=sessions, films=films, halls=halls, user=current_user())


@admin_bp.route('/admin/sessions/add', methods=['POST'])
@admin_required
def admin_session_add():
    film_id = request.form.get('film_id')
    hall_id = request.form.get('hall_id')
    date = request.form.get('date')
    time = request.form.get('time')
    price = request.form.get('price')

    if not all([film_id, hall_id, date, time, price]) or not str(price).isdigit():
        flash('Заполните все поля; цена — целое число', 'danger')
        return redirect(url_for('personal') + '?tab=sessions')

    conn = get_db()
    hall = conn.execute('SELECT status FROM halls WHERE id=?', (hall_id,)).fetchone()
    if not hall or hall['status'] == 'closed':
        conn.close()
        return redirect(url_for('personal') + '?tab=sessions')
    film = conn.execute('SELECT id FROM films WHERE id=?', (film_id,)).fetchone()
    if not film:
        conn.close()
        flash('Фильм не найден', 'danger')
        return redirect(url_for('personal') + '?tab=sessions')

    conn.execute('INSERT INTO sessions (film_id, hall_id, date, time, price) VALUES (?,?,?,?,?)',
                 (film_id, hall_id, date, time, int(price)))
    conn.commit()
    conn.close()
    return redirect(url_for('personal') + '?tab=sessions')


@admin_bp.route('/admin/sessions/delete/<int:session_id>', methods=['POST'])
@admin_required
def admin_session_delete(session_id):
    conn = get_db()
    # Сеанс удаляем вместе с бронями на него: брони ссылаются на сеанс внешним
    # ключом, а история бонусов — на брони.
    try:
        conn.execute('DELETE FROM bonus_history WHERE booking_id IN '
                     '(SELECT id FROM bookings WHERE session_id=?)', (session_id,))
        conn.execute('DELETE FROM bookings WHERE session_id=?', (session_id,))
        conn.execute('DELETE FROM sessions WHERE id=?', (session_id,))
        conn.commit()
        log_audit(conn, current_user(), 'session_delete', f'Удалён сеанс id={session_id}')
        flash('Сеанс удалён вместе с бронями.', 'success')
    except sqlite3.Error as e:
        conn.rollback()
        logger.exception('Не удалось удалить сеанс id=%s', session_id)
        flash('Не удалось удалить сеанс: внутренняя ошибка. Подробности в журнале.', 'danger')
    finally:
        conn.close()
    return redirect(url_for('personal') + '?tab=sessions')


@admin_bp.route('/admin/sessions/edit/<int:session_id>', methods=['POST'])
@admin_required
def admin_session_edit(session_id):
    film_id = request.form.get('film_id')
    hall_id = request.form.get('hall_id')
    date = request.form.get('date')
    time = request.form.get('time')
    price = request.form.get('price')

    # A10-13 аудита: раньше admin_session_edit принимал любые значения —
    # 'abc' в price, пустые film_id/hall_id, произвольные date/time. SQLite
    # приводил price к 0, а пустой film_id ломал FK. Проверяем всё то же,
    # что и в admin_session_add.
    if not all([film_id, hall_id, date, time, price]) or not str(price).isdigit():
        flash('Заполните все поля; цена — целое число', 'danger')
        return redirect(url_for('personal') + '?tab=sessions')

    # film_id/hall_id должны быть числами и существовать
    if not str(film_id).isdigit() or not str(hall_id).isdigit():
        flash('Некорректные фильм или зал', 'danger')
        return redirect(url_for('personal') + '?tab=sessions')

    # Простая валидация формата даты и времени — чтобы в TEXT не попал мусор
    try:
        from datetime import datetime as _dt
        _dt.strptime(date, '%Y-%m-%d')
        _dt.strptime(time, '%H:%M')
    except (ValueError, TypeError):
        flash('Некорректная дата или время', 'danger')
        return redirect(url_for('personal') + '?tab=sessions')

    conn = get_db()
    film = conn.execute('SELECT id FROM films WHERE id=?', (film_id,)).fetchone()
    hall = conn.execute('SELECT id FROM halls WHERE id=?', (hall_id,)).fetchone()
    if not film or not hall:
        conn.close()
        flash('Фильм или зал не найдены', 'danger')
        return redirect(url_for('personal') + '?tab=sessions')

    conn.execute('''
        UPDATE sessions SET film_id=?, hall_id=?, date=?, time=?, price=?
        WHERE id=?
    ''', (film_id, hall_id, date, time, int(price), session_id))
    conn.commit()
    conn.close()
    return redirect(url_for('personal') + '?tab=sessions')


# ═══════════════════════════════════════════════════════════════
# Бронирования
# ═══════════════════════════════════════════════════════════════


@admin_bp.route('/admin/bookings')
@admin_required
def admin_bookings():
    conn = get_db()
    filter_date = request.args.get('date', '')
    filter_film = request.args.get('film_id', '')
    query = '''
        SELECT b.*, u.name as user_name, u.email, f.title, s.date, s.time,
               h.name as hall_name, se.row_num, se.seat_num, s.price
        FROM bookings b
        JOIN users u ON b.user_id = u.id
        JOIN sessions s ON b.session_id = s.id
        JOIN films f ON s.film_id = f.id
        JOIN halls h ON s.hall_id = h.id
        JOIN seats se ON b.seat_id = se.id
        WHERE 1=1
    '''
    params = []
    if filter_date:
        query += ' AND s.date=?'
        params.append(filter_date)
    if filter_film:
        query += ' AND s.film_id=?'
        params.append(filter_film)
    query += ' ORDER BY s.date DESC, s.time DESC'
    bookings = conn.execute(query, params).fetchall()
    films = conn.execute('SELECT * FROM films').fetchall()
    conn.close()
    return render_template('admin/bookings.html', bookings=bookings, films=films,
                           filter_date=filter_date, filter_film=filter_film, user=current_user())


# ═══════════════════════════════════════════════════════════════
# Пользователи
# ═══════════════════════════════════════════════════════════════


@admin_bp.route('/admin/users')
@admin_required
def admin_users():
    conn = get_db()
    users = conn.execute('SELECT id, name, email, phone, role, is_banned FROM users ORDER BY id').fetchall()
    conn.close()
    return render_template('admin/users.html', users=users, user=current_user())


@admin_bp.route('/admin/users/ban/<int:user_id>', methods=['POST'])
@admin_required
def admin_user_ban(user_id):
    user = current_user()
    if user['id'] == user_id:
        return redirect(url_for('personal') + '?tab=users')
    conn = get_db()
    # Админ не может банить/разбанить других администраторов
    target = conn.execute('SELECT id, role, is_banned, email FROM users WHERE id=?', (user_id,)).fetchone()
    if not target:
        conn.close()
        return redirect(url_for('personal') + '?tab=users')
    if target['role'] == 'admin' and user['role'] == 'admin':
        conn.close()
        flash('Администратор не может менять статус другого администратора.', 'warning')
        return redirect(url_for('personal') + '?tab=users')
    new_status = 0 if target['is_banned'] else 1
    conn.execute('UPDATE users SET is_banned=? WHERE id=?', (new_status, user_id))
    log_audit(conn, current_user(), 'user_ban' if new_status else 'user_unban',
              f'Пользователь {target["email"]} (id {user_id})')
    conn.commit()
    conn.close()
    return redirect(url_for('personal') + '?tab=users')


def _count_admins(conn):
    """Сколько в системе администраторов (включая баннутых — они уже не входят)."""
    return conn.execute("SELECT COUNT(*) as c FROM users WHERE role='admin' AND is_banned=0").fetchone()['c']


@admin_bp.route('/admin/users/role/<int:user_id>', methods=['POST'])
@admin_required
def admin_user_role(user_id):
    user = current_user()
    if user['id'] == user_id:
        return redirect(url_for('personal') + '?tab=users')
    new_role = request.form.get('role')
    if new_role not in ('client', 'admin', 'cashier'):
        return redirect(url_for('personal') + '?tab=users')
    conn = get_db()
    # Админ не может менять роль другого администратора (в т.ч. будущего админа)
    target = conn.execute('SELECT id, role, email FROM users WHERE id=?', (user_id,)).fetchone()
    if not target:
        conn.close()
        return redirect(url_for('personal') + '?tab=users')
    if target['role'] == 'admin':
        conn.close()
        flash('Администратор не может менять роль другого администратора.', 'danger')
        return redirect(url_for('personal') + '?tab=users')
    # A01-4 аудита: нельзя остаться вообще без администраторов.
    # Понижая последнего действующего админа (или переводя обычного
    # пользователя в админа — ему уже можно), система теряет админку
    # навсегда (seed_db не создаст нового, т.к. users не пуст).
    if user['role'] == 'admin' and _count_admins(conn) <= 1:
        conn.close()
        flash('Нельзя изменить роль: в системе должен остаться хотя бы один действующий администратор.', 'danger')
        return redirect(url_for('personal') + '?tab=users')
    conn.execute('UPDATE users SET role=? WHERE id=?', (new_role, user_id))
    log_audit(conn, current_user(), 'role_change',
              f'Роль {target["email"]}: {target["role"]} → {new_role}')
    conn.commit()
    conn.close()
    return redirect(url_for('personal') + '?tab=users')


# короткие endpoint'ы для url_for в шаблонах (см. register_blueprints в main.py)
ALL_ADMIN_ENDPOINTS = [
    ('admin_audit', admin_audit),
    ('admin', admin),
    ('admin_promotion_add', admin_promotion_add),
    ('admin_promotion_delete', admin_promotion_delete),
    ('admin_promotion_image', admin_promotion_image),
    ('admin_promotion_desc', admin_promotion_desc),
    ('admin_promotion_edit', admin_promotion_edit),
    ('admin_films', admin_films),
    ('admin_film_poster', admin_film_poster),
    ('admin_film_desc', admin_film_desc),
    ('admin_film_add', admin_film_add),
    ('admin_film_edit', admin_film_edit),
    ('admin_film_delete', admin_film_delete),
    ('admin_film_inline_edit', admin_film_inline_edit),
    ('admin_halls', admin_halls),
    ('admin_hall_add', admin_hall_add),
    ('admin_hall_delete', admin_hall_delete),
    ('admin_hall_edit', admin_hall_edit),
    ('admin_sessions', admin_sessions),
    ('admin_session_add', admin_session_add),
    ('admin_session_delete', admin_session_delete),
    ('admin_session_edit', admin_session_edit),
    ('admin_bookings', admin_bookings),
    ('admin_users', admin_users),
    ('admin_user_ban', admin_user_ban),
    ('admin_user_role', admin_user_role),
]