from flask import Flask
from flask_mail import Mail
from flask_wtf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import config


def create_app():
    app = Flask(__name__)
    app.secret_key = config.SECRET_KEY
    app.config['DB_PATH'] = config.DB_PATH

    app.config['MAIL_SERVER'] = config.MAIL_SERVER
    app.config['MAIL_PORT'] = config.MAIL_PORT
    app.config['MAIL_USE_TLS'] = config.MAIL_USE_TLS
    app.config['MAIL_USE_SSL'] = config.MAIL_USE_SSL
    app.config['MAIL_USERNAME'] = config.MAIL_USERNAME
    app.config['MAIL_PASSWORD'] = config.MAIL_PASSWORD
    app.config['MAIL_DEFAULT_SENDER'] = config.MAIL_DEFAULT_SENDER

    app.config['SESSION_COOKIE_HTTPONLY'] = config.SESSION_COOKIE_HTTPONLY
    app.config['SESSION_COOKIE_SAMESITE'] = config.SESSION_COOKIE_SAMESITE
    app.config['SESSION_COOKIE_SECURE'] = config.SESSION_COOKIE_SECURE
    app.config['PERMANENT_SESSION_LIFETIME'] = config.PERMANENT_SESSION_LIFETIME

    app.config['MAX_CONTENT_LENGTH'] = config.MAX_CONTENT_LENGTH

    app.jinja_env.add_extension('jinja2.ext.do')

    return app


# По-старому: экземпляры расширений живут на уровне модуля, чтобы их можно
# было импортировать из любого модуля приложения (mailer, security и т.д.).
mail = Mail()
csrf = CSRFProtect()
limiter = Limiter(
    get_remote_address,
    default_limits=[],
    storage_uri=config.RATE_LIMIT_STORAGE_URI,
)


def init_extensions(app):
    mail.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)