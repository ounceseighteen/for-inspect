from datetime import datetime
from flask import session
from db import get_db, hash_password


def current_user():
    if 'user_id' in session:
        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
        conn.close()
        return user
    return None


def is_valid_email(email):
    # Регулярное выражение: проверяет структуру логин@домен.зона
    # Не пропускает адреса без точки, без собаки или с запрещенными символами
    import re
    email_regex = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
    return re.match(email_regex, email) is not None


def get_loyalty_level(total_spent):
    total_spent = float(total_spent or 0)
    if total_spent >= 30000:
        return 'Platinum'
    if total_spent >= 15000:
        return 'Gold'
    if total_spent >= 5000:
        return 'Silver'
    return 'Basic'


def get_loyalty_cashback(level):
    return {
        'Basic': 3,
        'Silver': 5,
        'Gold': 10,
        'Platinum': 15
    }.get(level or 'Basic', 3)


def get_next_loyalty_level_info(total_spent):
    total_spent = float(total_spent or 0)
    if total_spent < 5000:
        return {'name': 'Silver', 'left': int(5000 - total_spent)}
    if total_spent < 15000:
        return {'name': 'Gold', 'left': int(15000 - total_spent)}
    if total_spent < 30000:
        return {'name': 'Platinum', 'left': int(30000 - total_spent)}
    return None


def generate_loyalty_card_number(conn):
    import secrets
    while True:
        card_number = 'MK-' + ''.join(str(secrets.randbelow(10)) for _ in range(12))
        existing = conn.execute('SELECT id FROM loyalty_cards WHERE card_number=?', (card_number,)).fetchone()
        if not existing:
            return card_number


def create_loyalty_card(conn, user_id):
    existing = conn.execute('SELECT * FROM loyalty_cards WHERE user_id=?', (user_id,)).fetchone()
    if existing:
        return existing
    card_number = generate_loyalty_card_number(conn)
    conn.execute('''
        INSERT INTO loyalty_cards (user_id, card_number, bonus_balance, total_spent, level, created_at)
        VALUES (?, ?, 0, 0, 'Basic', ?)
    ''', (user_id, card_number, datetime.now().isoformat()))
    conn.commit()
    return conn.execute('SELECT * FROM loyalty_cards WHERE user_id=?', (user_id,)).fetchone()


def add_loyalty_history(conn, user_id, amount, operation, balance_after, booking_id=None):
    conn.execute('''
        INSERT INTO bonus_history (user_id, booking_id, amount, operation, balance_after, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, booking_id, int(amount), operation, int(balance_after), datetime.now().isoformat()))


def spend_bonuses_for_booking(conn, user_id, booking_id, amount):
    card = conn.execute('SELECT * FROM loyalty_cards WHERE user_id=?', (user_id,)).fetchone()
    if not card:
        return False, 'У пользователя нет бонусной карты'

    amount = int(round(float(amount or 0)))
    if amount <= 0:
        return True, None

    current_balance = int(card['bonus_balance'] or 0)
    if current_balance < amount:
        return False, 'Недостаточно бонусов на карте'

    new_balance = current_balance - amount
    conn.execute('UPDATE loyalty_cards SET bonus_balance=? WHERE user_id=?', (new_balance, user_id))
    add_loyalty_history(conn, user_id, -amount, 'Оплата билета бонусной картой', new_balance, booking_id)
    return True, None


def award_bonus_for_booking(conn, booking_id):
    booking = conn.execute('''
        SELECT b.id, b.user_id, b.status, b.final_price, b.payment_method, s.price as session_price
        FROM bookings b
        JOIN sessions s ON s.id = b.session_id
        WHERE b.id=?
    ''', (booking_id,)).fetchone()

    if not booking or booking['status'] != 'paid':
        return

    # Кешбэк начисляется только при оплате бонусной картой.
    # Обычная оплата банковской картой не влияет на бонусную карту.
    if booking['payment_method'] != 'bonus':
        return

    card = conn.execute('SELECT * FROM loyalty_cards WHERE user_id=?', (booking['user_id'],)).fetchone()
    if not card:
        return

    already = conn.execute('''
        SELECT id FROM bonus_history
        WHERE booking_id=? AND operation='Кешбэк за билет'
    ''', (booking_id,)).fetchone()
    if already:
        return

    final_price = float(booking['final_price'] or booking['session_price'] or 0)
    cashback = get_loyalty_cashback(card['level'])
    bonus_amount = int(final_price * cashback / 100)
    if bonus_amount <= 0:
        return

    new_total_spent = float(card['total_spent'] or 0) + final_price
    new_level = get_loyalty_level(new_total_spent)
    new_balance = int(card['bonus_balance'] or 0) + bonus_amount

    old_level = card['level']
    conn.execute('''
        UPDATE loyalty_cards
        SET bonus_balance=?, total_spent=?, level=?
        WHERE user_id=?
    ''', (new_balance, new_total_spent, new_level, booking['user_id']))

    add_loyalty_history(conn, booking['user_id'], bonus_amount, 'Кешбэк за билет', new_balance, booking_id)

    # Уведомление о повышении уровня лояльности
    if new_level != old_level:
        add_notification(
            conn, booking['user_id'], 'loyalty',
            f'Поздравляем, ваш уровень — {new_level}!',
            f'Кешбэк увеличен до {get_loyalty_cashback(new_level)}%. Продолжайте в том же духе!',
            '/loyalty'
        )


def fmt_date_ru(iso_date):
    """'2026-10-02' -> '02.10.2026'."""
    try:
        return datetime.strptime(iso_date, '%Y-%m-%d').strftime('%d.%m.%Y')
    except Exception:
        return iso_date or ''


def get_favorite_ids(conn, user_id):
    """Множество id фильмов в избранном клиента (для звёздочек на афише)."""
    if not user_id:
        return set()
    return {r['film_id'] for r in
            conn.execute('SELECT film_id FROM favorites WHERE user_id=?', (user_id,)).fetchall()}


def add_notification(conn, user_id, ntype, title, body='', link='', ref_type=None, ref_id=None, payload_extra=None):
    """Добавляет строку уведомления для пользователя."""
    try:
        if payload_extra:
            # Мягкая колонка payload (JSON-строка) — для минут до сеанса и т.п.
            # Если колонки ещё нет в старой БД — игнорируем доп. данные.
            cols = [r['name'] for r in conn.execute('PRAGMA table_info(notifications)').fetchall()]
            if 'payload' in cols:
                conn.execute('''
                    INSERT INTO notifications (user_id, type, title, body, link, ref_type, ref_id, created_at, read, payload)
                    VALUES (?,?,?,?,?,?,?,?,0,?)
                ''', (user_id, ntype, title, body, link, ref_type, ref_id, datetime.now().isoformat(), payload_extra))
                conn.commit()
                return
        conn.execute('''
            INSERT INTO notifications (user_id, type, title, body, link, ref_type, ref_id, created_at, read)
            VALUES (?,?,?,?,?,?,?,?,0)
        ''', (user_id, ntype, title, body, link, ref_type, ref_id, datetime.now().isoformat()))
        conn.commit()
    except Exception as e:
        import logging
        logging.getLogger(__name__).error('Ошибка создания уведомления: %s', e)


def generate_session_reminders(conn, user_id):
    """
    Генерирует напоминания о скором сеансе: для каждого активного билета
    пользователя (booked/paid), который начнётся в ближайшие 24 часа и о котором
    ещё не было создано напоминание. Без дублей благодаря ref_type='session',
    ref_id=session_id. minutes_until нужен для тоста «скоро сеанс» в JS
    (см. base.js): раньше поле не отдавалось, и тост показывался никогда.
    """
    from datetime import date, timedelta
    now_date = date.today().isoformat()
    now_time = datetime.now().strftime('%H:%M')
    cutoff_date = (datetime.now() + timedelta(hours=24)).date().isoformat()
    cutoff_time = (datetime.now() + timedelta(hours=24)).strftime('%H:%M')

    upcoming = conn.execute('''
        SELECT DISTINCT b.session_id, f.title, s.date, s.time, h.name as hall_name
        FROM bookings b
        JOIN sessions s ON s.id = b.session_id
        JOIN films f ON f.id = s.film_id
        JOIN halls h ON h.id = s.hall_id
        WHERE b.user_id=?
          AND b.status IN ('booked', 'paid')
          AND (s.date > ? OR (s.date = ? AND s.time > ?))
          AND (s.date < ? OR (s.date = ? AND s.time <= ?))
        ORDER BY s.date, s.time
    ''', (user_id, now_date, now_date, now_time, cutoff_date, cutoff_date, cutoff_time)).fetchall()

    for row in upcoming:
        already = conn.execute('''
            SELECT id FROM notifications
            WHERE user_id=? AND ref_type='session' AND ref_id=?
        ''', (user_id, row['session_id'])).fetchone()
        if already:
            continue
        date_text = fmt_date_ru(row['date'])
        session_text = f'{date_text} в {row["time"]}'
        paid = conn.execute('''
            SELECT 1 FROM bookings WHERE session_id=? AND user_id=? AND status='paid' LIMIT 1
        ''', (row['session_id'], user_id)).fetchone()
        if paid:
            body = (f'{session_text} будет сеанс «{row["title"]}». Электронные билеты '
                    f'можете увидеть у себя на почте или в личном кабинете')
        else:
            body = (f'{session_text} будет сеанс «{row["title"]}». '
                    f'Не забудьте оплатить бронь в личном кабинете')
        # A09/A06-2 аудита: считаем минуты до сеанса и храним в JSON-поле
        # extra (добавляется в колонку payload): тост в JS ожидает
        # minutes_until, но сервер его раньше не отдавал.
        session_dt = datetime.strptime(f"{row['date']} {row['time']}", "%Y-%m-%d %H:%M")
        minutes_until = max(0, int((session_dt - datetime.now()).total_seconds() // 60))
        add_notification(
            conn, user_id, 'session_reminder',
            'Напоминание о сеансе',
            body,
            f'/session/{row["session_id"]}',
            'session', row['session_id'],
            payload_extra=f'{{"minutes_until": {minutes_until}}}'
        )


def get_film_rating(conn, film_id):
    """Средний рейтинг фильма и число отзывов."""
    row = conn.execute('''
        SELECT COUNT(*) as cnt, AVG(rating) as avg FROM reviews WHERE film_id=?
    ''', (film_id,)).fetchone()
    cnt = int(row['cnt'] or 0)
    avg = round(float(row['avg'] or 0), 1) if cnt else 0
    return avg, cnt


def notify_all_clients(conn, ntype, title, body, link):
    """Рассылает уведомление всем активным клиентам (не админам/кассирам)."""
    users = conn.execute("SELECT id FROM users WHERE role='client' AND is_banned=0").fetchall()
    for u in users:
        add_notification(conn, u['id'], ntype, title, body, link)


def create_verification_code(conn, purpose, email, payload=None):
    """
    Создаёт одноразовый 6-значный код. Сам код нигде не хранится в открытом
    виде (ни в БД, ни в cookie) — только его хэш. secrets.randbelow — криптографически
    стойкий генератор (в отличие от random, использовавшегося раньше).
    """
    import secrets
    conn.execute('DELETE FROM verification_codes WHERE purpose=? AND email=?', (purpose, email))
    code = f"{secrets.randbelow(900000) + 100000}"
    conn.execute('''
        INSERT INTO verification_codes (purpose, email, code_hash, payload, attempts, created_at, consumed)
        VALUES (?, ?, ?, ?, 0, ?, 0)
    ''', (purpose, email, hash_password(code), payload, datetime.now().isoformat()))
    conn.commit()
    return code


def check_verification_code(conn, purpose, email, input_code, max_age_seconds=300, max_attempts=5):
    """Возвращает (ok, message_если_ошибка, payload_если_успех)."""
    from werkzeug.security import check_password_hash
    row = conn.execute('''
        SELECT * FROM verification_codes
        WHERE purpose=? AND email=? AND consumed=0
        ORDER BY id DESC LIMIT 1
    ''', (purpose, email)).fetchone()

    if not row:
        return False, 'Код не найден. Запросите новый.', None

    created_at = datetime.fromisoformat(row['created_at'])
    if (datetime.now() - created_at).total_seconds() > max_age_seconds:
        conn.execute('UPDATE verification_codes SET consumed=1 WHERE id=?', (row['id'],))
        conn.commit()
        return False, 'Код истёк. Запросите новый.', None

    if row['attempts'] >= max_attempts:
        conn.execute('UPDATE verification_codes SET consumed=1 WHERE id=?', (row['id'],))
        conn.commit()
        return False, 'Слишком много неверных попыток. Запросите новый код.', None

    if not input_code or not check_password_hash(row['code_hash'], input_code):
        conn.execute('UPDATE verification_codes SET attempts=attempts+1 WHERE id=?', (row['id'],))
        conn.commit()
        return False, 'Неверный код', None

    conn.execute('UPDATE verification_codes SET consumed=1 WHERE id=?', (row['id'],))
    conn.commit()
    return True, None, row['payload']


def make_ticket_code(title, film_id, booking_id):
    """Код билета как в personal: первая буква фильма + id фильма + '-' + id бронирования."""
    first_letter = (title or 'Б')[0].upper()
    return f"{first_letter}{film_id}-{booking_id}"


def save_uploaded_image(file_storage, subfolder, prefix):
    """
    Безопасно сохраняет загруженную картинку (постер/акцию).
    Возвращает имя файла или (None, сообщение_об_ошибке).
    Раньше расширение бралось из имени файла без всякой проверки — можно
    было залить .html/.svg со скриптом и получить XSS прямо со своего домена
    (файлы отдаются как статика). Теперь: белый список расширений + проверка,
    что это действительно изображение (через Pillow), а не просто
    переименованный произвольный файл.
    """
    import os
    import uuid
    import io
    import logging
    from flask import current_app
    from werkzeug.utils import secure_filename
    from PIL import Image
    import config

    if not file_storage or not file_storage.filename:
        return None, None

    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in config.ALLOWED_IMAGE_EXTENSIONS:
        return None, 'Разрешены только изображения: JPG, PNG, WEBP'

    raw = file_storage.read()
    if len(raw) > 5 * 1024 * 1024:
        return None, 'Файл слишком большой (максимум 5 МБ)'

    try:
        img = Image.open(io.BytesIO(raw))
        img.verify()  # бросит исключение, если это не настоящее изображение
        # Пересохраняем картинку без метаданных: это обрезает EXIF и делает
        # невозможным полиглоты «картинка + скрипт» (A08-1 аудита) — на диск
        # попадает только чистое изображение.
        img2 = Image.open(io.BytesIO(raw))
        img2.load()
        buf = io.BytesIO()
        img2.save(buf, format=img2.format or 'PNG')
        raw = buf.getvalue()
    except Exception:
        logging.getLogger(__name__).exception('Ошибка обработки загруженного изображения')
        return None, 'Файл повреждён или не является изображением'

    filename = secure_filename(f"{prefix}_{uuid.uuid4().hex}{ext}")
    save_dir = os.path.join(current_app.root_path, 'static', subfolder)
    os.makedirs(save_dir, exist_ok=True)
    with open(os.path.join(save_dir, filename), 'wb') as out:
        out.write(raw)
    return filename, None