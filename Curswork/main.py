import os
import logging

from app import create_app, init_extensions
from db import init_db
from security import register_security_handlers
import config

# A09-10 аудита: раньше logging.basicConfig не вызывался — в проде логи
# уходили в stderr по умолчанию, без формата и без уровня. Настраиваем
# явно. Формат включает время, уровень, имя логгера и сообщение.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
# werkzeug-логгер не спамит access-логами на каждый запрос в проде —
# оставь INFO, если нужны, или WARNING, если не нужны.
logging.getLogger('werkzeug').setLevel(logging.INFO)


def register_blueprints(app):
    from users_auth import users_bp, ALL_AUTH_ENDPOINTS
    from public import public_bp, ALL_PUBLIC_ENDPOINTS
    from cabinet import cabinet_bp, ALL_CABINET_ENDPOINTS
    from api import api_bp, ALL_API_ENDPOINTS
    from staff import staff_bp, ALL_STAFF_ENDPOINTS
    from admin import admin_bp, ALL_ADMIN_ENDPOINTS

    # Регистрируем, чтобы у каждой view-функции были свой url_rule и methods.
    # Flask сохраняет эти атрибуты на самой функции при вызове bp.route(...)
    # (через add_url_rule с endpoint'ом по имени функции).
    app.register_blueprint(users_bp)
    app.register_blueprint(public_bp)
    app.register_blueprint(cabinet_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(staff_bp)
    app.register_blueprint(admin_bp)

    # Раньше весь код жил в main.py без blueprint'ов, и шаблоны/JS звали
    # url_for('index'), url_for('login') и т.д. — «короткие» endpoint'ы.
    # В списках ALL_*_ENDPOINTS лежат пары (short_name, view_func).
    # Регистрируем каждый короткий endpoint отдельным add_url_rule с той же
    # функцией, но под коротким именем.
    for short, view_func in (ALL_AUTH_ENDPOINTS + ALL_PUBLIC_ENDPOINTS +
                             ALL_CABINET_ENDPOINTS + ALL_API_ENDPOINTS +
                             ALL_STAFF_ENDPOINTS + ALL_ADMIN_ENDPOINTS):
        if short in app.view_functions:
            continue
        # Навешиваем правило из атрибутов функции, которые проставляет Flask
        # в bp.route(). В старых версиях Flask их нет — тогда берём из
        # blueprint-правила через url_map (см. ниже fallback).
        rule = getattr(view_func, '_rule', None)
        methods = getattr(view_func, '_methods', None)
        if rule is None:
            # Fallback: найдём правило для длинного endpoint'а в url_map
            # по имени функции (bp.endpoint == view_func.__name__). Это не
            # сработает для переопределённых endpoint'ов, но все наши
            # endpoint'ы названы по именам функций.
            for r in app.url_map.iter_rules():
                if r.endpoint == view_func.__name__ or r.endpoint.endswith('.' + view_func.__name__):
                    rule = r.rule
                    methods = r.methods - {'HEAD', 'OPTIONS'}
                    break
        if rule:
            app.add_url_rule(rule, endpoint=short, view_func=view_func,
                             methods=methods or ['GET', 'POST'])


app = create_app()
init_extensions(app)
register_security_handlers(app)
register_blueprints(app)

# A09-9 аудита: request.host и X-Forwarded-For попадают в audit_log как есть.
# Без ProxyFix и TRUSTED_HOSTS внешний клиент может подделать Host: attacker.tld
# и X-Forwarded-For: 1.2.3.4 — в журнале окажется фейковый url/ip.
# TRUSTED_HOSTS появился в Flask 3.1 (у нас 3.1.3 — ок).
_allowed_hosts = os.environ.get('TRUSTED_HOSTS', '').strip()
if _allowed_hosts:
    app.config['TRUSTED_HOSTS'] = [h.strip() for h in _allowed_hosts.split(',') if h.strip()]

# ProxyFix — только если приложение реально за обратным прокси.
# Если TRUST_PROXY_HEADERS=0, заголовки X-Forwarded-* игнорируются.
if os.environ.get('TRUST_PROXY_HEADERS') == '1':
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


if __name__ == '__main__':
    # debug=True включает интерактивную консоль Werkzeug (A02-3 аудита):
    # /console даёт удалённый доступ к Python-процессу. Даже сознательно
    # включённый FLASK_DEBUG=1 не должен сочетаться со слушанием всех
    # интерфейсов — иначе консоль будет доступна извне.
    if config.DEBUG:
        host = os.environ.get('FLASK_HOST', '127.0.0.1')
        if host not in ('127.0.0.1', 'localhost', '::1'):
            raise RuntimeError(
                'FLASK_DEBUG=1 можно включать только при FLASK_HOST=127.0.0.1. '
                f'Сейчас FLASK_HOST={host} — debug-консоль была бы доступна извне.'
            )
    init_db()
    # A02-3 аудита: интерактивный отладчик Werkzeug (/console) — это RCE
    # (выполнение произвольного Python-кода на сервере). Одной проверки
    # FLASK_HOST недостаточно: если приложение стоит за обратным прокси,
    # «внутренний» 127.0.0.1 может быть доступен снаружи. Поэтому отладчик
    # выключается ЖЁСТКО и безусловно:
    #   * debug=False   — Flask не включает ни debugger, ни reloader сам;
    #   * use_debugger=False — /console не регистрируется вообще;
    #   * use_reloader=config.DEBUG — при FLASK_DEBUG=1 остаётся только
    #     авто-перезапуск при правке кода (удобство разработки, не RCE).
    # Итог: FLASK_DEBUG=1 больше НЕ открывает доступ к консоли ни при какой
    # конфигурации прокси.
    app.run(
        debug=False,
        use_debugger=False,
        use_reloader=config.DEBUG,
        host=os.environ.get('FLASK_HOST', '127.0.0.1'),
        port=int(os.environ.get('FLASK_PORT', 5000)),
    )