import logging
import time as _time
from flask import Blueprint, request, redirect, url_for, session, flash, jsonify
from datetime import datetime, timedelta

ALL_AUTH_ENDPOINTS = []  # (короткое_имя, view) заполняется внизу файла

from db import get_db, log_audit, hash_password, verify_password
from utils import (
    current_user, is_valid_email,
    create_verification_code, check_verification_code,
    create_loyalty_card,
)
from security import login_required
from app import limiter
from mailer import send_confirmation_email, send_recovery_email

logger = logging.getLogger(__name__)

users_bp = Blueprint('users', __name__)


def user_is_login_blocked(conn, email, threshold=10):
    """True, если за последние 15 минут было >= threshold неудачных попыток входа."""
    row = conn.execute('''
        SELECT COUNT(*) as c FROM login_attempts
        WHERE email=? AND attempted_at > ?
    ''', (email, (datetime.now() - timedelta(minutes=15)).isoformat())).fetchone()
    return bool(row and row['c'] >= threshold)


# ═══════════════════════════════════════════════════════════════
# АВТОРИЗАЦИЯ
# ═══════════════════════════════════════════════════════════════


@users_bp.route('/send_registration_code', methods=['POST'])
@limiter.limit('5 per minute')
def send_registration_code():
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '')
    confirm_password = request.form.get('confirm_password', '')

    if not name:
        return {"status": "error", "message": "Введите имя"}, 400
    if not is_valid_email(email):
        return {"status": "error", "message": "Некорректный формат почты"}, 400
    if len(password) < 8:
        return {"status": "error", "message": "Минимум 8 символов"}, 400
    if password != confirm_password:
        return {"status": "error", "message": "Пароли не совпадают"}, 400

    conn = get_db()
    existing = conn.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
    if existing:
        conn.close()
        return {"status": "error", "message": "Email уже зарегистрирован"}, 400

    import json
    # Хэш пароля храним в payload verification_codes с коротким TTL (5 минут,
    # см. check_verification_code). Это безопаснее, чем держать его в cookie-
    # сессии 12 часов: при утечке SECRET_KEY окно для брутфорса минимально.
    payload = json.dumps({
        'name': name,
        'password_hash': hash_password(password),
    })
    code = create_verification_code(conn, 'registration', email, payload)
    conn.close()

    # В сессии держим только email — чтобы найти запись в verification_codes.
    session['pending_registration_email'] = email

    try:
        send_confirmation_email(
            email,
            "Код подтверждения регистрации - Мир Кино",
            "Для завершения регистрации введите код подтверждения:",
            code
        )
        return {"status": "success", "message": "Код отправлен"}
    except Exception as e:
        logger.error('Ошибка SMTP при регистрации: %s', e)
        return {"status": "error", "message": "Ошибка при отправке письма. Проверьте настройки почты."}, 500


@users_bp.route('/resend_registration_code', methods=['POST'])
@limiter.limit('3 per minute')
def resend_registration_code():
    email = session.get('pending_registration_email')
    if not email:
        return {"status": "error", "message": "Данные регистрации не найдены. Заполните форму ещё раз."}, 400

    conn = get_db()
    existing = conn.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
    if existing:
        conn.close()
        return {"status": "error", "message": "Email уже зарегистрирован"}, 400

    # Переиспользуем payload от предыдущего запроса кода на этот email
    prev = conn.execute('''
        SELECT payload FROM verification_codes
        WHERE purpose='registration' AND email=? ORDER BY id DESC LIMIT 1
    ''', (email,)).fetchone()
    if not prev:
        conn.close()
        return {"status": "error", "message": "Данные регистрации не найдены. Заполните форму ещё раз."}, 400

    code = create_verification_code(conn, 'registration', email, prev['payload'])
    conn.close()

    try:
        send_confirmation_email(
            email,
            "Код подтверждения регистрации - Мир Кино",
            "Для завершения регистрации введите код подтверждения:",
            code
        )
        return {"status": "success", "message": "Код отправлен"}
    except Exception as e:
        logger.error('Ошибка SMTP при повторной отправке кода регистрации: %s', e)
        return {"status": "error", "message": "Ошибка при отправке письма. Проверьте настройки почты."}, 500


@users_bp.route('/verify_registration_code', methods=['POST'])
@limiter.limit('10 per minute')
def verify_registration_code():
    import json
    input_code = request.form.get('code', '').strip()
    email = session.get('pending_registration_email')
    if not email:
        return {"status": "error", "message": "Данные регистрации не найдены. Заполните форму ещё раз."}, 400

    conn = get_db()
    ok, message, payload = check_verification_code(conn, 'registration', email, input_code)
    if not ok:
        conn.close()
        return {"status": "error", "message": message}, 400

    existing = conn.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
    if existing:
        conn.close()
        return {"status": "error", "message": "Email уже зарегистрирован"}, 400

    data = json.loads(payload)
    password_hash = data.get('password_hash')
    if not password_hash:
        conn.close()
        session.pop('pending_registration_email', None)
        return {"status": "error", "message": "Время на регистрацию истекло. Заполните форму заново."}, 400
    conn.execute('INSERT INTO users (name, email, password, role) VALUES (?,?,?,?)',
                 (data['name'], email, password_hash, 'client'))
    conn.commit()
    user = conn.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
    conn.close()

    session.clear()
    session['session_started_at'] = datetime.now().isoformat()
    session.permanent = True
    session['user_id'] = user['id']
    session['user_role'] = user['role']
    return {"status": "success", "message": "Регистрация завершена", "redirect": url_for('index')}


@users_bp.route('/send_loyalty_code', methods=['POST'])
@login_required
@limiter.limit('5 per minute')
def send_loyalty_code():
    user = current_user()
    if not user or user['role'] != 'client':
        return {"status": "error", "message": "Карта доступна только клиентам"}, 403

    conn = get_db()
    existing = conn.execute('SELECT id FROM loyalty_cards WHERE user_id=?', (user['id'],)).fetchone()
    if existing:
        conn.close()
        return {"status": "error", "message": "Карта уже выпущена"}, 400

    code = create_verification_code(conn, 'loyalty', user['email'])
    conn.close()

    try:
        send_confirmation_email(
            user['email'],
            "Код для выпуска бонусной карты - Мир Кино",
            "Для выпуска бонусной карты введите код подтверждения:",
            code
        )
        return {"status": "success", "message": "Код отправлен"}
    except Exception as e:
        logger.error('Ошибка SMTP при выпуске карты: %s', e)
        return {"status": "error", "message": "Ошибка при отправке письма. Проверьте настройки почты."}, 500


@users_bp.route('/verify_loyalty_code', methods=['POST'])
@login_required
@limiter.limit('10 per minute')
def verify_loyalty_code():
    user = current_user()
    if not user or user['role'] != 'client':
        return {"status": "error", "message": "Карта доступна только клиентам"}, 403

    input_code = request.form.get('code', '').strip()
    conn = get_db()
    ok, message, _ = check_verification_code(conn, 'loyalty', user['email'], input_code)
    if not ok:
        conn.close()
        return {"status": "error", "message": message}, 400

    create_loyalty_card(conn, user['id'])
    conn.close()
    return {"status": "success", "message": "Бонусная карта выпущена", "redirect": url_for('loyalty')}


# LEGACY (Легаси-2 аудита): форма /register больше не регистрирует — регистрация
# идёт только через код из письма (send_registration_code → verify_registration_code).
# Маршрут оставлен как заглушка: молча прогоняет любой POST на главную, чтобы
# старые ссылки не давали 404. Удалите его вместе со старыми формами в шаблонах.
@users_bp.route('/register', methods=['GET', 'POST'])
def register():
    return redirect(url_for('index'))


@users_bp.route('/login', methods=['POST'])
@limiter.limit('5 per minute')
def login():
    email = (request.form.get('email') or '').strip().lower()
    password = request.form.get('password') or ''

    if not email or not password:
        log_audit(None, None, 'login_failed', 'Попытка входа без email или пароля')
        flash('Неверный email или пароль', 'danger')
        return redirect(url_for('index'))

    # Антибрутфорс на аккаунт: после 10 неудачных попыток по одному email
    # следующие 15 минут считаем каждую попытку неудачной без полной
    # проверки пароля (дешевле CPU, не подкармливает перебор).
    conn = get_db()
    now_iso = datetime.now().isoformat()
    conn.execute('''
        DELETE FROM login_attempts
        WHERE email = ? AND attempted_at < ?
    ''', (email, (datetime.now() - timedelta(minutes=15)).isoformat()))
    conn.commit()

    if not user_is_login_blocked(conn, email):
        user = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()

        # Проверяем бан перед проверкой пароля (экономия CPU и улучшение безопасности)
        if user and user['is_banned']:
            log_audit(None, user, 'login_banned', 'Забаненный пользователь пытался войти')
            conn.close()
            flash('Ваш аккаунт заблокирован.', 'danger')
            return redirect(url_for('index'))

        if user and verify_password(user['password'], password):
            log_audit(conn, user, 'login', f'Вход (email: {email})')
            conn.execute('DELETE FROM login_attempts WHERE email=?', (email,))
            conn.commit()
            conn.close()

            session.clear()  # анти fixation: гарантированно новая сессия
            session.permanent = True
            # Запоминаем «рождение» сессии: после смены пароля (password_changed_at
            # становится новее) все старые сессии умирают (A07-3 аудита).
            session['session_started_at'] = datetime.now().isoformat()
            session['user_id'] = user['id']
            session['user_role'] = user['role']
            # Для админов сессия заканчивается при закрытии браузера
            if user['role'] in ('admin', 'cashier'):
                session.permanent = False
                session.modified = True
                return redirect(url_for('personal'))
            return redirect(url_for('index'))
        else:
            # Фиксируем неудачную попытку (только если аккаунт существует —
            # чтобы чужим email нельзя было исчерпать лимит)
            if user:
                conn.execute(
                    'INSERT INTO login_attempts (email, attempted_at) VALUES (?,?)',
                    (email, now_iso))
                conn.commit()
            # Логируем полную проверку пароля
            if user:
                log_audit(None, user, 'login_failed', 'Неверный пароль')
            else:
                log_audit(None, None, 'login_failed', f'Неизвестный email {email}')
            conn.close()
            # Один и тот же текст ошибки независимо от причины — чтобы нельзя
            # было перебором узнать, какие email зарегистрированы.
            flash('Неверный email или пароль', 'danger')
            return redirect(url_for('index'))
    else:
        # Аккаунт временно заблокирован из-за большого числа неудачных попыток.
        # Тот же текст ошибки, чтобы не раскрывать причину стороннему глазу.
        log_audit(None, None, 'login_failed', f'Временная блокировка по перебору (email: {email})')
        conn.close()
        flash('Неверный email или пароль', 'danger')
        return redirect(url_for('index'))


@users_bp.route('/logout', methods=['POST'])
def logout():
    user = current_user()
    conn = get_db()
    log_audit(conn, user, 'logout', 'Выход из системы')
    conn.commit()
    conn.close()
    session.clear()
    return redirect(url_for('index'))


# ═══════════════════════════════════════════════════════════════
# Восстановление пароля
# ═══════════════════════════════════════════════════════════════


@users_bp.route('/send_recovery_code', methods=['POST'])
@limiter.limit('5 per minute')
def send_recovery_code():
    email = request.form.get('email', '').strip().lower()
    if not is_valid_email(email):
        return {"status": "error", "message": "Некорректный формат почты"}, 400

    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()

    # Не раскрываем, зарегистрирован ли email: отвечаем одинаково в обоих
    # случаях, письмо реально уходит только если пользователь существует.
    session['recovery_email'] = email
    session.pop('recovery_verified', None)

    # A07 аудита (user enumeration): ответ на существующий и несуществующий
    # email должен быть БУКВАЛЬНО одинаковым. Раньше ветки возвращали разный
    # текст («Код успешно отправлен» против «Если такой email зарегистрирован…»),
    # и по нему перебором узнавались зарегистрированные адреса. Текст один.
    ok_message = "Если такой email зарегистрирован, код восстановления отправлен на почту"

    if not user:
        conn.close()
        return {"status": "success", "message": ok_message}

    code = create_verification_code(conn, 'recovery', email)
    conn.close()

    try:
        send_recovery_email(email, code)
        return {"status": "success", "message": ok_message}
    except Exception as e:
        logger.error('Ошибка SMTP: %s', e)
        return {"status": "error", "message": "Ошибка при отправке письма. Проверьте настройки почты."}, 500


@users_bp.route('/verify_recovery_code', methods=['POST'])
@limiter.limit('10 per minute')
def verify_recovery_code():
    input_code = request.form.get('code', '').strip()
    email = session.get('recovery_email')

    if not email:
        return {"status": "error", "message": "Код не найден. Запросите новый."}, 400

    conn = get_db()
    ok, message, _ = check_verification_code(conn, 'recovery', email, input_code)
    if not ok:
        conn.close()
        return {"status": "error", "message": message}, 400

    # A07-11 аудита: если пользователя забанили в течение 5-минутного окна
    # между отправкой кода и его вводом — не даём подтвердить восстановление.
    # Забаненный всё равно не войдёт (login проверяет is_banned), но лучше
    # не открывать ему даже сброс пароля.
    user_row = conn.execute('SELECT id, is_banned FROM users WHERE email=?', (email,)).fetchone()
    conn.close()
    if not user_row:
        session.pop('recovery_email', None)
        return {"status": "error", "message": "Пользователь не найден"}, 400
    if user_row['is_banned']:
        session.pop('recovery_email', None)
        return {"status": "error", "message": "Аккаунт заблокирован"}, 403

    session['recovery_verified'] = True
    session['recovery_verified_at'] = _time.time()
    return {"status": "success", "message": "Код подтвержден"}


@users_bp.route('/reset_password', methods=['POST'])
@limiter.limit('10 per minute')
def reset_password():
    new_password = request.form.get('password', '')
    email = session.get('recovery_email')
    verified = session.get('recovery_verified')
    verified_at = session.get('recovery_verified_at')

    if not email or not verified or not verified_at:
        return {"status": "error", "message": "Сначала подтвердите код из письма."}, 400

    if _time.time() - verified_at > 300:  # окно в 5 минут на смену пароля после подтверждения кода
        session.pop('recovery_verified', None)
        session.pop('recovery_verified_at', None)
        return {"status": "error", "message": "Время на смену пароля истекло. Начните восстановление заново."}, 400

    if len(new_password) < 8:
        return {"status": "error", "message": "Пароль должен быть не короче 8 символов"}, 400

    conn = get_db()
    user = conn.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
    if not user:
        conn.close()
        session.clear()
        return {"status": "error", "message": "Сессия истекла. Начните восстановление заново."}, 400

    # password_changed_at ставим в момент смены: все ранее выданные сессии
    # (включая украденные) перестают действовать (A07-3 аудита).
    conn.execute('UPDATE users SET password=?, password_changed_at=? WHERE email=?',
                 (hash_password(new_password), datetime.now().isoformat(), email))
    conn.commit()
    conn.close()

    session.pop('recovery_email', None)
    session.pop('recovery_verified', None)
    session.pop('recovery_verified_at', None)

    return {"status": "success", "message": "Пароль успешно изменен"}


# короткие endpoint'ы для url_for в шаблонах (см. register_blueprints в main.py)
ALL_AUTH_ENDPOINTS = [
    ('send_registration_code', send_registration_code),
    ('resend_registration_code', resend_registration_code),
    ('verify_registration_code', verify_registration_code),
    ('send_loyalty_code', send_loyalty_code),
    ('verify_loyalty_code', verify_loyalty_code),
    ('register', register),
    ('login', login),
    ('logout', logout),
    ('send_recovery_code', send_recovery_code),
    ('verify_recovery_code', verify_recovery_code),
    ('reset_password', reset_password),
]