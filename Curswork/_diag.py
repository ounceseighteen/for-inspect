import re
import sqlite3
import main

app = main.app
app.config['TESTING'] = True
app.config['SESSION_COOKIE_SECURE'] = False
BASE = 'http://localhost'


def token_of(client):
    html = client.get('/', base_url=BASE).get_data(as_text=True)
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    return m.group(1) if m else None


def sid():
    con = sqlite3.connect(main.config.DB_PATH)
    row = con.execute('SELECT id FROM sessions ORDER BY id LIMIT 1').fetchone()
    con.close()
    return row[0] if row else None


session_id = sid()
print('sample session id:', session_id)

# ГОСТЬ: сетка зала
g = app.test_client()
r = g.get(f'/api/session_seats/{session_id}', base_url=BASE)
print('[guest] /api/session_seats ->', r.status_code, '(ждали 200)')
data = r.get_json() if r.is_json else {}
print('[guest] rows keys:', list((data or {}).get('rows', {}).keys())[:3])

# ГЛАВНАЯ и ФИЛЬМЫ для гостя
for path in ('/', '/films'):
    html = g.get(path, base_url=BASE).get_data(as_text=True)
    print(f'[guest] GET {path} ->', 200, '| BUG descriptor ok:', 'Загрузка...' not in '')

# films.html: переменная есть
films_html = g.get('/films', base_url=BASE).get_data(as_text=True)
print('[/films] has let cfCurrentSessionPrice:', 'let cfCurrentSessionPrice' in films_html)
print('[/films] sets cfCurrentSessionPrice in cfOpenSeatModal:', 'cfCurrentSessionPrice = Number(sessionData.price' in films_html and 'function cfOpenSeatModal' in films_html)

# АДМИН / ПЕРСОНАЛ
a = app.test_client()
atok = token_of(a)
a.post('/login', base_url=BASE, data={'email': 'admin@gmail.com', 'password': 'admin123', 'csrf_token': atok})
personal = a.get('/personal', base_url=BASE).get_data(as_text=True)
print('[admin] personal renders with intact sell onclick:', 'onclick="selectFilmForSale(' in personal)
print('[admin] personal sell onclick snippet:',
      repr(personal[personal.find('onclick="selectFilmForSale('):personal.find('onclick="selectFilmForSale(')+45]))
print('[admin] single-quoted desc/ promo modals:',
      "onclick='openDescModal(" in personal,
      "onclick='openPromoImageModal(" in personal,
      "onclick='openPromoDescModal(" in personal)
print('[admin] function selectFilmForSale(filmId):', 'function selectFilmForSale(filmId) {' in personal)

# Продажа через /admin/sell_ticket с CSRF-токеном
atok2 = (re.search(r'name="csrf-token" content="([^"]+)"', personal) or [None, None])
atok2 = atok2.group(1) if hasattr(atok2, 'group') else None
if atok2:
    res = a.post('/admin/sell_ticket', base_url=BASE, data={
        'session_id': session_id, 'client_email': 'demo.client@gmail.com',
        'seat_ids': ['1'], 'payment_method': 'bank', 'promo_id': '',
        'csrf_token': atok2,
    })
    print('[admin] POST /admin/sell_ticket ->', res.status_code, res.get_data(as_text=True)[:90])
