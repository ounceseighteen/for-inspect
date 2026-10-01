from datetime import timedelta
from dotenv import load_dotenv
import os

load_dotenv()  # читает переменные из .env (см. .env.example)


def require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Не задана обязательная переменная окружения {name}. "
            f"Скопируйте .env.example в .env и заполните значения."
        )
    return value


def require_strong_env(name, min_len=8):
    """Обязательная переменная + минимальная длина (A02-1 аудита).
    Админ-пароль и подобные секреты не должны быть короткими: раньше
    требовалось только «заполнено», и ADMIN_PASSWORD=123 проходил мимо."""
    value = require_env(name)
    if len(value) < min_len:
        raise RuntimeError(
            f"{name} должен быть не короче {min_len} символов (текущая длина: {len(value)})."
        )
    return value


# ─── Секреты и конфигурация только из окружения, никогда не в коде ───
SECRET_KEY = require_env('SECRET_KEY')

# Валидация SECRET_KEY
if len(SECRET_KEY) < 32:
    raise RuntimeError(
        f"SECRET_KEY должен быть минимум 32 символа (текущая длина: {len(SECRET_KEY)}). "
        "Создайте надежный секретный ключ: python -c 'import secrets; print(secrets.token_hex(32))'"
    )
if SECRET_KEY.lower() in ('changeme', 'secret', '123456', 'password', 'admin', 'demo'):
    raise RuntimeError(
        "SECRET_KEY не должен быть базовым значением (changeme, secret и т.д.). "
        "Создайте надежный секретный ключ: python -c 'import secrets; print(secrets.token_hex(32))'"
    )
DB_PATH = os.environ.get('DB_PATH', 'mirkino.db')

MAIL_SERVER = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
MAIL_PORT = int(os.environ.get('MAIL_PORT', 587))
# TLS (STARTTLS, порт 587 у Gmail) и SSL (implicit TLS, порт 465 у Яндекс/Почты)
# включаются из .env, но по умолчанию определяются по порту. Важно: Gmail SMTP
# с российских IP не отвечает (соединение обрывается до аутентификации), поэтому
# для РФ рекомендуются smtp.yandex.ru:465 или smtp.mail.ru:465 с паролем приложения.
MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS', '1' if MAIL_PORT == 587 else '0') == '1'
MAIL_USE_SSL = os.environ.get('MAIL_USE_SSL', '1' if MAIL_PORT == 465 else '0') == '1'

if MAIL_USE_TLS and MAIL_USE_SSL:
    raise RuntimeError(
        'MAIL_USE_TLS и MAIL_USE_SSL не могут быть включены одновременно. '
        f'Выберите один режим: STARTTLS (обычно порт 587) или implicit TLS '
        f'(обычно порт 465). Сейчас MAIL_PORT={MAIL_PORT}.'
    )

MAIL_USERNAME = require_env('MAIL_USERNAME')
MAIL_PASSWORD = require_env('MAIL_PASSWORD')
MAIL_DEFAULT_SENDER = ('Мир Кино', MAIL_USERNAME)

# ─── Куки и сессии ───
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
# Secure-флаг требует HTTPS. На локальном http://127.0.0.1 отключаем,
# на проде FORCE_HTTPS=1 обязателен.
SESSION_COOKIE_SECURE = os.environ.get('FORCE_HTTPS') == '1'
PERMANENT_SESSION_LIFETIME = timedelta(hours=12)

DEBUG = os.environ.get('FLASK_DEBUG') == '1'

if not DEBUG and not SESSION_COOKIE_SECURE:
    raise RuntimeError(
        'На проде обязателен FORCE_HTTPS=1: cookie сессии должны передаваться '
        'только по HTTPS. Для локальной разработки установите FLASK_DEBUG=1.'
    )

# ─── Загрузка файлов ───
MAX_CONTENT_LENGTH = 8 * 1024 * 1024  # 8 МБ на запрос

# Хранилище лимитов по умолчанию — в памяти процесса. Для одного экземпляра
# это нормально, но роняем предупреждение flask-limiter в логах и
# теряем счётчики при перезапуске. Чтобы убрать предупреждение и сделать
# лимиты стойкими, поставьте redis-py (`pip install redis`) и задайте в .env
# `RATE_LIMIT_STORAGE_URI=redis://localhost:6379/0`.
RATE_LIMIT_STORAGE_URI = os.environ.get('RATE_LIMIT_STORAGE_URI', 'memory://')

ALLOWED_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
ALLOWED_ROLES = ('client', 'admin', 'cashier')

REFUND_HOURS_BEFORE = int(os.environ.get('REFUND_HOURS_BEFORE', 6))  # вернуть билет можно не позднее чем за N часов до сеанса