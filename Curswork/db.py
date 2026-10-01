import sqlite3
import logging
from datetime import datetime
from flask import request
import config
from werkzeug.security import generate_password_hash, check_password_hash

logger = logging.getLogger(__name__)


def get_db():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    # Ждать до 30 секунд, если другой запрос держит запись в БД,
    # вместо мгновенной ошибки "database is locked".
    conn.execute('PRAGMA busy_timeout = 30000')
    return conn


def log_audit(conn, user, action, details=''):
    """
    Одна строка в журнале действий. user — объект строки users или None.
    Устойчив на любых закрытых/поломанных соединениях: если вставка не
    удалась, открывает своё соединение и пишет повторно. Ошибку пишет в
    лог приложения (не глотает молча), чтобы отказ аудита был заметен.
    """
    ip = request.remote_addr or ''
    # Доверяем X-Forwarded-For только если явно включён TRUST_PROXY_HEADERS=1
    # (см. main.py — тогда ProxyFix уже переписал request.remote_addr, и
    # эта ветка вообще не нужна). Без прокси заголовок игнорируем, чтобы
    # клиент не мог подделать ip в журнале аудита.
    if os.environ.get('TRUST_PROXY_HEADERS') != '1' and request.headers.get('X-Forwarded-For'):
        # не доверяем, оставляем request.remote_addr
        pass
    scheme = request.scheme or ''
    row = (
        user['id'] if user else None,
        user['name'] if user else '—',
        action,
        (details or '')[:500],
        ip,
        f'{scheme}://{request.host}' if request.host else '',
        datetime.now().isoformat(),
    )
    try:
        conn.execute(
            'INSERT INTO audit_log (user_id, user_name, action, details, ip, url, created_at) '
            'VALUES (?,?,?,?,?,?,?)',
            row
        )
        conn.commit()
    except Exception:
        try:
            conn2 = get_db()
            conn2.execute(
                'INSERT INTO audit_log (user_id, user_name, action, details, ip, url, created_at) '
                'VALUES (?,?,?,?,?,?,?)',
                row
            )
            conn2.commit()
            conn2.close()
        except Exception as e2:
            logger.error('log_audit fallback failed (action=%s, user=%s): %s',
                         action, row[0], e2)


def hash_password(password):
    """Новые пароли всегда хэшируются scrypt'ом через werkzeug (явный метод)."""
    return generate_password_hash(password, method='scrypt')


def verify_password(stored_hash, password):
    """
    Проверяет пароль. Поддерживает только современный формат werkzeug
    (scrypt/pbkdf2:sha256). Легаси-хэши из прежней версии (сырой
    sha256-hex, ровно 64 hex-символа) больше не принимаются: в рабочей
    БД таких нет, а их проверка — это слабое место (радужные таблицы,
    одинаковые пароли дают одинаковые хэши). Если хэш выглядит как
    не-werkzeug — это не зарегистрированный в системе пароль.
    """
    if not stored_hash:
        return False
    # Werkzeug-хэши всегда имеют формат method$params$salt$hash,
    # т.е. в строке есть хотя бы один '$'. Сырой sha256-hex его не имеет.
    if '$' not in stored_hash:
        return False
    return check_password_hash(stored_hash, password)


def init_db():
    conn = get_db()
    c = conn.cursor()

    # WAL-режим ставим ОДИН раз при создании/инициализации БД, а не на каждом
    # новом соединении. Раньше PRAGMA journal_mode=WAL выполнялась в get_db()
    # на каждое соединение: на Windows это приводило к "database is locked"
    # при одновременных запросах (смена journal-режима берёт эксклюзивную
    # блокировку). В WAL один писатель + много читателей работают параллельно.
    conn.execute('PRAGMA journal_mode = WAL')

    c.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'client',
            is_banned INTEGER NOT NULL DEFAULT 0,
            phone TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS films (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            genre TEXT,
            duration INTEGER,
            poster TEXT
        );

        CREATE TABLE IF NOT EXISTS halls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            capacity INTEGER NOT NULL,
            rows INTEGER NOT NULL,
            seats_per_row INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            format TEXT NOT NULL DEFAULT '2D'
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            film_id INTEGER NOT NULL,
            hall_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            price REAL NOT NULL,
            FOREIGN KEY (film_id) REFERENCES films(id),
            FOREIGN KEY (hall_id) REFERENCES halls(id)
        );

        CREATE TABLE IF NOT EXISTS seats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hall_id INTEGER NOT NULL,
            row_num INTEGER NOT NULL,
            seat_num INTEGER NOT NULL,
            seat_type TEXT NOT NULL DEFAULT 'standard',
            FOREIGN KEY (hall_id) REFERENCES halls(id)
        );

        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            session_id INTEGER NOT NULL,
            seat_id INTEGER NOT NULL,
            booked_at TEXT,
            status TEXT NOT NULL DEFAULT 'booked',
            custom_code TEXT,
            promo_id INTEGER REFERENCES promotions(id),
            final_price REAL,
            payment_method TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (session_id) REFERENCES sessions(id),
            FOREIGN KEY (seat_id) REFERENCES seats(id)
        );

       CREATE TABLE IF NOT EXISTS promotions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            discount INTEGER DEFAULT 0,
            image TEXT
        );

        CREATE TABLE IF NOT EXISTS loyalty_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            card_number TEXT NOT NULL UNIQUE,
            bonus_balance INTEGER NOT NULL DEFAULT 0,
            total_spent REAL NOT NULL DEFAULT 0,
            level TEXT NOT NULL DEFAULT 'Basic',
            created_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS bonus_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            booking_id INTEGER,
            amount INTEGER NOT NULL,
            operation TEXT NOT NULL,
            balance_after INTEGER NOT NULL,
            created_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (booking_id) REFERENCES bookings(id)
        );
    ''')

    conn.commit()

    # Мягкая миграция для уже созданной БД: добавляем новые поля, если их нет
    booking_columns = [r['name'] for r in c.execute("PRAGMA table_info(bookings)").fetchall()]
    if 'payment_method' not in booking_columns:
        c.execute("ALTER TABLE bookings ADD COLUMN payment_method TEXT")

    # Защита от гонки при бронировании: два человека не смогут занять одно и то
    # же место на один сеанс одновременно, даже если оба прошли проверку в Python
    # в один момент времени. Уникальность только среди "живых" (не отменённых) броней.
    c.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_active_seat
        ON bookings (session_id, seat_id)
        WHERE status != 'cancelled'
    ''')

    # Хранилище кодов подтверждения (регистрация / восстановление / лояльность).
    # Коды больше не читаются напрямую из cookie: хранится только их хэш,
    # ограничено число попыток и есть время жизни.
    c.execute('''
        CREATE TABLE IF NOT EXISTS verification_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purpose TEXT NOT NULL,
            email TEXT NOT NULL,
            code_hash TEXT NOT NULL,
            payload TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            consumed INTEGER NOT NULL DEFAULT 0
        )
    ''')

    # Отзывы и рейтинг фильмов.
    c.execute('''
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            film_id INTEGER NOT NULL,
            rating INTEGER NOT NULL DEFAULT 5,
            comment TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (film_id) REFERENCES films(id)
        )
    ''')

    # Избранное «Хочу посмотреть».
    c.execute('''
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            film_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, film_id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (film_id) REFERENCES films(id)
        )
    ''')

    # Уведомления пользователей (сеанс скоро, бронь, оплата, уровень лояльности).
    c.execute('''
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL DEFAULT 'info',
            title TEXT NOT NULL,
            body TEXT,
            link TEXT,
            ref_type TEXT,
            ref_id INTEGER,
            created_at TEXT NOT NULL,
            read INTEGER NOT NULL DEFAULT 0,
            payload TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')

    # Счётчик неудачных попыток входа (антибрутфорс по аккаунту).
    c.execute('''
        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            attempted_at TEXT NOT NULL
        )
    ''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_login_attempts_email ON login_attempts (email)')

    # Журнал действий для аудита (кто и когда что сделал).
    c.execute('''
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            user_name TEXT,
            action TEXT NOT NULL,
            details TEXT,
            ip TEXT,
            url TEXT,
            created_at TEXT NOT NULL
        )
    ''')

    # Мягкие миграции: добавляем колонки, которых нет в старой БД.
    user_columns = [r['name'] for r in c.execute("PRAGMA table_info(users)").fetchall()]
    if 'phone' not in user_columns:
        c.execute("ALTER TABLE users ADD COLUMN phone TEXT DEFAULT ''")
    if 'password_changed_at' not in user_columns:
        c.execute("ALTER TABLE users ADD COLUMN password_changed_at TEXT")
    notif_columns = [r['name'] for r in c.execute("PRAGMA table_info(notifications)").fetchall()]
    if 'payload' not in notif_columns:
        c.execute("ALTER TABLE notifications ADD COLUMN payload TEXT")
    seat_columns = [r['name'] for r in c.execute("PRAGMA table_info(seats)").fetchall()]
    if 'seat_type' not in seat_columns:
        c.execute("ALTER TABLE seats ADD COLUMN seat_type TEXT NOT NULL DEFAULT 'standard'")
    audit_columns = [r['name'] for r in c.execute("PRAGMA table_info(audit_log)").fetchall()]
    if 'url' not in audit_columns:
        c.execute("ALTER TABLE audit_log ADD COLUMN url TEXT")

    conn.commit()
    seed_db(conn)
    conn.close()


def seed_db(conn):
    c = conn.cursor()

    # Проверяем — уже заполнено?
    if c.execute('SELECT COUNT(*) FROM users').fetchone()[0] > 0:
        return

    # Администратор. Логин/пароль задаются в .env — никаких дефолтов в коде.
    # require_strong_env дополнительно требует пароль не короче 8 символов
    # (A02-1 аудита: слабый ADMIN_PASSWORD раньше принимался молча).
    admin_email = config.require_env('ADMIN_EMAIL')
    admin_password = config.require_strong_env('ADMIN_PASSWORD')
    admin_user = (
        'Администратор',
        admin_email,
        hash_password(admin_password),
        'admin'
    )

    c.execute(
        'INSERT INTO users (name, email, password, role) VALUES (?,?,?,?)',
        admin_user
    )

    # ── Тестовые данные (только для разработки/презентации). ──
    # Включаются переменной SEED_DEMO_DATA=1 в .env. Никаких тестовых
    # аккаунтов в чистом проде: без этой переменной создаётся только админ.
    if os.environ.get('SEED_DEMO_DATA') == '1':
        # A02-7 аудита: раньше здесь был дефолтный пароль 'DemoPass123!',
        # зашитый в код. Если кто-то включит SEED_DEMO_DATA=1 в проде — все
        # демо-аккаунты (включая cashier@demo.ru) станут публично доступны.
        # Теперь DEMO_PASSWORD обязателен и проверяется на длину.
        if config.DEBUG is False:
            raise RuntimeError(
                'SEED_DEMO_DATA=1 запрещён при DEBUG=0: демо-аккаунты не должны '
                'появляться в проде. Уберите SEED_DEMO_DATA из .env.'
            )
        demo_password = os.environ.get('DEMO_PASSWORD')
        if not demo_password or len(demo_password) < 8:
            raise RuntimeError(
                'SEED_DEMO_DATA=1 требует DEMO_PASSWORD не короче 8 символов в .env. '
                'Задайте свой пароль — дефолтных в коде больше нет.'
            )

        def _add_user(name, email, role, phone=''):
            c.execute(
                'INSERT INTO users (name, email, password, role, phone) VALUES (?,?,?,?,?)',
                (name, email, hash_password(demo_password), role, phone)
            )
            return c.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()['id']

        cashier_id = _add_user('Кассир Мария', 'cashier@demo.ru', 'cashier', '+7 900 111-22-33')
        u_anna = _add_user('Анна Смирнова', 'anna@demo.ru', 'client', '+7 900 111-22-34')
        u_ivan = _add_user('Иван Петров', 'ivan@demo.ru', 'client', '+7 900 111-22-35')
        u_olga = _add_user('Ольга Кузнецова', 'olga@demo.ru', 'client', '+7 900 111-22-36')
        u_dmitry = _add_user('Дмитрий Соколов', 'dmitry@demo.ru', 'client', '+7 900 111-22-37')
        u_elena = _add_user('Елена Морозова', 'elena@demo.ru', 'client', '+7 900 111-22-38')

        from datetime import timedelta as _td

        # ── 2 зала (большой 2D и малый VIP) ──
        def _add_hall(name, format, rows, seats_per_row):
            capacity = rows * seats_per_row
            c.execute('INSERT INTO halls (name, capacity, rows, seats_per_row, format) VALUES (?,?,?,?,?)',
                      (name, capacity, rows, seats_per_row, format))
            hall_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
            for r in range(1, rows + 1):
                for s in range(1, seats_per_row + 1):
                    # Последний ряд большого зала — "VIP" (мягкие кресла с подлокотниками)
                    seat_type = 'vip' if (format == 'IMAX' or (name.startswith('Большой') and r == rows)) else 'standard'
                    c.execute('INSERT INTO seats (hall_id, row_num, seat_num, seat_type) VALUES (?,?,?,?)',
                              (hall_id, r, s, seat_type))
            return hall_id

        hall_big_id = _add_hall('Большой зал', '2D', 8, 12)   # 96 мест
        hall_vip_id = _add_hall('VIP-зал', 'IMAX', 4, 8)      # 32 места

        # ── Фильмы ──
        films = [
            ('Дюна: Часть третья', 'Продолжение эпической саги об Арракисе и борьбе за специи.',
             'Фантастика', 172, ''),
            ('Побег из Шоушенка: переиздание', 'Легендарная история надежды и дружбы — снова в кинотеатрах.',
             'Драма', 142, ''),
            ('Кот в сапогах 3', 'Новые приключения отважного кота и его друзей.',
             'Мультфильмы', 96, ''),
            ('Тайна старой усадьбы', 'Мистический детектив о загадочных событиях в старинном особняке.',
             'Триллер', 118, ''),
            ('Гонка за мечтой', 'Молодой гонщик мечтает попасть в большую лигу.',
             'Спорт', 121, ''),
        ]
        film_ids = []
        for title, desc, genre, dur, poster in films:
            c.execute('INSERT INTO films (title, description, genre, duration, poster) VALUES (?,?,?,?,?)',
                      (title, desc, genre, dur, poster))
            film_ids.append(c.execute('SELECT last_insert_rowid()').fetchone()[0])

        # ── Сеансы на 14 дней вперёд ──
        today = date.today()
        session_dates = [today + _td(days=d) for d in range(1, 15)]
        times = ['10:00', '13:00', '16:00', '19:00', '22:00']
        prices = {0: 450, 1: 400, 2: 350, 3: 400, 4: 380}
        session_ids = []
        for film_i, film_id in enumerate(film_ids):
            base_price = prices[film_i]
            for day in session_dates:
                if day.weekday() >= 5:
                    base_price = int(base_price * 1.2)  # выходные дороже
                for t in times[::2]:  # 10:00, 16:00, 22:00
                    hall_id = hall_big_id if (film_i % 2 == 0) else hall_vip_id
                    c.execute('INSERT INTO sessions (film_id, hall_id, date, time, price) VALUES (?,?,?,?,?)',
                              (film_id, hall_id, day.isoformat(), t, base_price))
                    session_ids.append(c.execute('SELECT last_insert_rowid()').fetchone()[0])

        # ── Акции ──
        promos = [
            ('Студенческая скидка', 'Скидка 20% на все сеансы со вторника по четверг при предъявлении студенческого билета.', 20, ''),
            ('Семейный день', 'Каждое воскресенье — скидка 30% на билеты для всей семьи (от 3 билетов).', 30, ''),
            ('Счастливые часы', 'Скидка 25% на сеансы до 16:00 в будни.', 25, ''),
            ('День киномана', 'Каждую пятницу — двойные бонусы по карте лояльности на все покупки.', 15, ''),
        ]
        promo_ids = []
        for title, desc, discount, image in promos:
            c.execute('INSERT INTO promotions (title, description, discount, image) VALUES (?,?,?,?)',
                      (title, desc, discount, image))
            promo_ids.append(c.execute('SELECT last_insert_rowid()').fetchone()[0])

        # ── Бонусные карты и немного истории ──
        cards = {
            u_anna: (4200, 15600, 'Silver'),
            u_ivan: (900, 3100, 'Basic'),
            u_olga: (18750, 32100, 'Platinum'),
            u_dmitry: (3500, 9800, 'Silver'),
            u_elena: (0, 0, 'Basic'),
        }
        now_iso = datetime.now().isoformat()
        for uid, (balance, spent, level) in cards.items():
            card_number = 'MK-' + ''.join(str(secrets.randbelow(10)) for _ in range(12))
            c.execute('''INSERT INTO loyalty_cards
                         (user_id, card_number, bonus_balance, total_spent, level, created_at)
                         VALUES (?,?,?,?,?,?)''', (uid, card_number, balance, spent, level, now_iso))
            if balance > 0:
                c.execute('''INSERT INTO bonus_history (user_id, booking_id, amount, operation, balance_after, created_at)
                             VALUES (?, NULL, ?, 'Пополнение на кассе', ?, ?)''',
                          (uid, balance, balance, now_iso))

        # ── Примеры проданных билетов (статус paid) ──
        def _all_seats(hall_id):
            rows = c.execute('SELECT id FROM seats WHERE hall_id=?', (hall_id,)).fetchall()
            return [r['id'] for r in rows]

        # пара билетов Анне на 1-й сеанс
        for seat_id in _all_seats(hall_big_id)[:2]:
            # переиспользуем первый session_id из списка сеансов (большой зал)
            sid = session_ids[0]
            c.execute('''INSERT INTO bookings (user_id, session_id, seat_id, booked_at, status, custom_code, promo_id, final_price, payment_method)
                         VALUES (?,?,?,?,?,?,?,?,?)''',
                      (u_anna, sid, seat_id, now_iso, 'paid', None, promo_ids[2], 337, 'bonus'))
            bid = c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.execute('UPDATE bookings SET custom_code=? WHERE id=?',
                      (make_ticket_code('Дюна: Часть третья', film_ids[0], bid), bid))

    conn.commit()


# импорты, нужные seed_db (сделаны здесь, чтобы не тянуть их везде)
from datetime import date
import os
import secrets
from werkzeug.security import check_password_hash
from utils import make_ticket_code