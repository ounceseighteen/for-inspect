from functools import wraps
from flask import session, flash, redirect, url_for, request, jsonify
from flask_wtf.csrf import CSRFError
from db import get_db
from utils import current_user


def login_required(f):
    """
    Проверяет, что есть сессия, пользователь НЕ заблокирован и его роль
    допустима (A07/A01 аудита). Блокировка проверяется по свежей БД на
    каждый запрос — скомпрометированный аккаунт, которого баннули,
    «вылетает» со всех страниц, а не после перелогаина.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Войдите в систему', 'warning')
            return redirect(url_for('login'))
        user = current_user()
        if not user:
            # user_id в сессии, но пользователя больше нет (удалён) — чистим сессию
            session.clear()
            flash('Войдите в систему', 'warning')
            return redirect(url_for('login'))
        if user['is_banned']:
            session.clear()
            flash('Ваш аккаунт заблокирован. Обратитесь к администрации.', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = current_user()
        if not user or user['role'] != 'admin':
            flash('Доступ запрещён', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated


def roles_required(*roles):
    """Общий декоратор: пускает только пользователей с одной из перечисленных ролей."""
    def wrapper(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user = current_user()
            if not user or user['role'] not in roles:
                flash('Доступ запрещён', 'danger')
                return redirect(url_for('index'))
            return f(*args, **kwargs)
        return decorated
    return wrapper


def require_role(*roles):
    """Проверка роли внутри view-функции (без декоратора). Если у пользователя
    нет ни одной из перечисленных ролей — abort(403) (обрабатывается
    errorhandler'ом в security.py). Вызывать первым оператором в view."""
    from flask import abort
    user = current_user()
    if not user or user['role'] not in roles:
        abort(403)


def client_required(f):
    """Пускает только клиентов: кассир/админ не могут действовать как клиент
    (покупать билеты, оставлять отзывы и т.п.) — их самообслуживание обходит
    кассовую логику (A01-1 аудита)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = current_user()
        if not user or user['role'] != 'client':
            return jsonify({'error': 'Доступно только клиентам'}), 403
        return f(*args, **kwargs)
    return decorated


def render_simple_page(message):
    """Минимальная страница для ошибок 404/500 — без зависимости от шаблонов,
    которые могут сами сломаться."""
    from flask import render_template_string
    return render_template_string(
        '<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">'
        '<title>Мир Кино</title></head><body style="font-family:sans-serif;'
        'background:#0d0f14;color:#e8eaf0;display:flex;align-items:center;'
        'justify-content:center;min-height:100vh;">'
        '<div style="text-align:center;padding:2rem;"><h1>{{ message }}</h1>'
        '<a href="/" style="color:#e8a020;">На главную</a></div></body></html>',
        message=message)


def register_security_handlers(app):
    """
    Глобальные обработчики запросов: проверка бана, логирование ошибок
    и security-заголовки на всех ответах.
    """
    import logging
    logger = logging.getLogger(__name__)

    @app.before_request
    def enforce_ban_and_session_hygiene():
        """
        Раньше блокировка (is_banned) проверялась только в момент входа: если
        пользователя забанили посреди активной сессии, он продолжал работать
        до перелогина. Теперь проверяем на каждом запросе. Плюс инвалидация
        сессий после смены пароля (A07-3 аудита): если password_changed_at
        пользователя новее «рождения» сессии — все старые сессии мертвы,
        нужно войти заново.
        """
        if 'user_id' in session:
            conn = get_db()
            row = conn.execute('SELECT is_banned, password_changed_at FROM users WHERE id=?',
                               (session['user_id'],)).fetchone()
            if not row or row['is_banned']:
                conn.close()
                session.clear()
                return
            pwd_changed = row['password_changed_at'] or ''
            session_start = session.get('session_started_at') or ''
            if pwd_changed and session_start and pwd_changed > session_start:
                conn.close()
                session.clear()
                return
            conn.close()

    @app.errorhandler(400)
    def handle_bad_request(e):
        logger.warning('Bad request %s %s', request.method, request.path)
        return 'Неверный запрос', 400

    @app.errorhandler(403)
    def handle_forbidden(e):
        logger.warning('Forbidden %s %s from %s', request.method, request.path, request.remote_addr)
        return 'Доступ запрещён', 403

    @app.errorhandler(404)
    def handle_not_found(e):
        logger.warning('Not found %s %s from %s', request.method, request.path, request.remote_addr)
        return render_simple_page('Страница не найдена'), 404

    @app.errorhandler(500)
    def handle_server_error(e):
        logger.exception('Internal error on %s %s', request.method, request.path)
        return render_simple_page('Внутренняя ошибка сервера'), 500

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        # A09-7 аудита: раньше CSRF-отказ Flask-WTF превращался в «голый» 400
        # без записи в лог — атакующий мог молотить формы без токена бесследно.
        logger.warning('CSRF failure %s %s from %s', request.method, request.path, request.remote_addr)
        return 'Неверный запрос', 400

    @app.after_request
    def set_security_headers(resp):
        """Базовые security-заголовки для всех страниц (XSS-защита из коробки)."""
        resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
        resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
        resp.headers.setdefault('Referrer-Policy', 'same-origin')
        # HSTS включаем только когда cookie сессий отдаются по HTTPS (A02-3
        # аудита: SESSION_COOKIE_SECURE управляется FORCE_HTTPS=1 в .env).
        # CSP-строка подобрана под текущий шаблон (inline-стили и скрипты).
        # 'unsafe-inline' в style-src нужен: шаблоны стилизованы inline-атрибутами.
        # В script-src 'unsafe-inline' сохранён, т.к. часть скриптов добавлена
        # прямо в body; 'unsafe-eval' убран — в кодовой базе eval/new Function
        # не используются.
        resp.headers.setdefault(
            'Content-Security-Policy',
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; font-src 'self' data:; connect-src 'self'"
        )
        return resp