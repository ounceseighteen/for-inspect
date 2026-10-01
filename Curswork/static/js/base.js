// ═══════════════════════════════════════════════════════════════
// Базовый JS «Мир Кино»: CSRF, модалка входа/регистрации/
// восстановления пароля, уведомления, модалка билета.
// Подключается из base.html на КАЖДОЙ странице.
// ═══════════════════════════════════════════════════════════════

// ─────────────────────────────────────────────
// CSRF-защита (Flask-WTF) для ВСЕХ форм и fetch-запросов сайта.
// Подключено один раз на всех страницах (base.html) — так не нужно
// вручную добавлять скрытое поле в каждую форму.
// ─────────────────────────────────────────────
(function () {
    var metaTag = document.querySelector('meta[name="csrf-token"]');
    var CSRF_TOKEN = metaTag ? metaTag.getAttribute('content') : '';

    function injectToken(form) {
        if (!(form instanceof HTMLFormElement)) return;
        if (form.method && form.method.toLowerCase() !== 'post') return;
        if (form.querySelector('input[name="csrf_token"]')) return;
        var input = document.createElement('input');
        input.type = 'hidden';
        input.name = 'csrf_token';
        input.value = CSRF_TOKEN;
        form.appendChild(input);
    }

    function injectAll() {
        document.querySelectorAll('form').forEach(injectToken);
    }

    // Важно: часть форм в личном кабинете/админке отправляется через
    // form.submit() прямо из JS (например, при смене роли пользователя
    // или удалении фильма/сеанса/зала/акции). form.submit() НЕ порождает
    // событие "submit", поэтому token нельзя добавлять только по этому
    // событию — иначе такие формы уйдут без CSRF-токена и получат 400.
    // Поэтому токен проставляется сразу при загрузке DOM...
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', injectAll);
    } else {
        injectAll();
    }

    // ...а на случай форм, которые всё же появляются в DOM позже —
    // подстраховка через делегирование события submit (сработает для
    // обычной отправки по клику) и MutationObserver (на случай, если
    // в будущем появится динамическое создание форм).
    document.addEventListener('submit', function (e) { injectToken(e.target); }, true);
    new MutationObserver(function () { injectAll(); })
        .observe(document.documentElement, { childList: true, subtree: true });

    // fetch(...) — добавляем заголовок X-CSRFToken ко всем запросам,
    // которые меняют данные (POST/PUT/PATCH/DELETE) на своём же домене.
    var originalFetch = window.fetch;
    window.fetch = function (input, init) {
        init = init || {};
        var method = (init.method || 'GET').toUpperCase();
        if (['POST', 'PUT', 'PATCH', 'DELETE'].indexOf(method) !== -1) {
            init.headers = new Headers(init.headers || {});
            if (!init.headers.has('X-CSRFToken')) {
                init.headers.set('X-CSRFToken', CSRF_TOKEN);
            }
        }
        return originalFetch.call(this, input, init);
    };
})();

// ─────────────────────────────────────────────
// ВОССТАНОВЛЕНИЕ ПАРОЛЯ (handleForgot / resendCode / verifyCode /
// saveNewPassword) и общий механизм модалки авторизации
// ─────────────────────────────────────────────

function handleForgot(event) {
    if (event) {
        event.preventDefault();
        event.stopPropagation(); // Важно: предотвращает закрытие модалки
    }

    const emailInput = document.getElementById('forgotEmail');
    const email = emailInput.value.trim();
    const errorBlock = document.getElementById('forgotEmailError');
    const btn = event.target;

    emailInput.addEventListener('input', () => {
        emailInput.style.borderColor = 'var(--border)';
        errorBlock.style.display = 'none';
    });

    if (!email) {
        errorBlock.textContent = "Введите почту";
        errorBlock.style.display = 'block';
        emailInput.style.borderColor = 'var(--danger)';
        return;
    }

    // Блокируем кнопку, чтобы не нажимали много раз
    btn.disabled = true;
    btn.textContent = "Отправка...";
    errorBlock.style.display = 'none';

    const formData = new FormData();
    formData.append('email', email);

    fetch('/send_recovery_code', {
        method: 'POST',
        body: formData
    })
    .then(async response => {
        const data = await response.json();
        if (!response.ok) throw new Error(data.message || "Ошибка сервера");
        return data;
    })
    .then(data => {
        // ЗАМЕНЯЕМ контент вкладки на поле для ввода кода
        document.getElementById('forgotTab').innerHTML = `
            <div class="auth-form" style="text-align: center;">
                <h2 class="modal__title">Код отправлен!</h2>
                <p class="modal__subtitle">Проверьте почту <b>${email}</b></p>
                <div class="input-group">
                    <input type="text" id="verifyCode" class="form-input" placeholder="Код из письма"
                           inputmode="numeric" maxlength="6"
                           style="text-align: center; font-size: 1.2rem; letter-spacing: 3px;">
                    <div id="verifyCodeError" class="side-error"></div>
                </div>
                <button type="button" class="btn btn--primary btn--full" onclick="verifyCode()">Подтвердить</button>
                <p id="resendTimer" style="margin-top: 12px; font-size: 0.85rem; color: var(--text-muted);">
                    Отправить ещё раз через <span id="timerCount">60</span> сек
                </p>
                <a id="resendLink" href="javascript:void(0)" onclick="resendCode('${email}')"
                   style="display: none; margin-top: 12px; font-size: 0.85rem; color: var(--accent); text-decoration: none;">
                    Отправить ещё раз
                </a>
            </div>
        `;
        const vc = document.getElementById('verifyCode');
        vc.addEventListener('input', () => {
            vc.value = vc.value.replace(/\D/g, '');
            document.getElementById('verifyCodeError').style.display = 'none';
            vc.style.borderColor = 'var(--border)';
        });

        if (_resendInterval) clearInterval(_resendInterval);
        let seconds = 60;
        _resendInterval = setInterval(() => {
            seconds--;
            const el = document.getElementById('timerCount');
            if (el) el.textContent = seconds;
            if (seconds <= 0) {
                clearInterval(_resendInterval);
                _resendInterval = null;
                const t = document.getElementById('resendTimer');
                const l = document.getElementById('resendLink');
                if (t) t.style.display = 'none';
                if (l) l.style.display = 'block';
            }
        }, 1000);
    })
    .catch(err => {
        btn.disabled = false;
        btn.textContent = "Отправить код";
        errorBlock.textContent = err.message;
        errorBlock.style.display = 'block';
        emailInput.style.borderColor = 'var(--danger)';
        console.error("Ошибка восстановления:", err);
    });
}

let _resendInterval = null;

function resetRegistrationButton() {
    const btn = document.getElementById('regSubmitBtn');
    if (btn) {
        btn.disabled = false;
        btn.textContent = 'Зарегистрироваться';
    }
}

function clearAuthTimers() {
    if (_resendInterval) {
        clearInterval(_resendInterval);
        _resendInterval = null;
    }
}

function resendCode(email) {
    const link = document.getElementById('resendLink');
    const timer = document.getElementById('resendTimer');
    const counter = document.getElementById('timerCount');

    // Сразу блокируем кнопку и показываем таймер — до ответа сервера
    if (link) link.style.display = 'none';
    if (timer) timer.style.display = 'block';
    if (counter) counter.textContent = '...';

    const formData = new FormData();
    formData.append('email', email);
    fetch('/send_recovery_code', { method: 'POST', body: formData })
    .then(async r => {
        const data = await r.json();
        if (!r.ok) throw new Error(data.message);

        // Убиваем старый таймер если был
        if (_resendInterval) clearInterval(_resendInterval);

        if (counter) counter.textContent = 60;
        let seconds = 60;
        _resendInterval = setInterval(() => {
            seconds--;
            if (counter) counter.textContent = seconds;
            if (seconds <= 0) {
                clearInterval(_resendInterval);
                _resendInterval = null;
                if (timer) timer.style.display = 'none';
                if (link) link.style.display = 'block';
            }
        }, 1000);
    })
    .catch(err => {
        // Если ошибка — возвращаем кнопку
        if (timer) timer.style.display = 'none';
        if (link) link.style.display = 'block';
        console.error(err);
    });
}

function verifyCode() {
    const codeInput = document.getElementById('verifyCode');
    const code = codeInput.value.trim();
    const errorBlock = document.getElementById('verifyCodeError');
    const formData = new FormData();
    formData.append('code', code);

    fetch('/verify_recovery_code', {
        method: 'POST',
        body: formData
    })
    .then(async response => {
        const data = await response.json();
        if (!response.ok) throw new Error(data.message || "Неверный код");
        return data;
    })
    .then(data => {
        document.getElementById('forgotTab').innerHTML = `
            <div class="auth-form">
                <h2 class="modal__title" style="text-align: left;">Введите новый пароль</h2>

                <div class="passwords-group" style="position: relative;">
                    <div class="input-wrapper">
                        <input type="password" id="newPassword" name="new_password" class="form-input" placeholder="Пароль" required>
                        <span class="password-toggle" onclick="togglePass(this)">○</span>
                    </div>

                    <div class="input-wrapper" style="margin-top: 15px;">
                        <input type="password" id="confirmNewPassword" name="confirm_password" class="form-input" placeholder="Подтвердите пароль" required>
                        <span class="password-toggle" onclick="togglePass(this)">○</span>
                    </div>

                    <div id="newPasswordError" class="side-error double-arrow">Введите пароль</div>
                </div>

                <button type="button" class="btn btn--primary btn--full" style="margin-top: 0px;" onclick="saveNewPassword()">
                    Сохранить
                </button>
            </div>
        `;

        // Слушатели для очистки ошибок
        const p1 = document.getElementById('newPassword');
        const p2 = document.getElementById('confirmNewPassword');
        const err = document.getElementById('newPasswordError');

        [p1, p2].forEach(input => {
            input.addEventListener('input', () => {
                // Сбрасываем тоже через setProperty
                p1.style.setProperty('border-color', 'var(--border)', 'important');
                p2.style.setProperty('border-color', 'var(--border)', 'important');
                err.style.display = 'none';
            });
        });
    })
    .catch(err => {
        if (errorBlock) {
            errorBlock.textContent = err.message;
            errorBlock.style.display = 'block';
            codeInput.style.borderColor = 'var(--danger)';
        }
    });
}

// Функция сохранения пароля
function saveNewPassword() {
    const p1_input = document.getElementById('newPassword');
    const p2_input = document.getElementById('confirmNewPassword');
    const errorBlock = document.getElementById('newPasswordError');
    const serverMsg = document.getElementById('serverErrorMessage');

    if (!p1_input || !p2_input) return;

    const p1 = p1_input.value;
    const p2 = p2_input.value;

    // 1. СБРОС СТИЛЕЙ (как в регистрации)
    p1_input.style.borderColor = 'var(--border)';
    p2_input.style.borderColor = 'var(--border)';
    errorBlock.style.display = 'none';

    let hasError = false;
    let message = "";

    // 2. ПРОВЕРКА ЛОГИКИ (убираем блокирующие else if)
    if (p1 === "") {
        message = "Введите пароль";
        hasError = true;
    } else if (p1.length < 8) {
        message = "Минимум 8 символов";
        hasError = true;
    } else if (p1 !== p2) {
        message = "Пароли не совпадают";
        hasError = true;
    }

    // 3. ПОДСВЕТКА (выполняется для ОБОИХ полей при любой ошибке выше)
    if (hasError) {
        errorBlock.textContent = message;
        errorBlock.style.display = 'block';

        // МЕНЯЕМ ЗДЕСЬ: используем cssText, чтобы перебить :focus и другие стили
        p1_input.style.setProperty('border-color', 'var(--danger)', 'important');
        p2_input.style.setProperty('border-color', 'var(--danger)', 'important');

        // Дополнительно: убираем фокус, чтобы :focus не перебивал цвет
        p1_input.blur();
        p2_input.blur();
        return;
    }

    // 4. ОТПРАВКА
    const formData = new FormData();
    formData.append('password', p1);

    fetch('/reset_password', {
        method: 'POST',
        body: formData
    })
    .then(response => response.json())
    .then(data => {
        if (data.status === 'success') {
            resetAuthForms();
            showTab('login');
            if (serverMsg) {
                serverMsg.removeAttribute('style');
                serverMsg.textContent = "Пароль успешно изменен! Войдите.";
                serverMsg.style.display = 'block';
                serverMsg.style.backgroundColor = 'var(--success)';
                serverMsg.style.color = 'white';
                setTimeout(() => serverMsg.style.display = 'none', 3000);
            }
        }
    })
    .catch(err => console.error("Ошибка:", err));
}

function resetAuthForms() {
    const modal = document.getElementById('authModal');
    if (!modal) return;

    modal.querySelectorAll('input[type="text"]').forEach(input => {
        // Находим только те поля, которые должны быть паролями (по ID или родителю)
        if (input.id.toLowerCase().includes('pass') || input.name.toLowerCase().includes('pass')) {
            input.type = 'password';
        }
    });

    modal.querySelectorAll('.password-toggle').forEach(btn => {
        btn.textContent = '○';
    });

    // 1. Сбрасываем значения всех полей ввода
    modal.querySelectorAll('form').forEach(f => f.reset());

    clearAuthTimers();
    resetRegistrationButton();

    // Возвращаем обычную форму регистрации, если перед этим показывался ввод кода
    const regFormReset = document.getElementById('registerForm');
    const regVerifyTabReset = document.getElementById('registrationVerifyTab');
    if (regFormReset) regFormReset.style.display = 'flex';
    if (regVerifyTabReset) regVerifyTabReset.innerHTML = '';

    // 2. Скрываем все сообщения об ошибках
    modal.querySelectorAll('.side-error').forEach(e => e.style.display = 'none');
    const serverErr = document.getElementById('serverErrorMessage');
    if (serverErr) serverErr.style.display = 'none';

    // 3. Возвращаем стандартный цвет границ инпутов
    modal.querySelectorAll('.form-input').forEach(i => i.style.borderColor = 'var(--border)');

    // 4. ВОССТАНАВЛИВАЕМ содержимое вкладки восстановления пароля
    // Это важно, так как при отправке кода HTML внутри этой вкладки меняется
    const forgotTab = document.getElementById('forgotTab');
    if (forgotTab) {
        forgotTab.innerHTML = `
            <div class="auth-form">
                <h2 class="modal__title">Восстановление доступа</h2>
                <p class="modal__subtitle">Введите почту, указанную при регистрации</p>
                <div class="input-group">
                    <input type="email" id="forgotEmail" class="form-input" placeholder="Email" required>
                    <div id="forgotEmailError" class="side-error"></div>
                </div>
                <button type="button" class="btn btn--primary btn--full" onclick="handleForgot(event)">
                    Отправить код
                </button>
                <div class="modal__footer">
                    <a href="#" class="modal__link" onclick="showTab('login')">Назад</a>
                </div>
            </div>
        `;
    }

    // 5. Показываем шапку табов (Вход / Регистрация), если она была скрыта
    const tabsHeader = document.querySelector('.modal__tabs');
    if (tabsHeader) tabsHeader.style.display = 'flex';

    // 6. Переключаем активную вкладку на "Вход"
    document.querySelectorAll('.modal__tab-content').forEach(c => c.classList.remove('active'));
    document.getElementById('loginTab').classList.add('active');

    // 7. Подсвечиваем кнопку "Вход" в шапке модалки
    document.querySelectorAll('.modal__tab').forEach(t => t.classList.remove('active'));
    const loginBtn = document.getElementById('loginTabBtn');
    if (loginBtn) loginBtn.classList.add('active');
}

// ─────────────────────────────────────────────
// УВЕДОМЛЕНИЯ: колокольчик, панель, тост о скором сеансе
// ─────────────────────────────────────────────

let notifData = { items: [], unread: 0 };

function notifTimeAgo(iso) {
    const t = new Date(iso.indexOf('T') === -1 ? iso.replace(' ', 'T') : iso);
    const diff = Math.max(0, (Date.now() - t.getTime()) / 1000);
    if (diff < 60) return 'только что';
    if (diff < 3600) return Math.floor(diff / 60) + ' мин назад';
    if (diff < 86400) return Math.floor(diff / 3600) + ' ч назад';
    return t.toLocaleDateString('ru-RU', { day: 'numeric', month: 'numeric' });
}

function notifIcon(type) {
    if (type === 'session_reminder') return '🎬';
    if (type === 'payment') return '🎟️';
    if (type === 'booking') return '📌';
    if (type === 'loyalty') return '⭐';
    return '🔔';
}

function escapeHtml(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function renderNotifList() {
    const list = document.getElementById('notifList');
    const badge = document.getElementById('notifBadge');
    if (!list) return;

    if (badge) {
        if (notifData.unread > 0) {
            badge.textContent = notifData.unread > 99 ? '99+' : notifData.unread;
            badge.style.display = 'flex';
        } else {
            badge.style.display = 'none';
        }
    }

    if (!notifData.items.length) {
        list.innerHTML = '<div style="padding:1.4rem; text-align:center; color:var(--text-muted); font-size:0.85rem;">Уведомлений нет</div>';
        return;
    }

    list.innerHTML = notifData.items.map(n => {
        const unreadDot = n.read ? '' : '<span class="notif-dot"></span>';
        // Передаём в openNotif объект с link и booking_id (без inline-строк,
        // чтобы не ловить кавычки в ссылках/заголовках).
        const notif = JSON.stringify({ link: n.link || '', booking_id: n.booking_id || '' })
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        return '<div class="notif-item' + (n.read ? '' : ' notif-item--new') + '" onclick="openNotif(' + n.id + ', ' + notif + ')">' +
            '<div class="notif-item__icon">' + notifIcon(n.type) + '</div>' +
            '<div class="notif-item__body">' +
                '<div class="notif-item__title">' + escapeHtml(n.title) + '</div>' +
                (n.body ? '<div class="notif-item__text">' + escapeHtml(n.body) + '</div>' : '') +
                '<div class="notif-item__time">' + notifTimeAgo(n.created_at) + '</div>' +
            '</div>' +
            unreadDot +
        '</div>';
    }).join('');
}

function toggleNotifPanel(event) {
    if (event) event.stopPropagation();
    const panel = document.getElementById('notifPanel');
    if (!panel) return;
    panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
}

function closeNotifPanel() {
    const panel = document.getElementById('notifPanel');
    if (panel) panel.style.display = 'none';
}

function openNotif(id, data) {
    // Совместимость: раньше сюда передавалась строка-ссылка.
    if (typeof data === 'string') {
        data = { link: data, booking_id: '' };
    }
    data = data || {};
    const link = data.link || '';
    const bookingId = data.booking_id || '';

    fetch('/api/notifications/read', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: [id] })
    }).then(() => notifFetch());

    // Уведомление о билете (бронь/оплата/напоминание) — открываем ту же
    // модалку билета, что и в личном кабинете. Она теперь в base.html,
    // поэтому работает с любой страницы.
    if (bookingId && typeof openTicketModal === 'function') {
        openTicketModal(null, parseInt(bookingId, 10), false);
        closeNotifPanel();
        return;
    }

    // Обычные уведомления (акции, лояльность) — просто переход по ссылке.
    if (link && link.indexOf('/') === 0) {
        window.location.href = link;
    }
}

function markAllNotifRead() {
    fetch('/api/notifications/read', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ all: true })
    }).then(() => notifFetch());
}

function notifFetch() {
    fetch('/api/notifications')
        .then(r => r.json())
        .then(data => {
            notifData = data;
            renderNotifList();
            maybeShowSessionToast(data.items);
        })
        .catch(() => {});
}

function maybeShowSessionToast(items) {
    // Тост появляется, только если сеанс начнётся в течение 30 минут,
    // и это напоминание ещё не показывалось в этой вкладке.
    const reminder = (items || []).filter(n => n.type === 'session_reminder' && n.minutes_until != null && n.minutes_until <= 30)[0];
    if (!reminder) return;
    const seen = JSON.parse(sessionStorage.getItem('mirkino_toasts') || '[]');
    if (seen.indexOf(reminder.id) !== -1 || !reminder.link) return;
    seen.push(reminder.id);
    sessionStorage.setItem('mirkino_toasts', JSON.stringify(seen));

    let toast = document.getElementById('sessionToast');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = 'sessionToast';
        toast.style.cssText = 'position:fixed; bottom:24px; right:24px; z-index:9999; background:var(--bg-card); border:1px solid var(--accent); border-radius:var(--radius); padding:1rem 1.2rem; max-width:320px; box-shadow:var(--shadow); cursor:pointer;';
        document.body.appendChild(toast);
    }
    toast.innerHTML = '<div style="font-size:0.85rem; color:var(--text-muted); margin-bottom:0.3rem;">⏰ Скоро сеанс</div>' +
        '<div style="font-size:0.95rem; font-weight:800; line-height:1.35;">' + escapeHtml(reminder.title) + '</div>' +
        '<div style="font-size:0.8rem; color:var(--text-muted); margin-top:0.4rem;">' + escapeHtml(reminder.body) + '</div>';
    toast.style.display = 'block';
    toast.onclick = function () {
        window.location.href = reminder.link;
    };
    setTimeout(() => { toast.style.display = 'none'; }, 15000);
}

// Закрытие панели по клику вне её
document.addEventListener('click', function (e) {
    const wrap = document.querySelector('.notif-wrap');
    if (wrap && !wrap.contains(e.target)) closeNotifPanel();
});
document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeNotifPanel();
});

// Первичная загрузка и периодическое обновление
document.addEventListener('DOMContentLoaded', function () {
    if (document.getElementById('notifBell')) {
        notifFetch();
        setInterval(notifFetch, 60000);
    }
});

// ─────────────────────────────────────────────
// МОДАЛКА БИЛЕТА (общая для ЛК и уведомлений)
// ─────────────────────────────────────────────

let ticketModalData = null;

function openTicketModal(event, bookingId, isPast) {
    // Посещённые/прошедшие билеты не открываются
    if (isPast) return;
    if (event && event.target.closest('button, a, form, input, select, .fav-star')) return;

    const statusEl = document.getElementById('ticketModalStatus');
    const titleEl = document.getElementById('ticketModalTitle');
    statusEl.style.borderColor = 'var(--border)';
    statusEl.style.color = 'var(--text-muted)';
    titleEl.textContent = '';
    document.getElementById('ticketModal').style.display = 'flex';

    fetch(`/api/ticket/${bookingId}`)
    .then(r => r.json())
    .then(data => {
        if (data.error) throw new Error(data.error);
        ticketModalData = data;

        // Постер как в письме (320×500)
        const poster = document.getElementById('ticketModalPoster');
        const noPoster = document.getElementById('ticketModalNoPoster');
        if (data.poster) {
            poster.src = '/static/posters/' + data.poster;
            poster.style.display = 'block';
            noPoster.style.display = 'none';
        } else {
            poster.style.display = 'none';
            noPoster.style.display = 'flex';
            noPoster.textContent = data.title[0].toUpperCase();
        }

        // Статус
        const isPaid = data.status === 'paid';
        const lbl = isPaid ? 'Куплен' : 'Забронирован';
        const color = isPaid ? 'var(--success)' : 'var(--accent)';
        statusEl.textContent = lbl;
        statusEl.style.borderColor = color;
        statusEl.style.color = color;
        titleEl.textContent = data.title;

        // Строки как в письме: Дата | Время | Зал | Места | Оплата | (Акция) | Сумма
        const payTxt = data.payment_method === 'bonus' ? 'Бонусная карта'
                     : (data.payment_method === 'bank' ? 'Банковская карта' : '—');
        const rows = [
            ['Дата', data.date_text],
            ['Время', data.time],
            ['Зал', data.hall_name],
            ['Места', `ряд ${data.row_num}, место ${data.seat_num}`],
            ['Оплата', payTxt],
        ];
        if (data.promo_title && data.promo_discount) {
            rows.push(['Акция', `${data.promo_title} −${data.promo_discount}%`]);
        }
        rows.push(['Сумма', `${data.price} ₽`]);
        const table = document.getElementById('ticketModalInfoTable');
        table.innerHTML = rows.map(r => `
            <tr>
                <td style="padding:7px 0; color:#7a8399; width:130px;">${escapeHtml(r[0])}</td>
                <td style="padding:7px 0; color:#e8eaf0; font-weight:700;">${escapeHtml(r[1])}</td>
            </tr>`).join('');

        document.getElementById('ticketModalMsg').style.display = 'none';

        // QR только для оплаченных (прошедшие сюда не попадают)
        const qrBlock = document.getElementById('ticketModalQRBlock');
        const qrImg = document.getElementById('ticketModalQR');
        if (isPaid && data.qr) {
            qrImg.src = 'data:image/png;base64,' + data.qr;
            qrBlock.style.display = 'block';
        } else {
            qrBlock.style.display = 'none';
        }

        // Действия — в самый низ модалки
        const bookedActions = document.getElementById('ticketModalBookedActions');
        const paidActions = document.getElementById('ticketModalPaidActions');
        const cancelForm = document.getElementById('ticketModalCancelForm');
        const refundForm = document.getElementById('ticketModalRefundForm');
        cancelForm.action = `/cancel_booking/${data.id}`;
        refundForm.action = `/refund_ticket/${data.id}`;

        if (data.status === 'booked') {
            bookedActions.style.display = 'flex';
            paidActions.style.display = 'none';
            document.getElementById('ticketModalPayBtn').style.display = data.can_pay ? '' : 'none';
            cancelForm.style.display = data.can_cancel ? '' : 'none';
        } else if (isPaid) {
            bookedActions.style.display = 'none';
            paidActions.style.display = data.can_return ? 'flex' : 'none';
        } else {
            bookedActions.style.display = 'none';
            paidActions.style.display = 'none';
        }
    })
    .catch(err => {
        document.getElementById('ticketModal').style.display = 'none';
        alert(err.message || 'Не удалось загрузить билет');
    });
}

function closeTicketModal(event) {
    if (!event || event.target === document.getElementById('ticketModal')) {
        document.getElementById('ticketModal').style.display = 'none';
    }
}

function ticketModalPay() {
    if (!ticketModalData) return;
    const d = ticketModalData;
    if (typeof openBookedPaymentModal === 'function') {
        openBookedPaymentModal(d.id, d.session_id, d.seat_id, d.row_num, d.seat_num,
                               d.price, d.title, d.date_text, d.time, d.hall_name);
    } else {
        // Модалка оплаты живёт в ЛК — уходим туда, если открыли билет с другой страницы.
        window.location.href = '/cabinet?ticket=' + encodeURIComponent(d.id);
    }
}

// При открытии /cabinet?ticket=ID (переход из уведомления) показываем билет сразу.
document.addEventListener('DOMContentLoaded', function () {
    const urlParams = new URLSearchParams(window.location.search);
    const ticketId = urlParams.get('ticket');
    if (ticketId && document.getElementById('ticketModal') && typeof openTicketModal === 'function') {
        openTicketModal(null, parseInt(ticketId, 10), false);
    }
});

// ─────────────────────────────────────────────
// МОДАЛКА АВТОРИЗАЦИИ: открытие, вкладки, показ пароля
// ─────────────────────────────────────────────

// 1. Общие функции модалки
function openAuthModal() {
    resetAuthForms(); // Очистка перед показом
    document.getElementById('authModal').style.display = 'flex';
}

function closeAuthModal() {
    document.getElementById('authModal').style.display = 'none';
    resetAuthForms();
}

function showTab(tabName) {
    resetAuthForms();

    // 1. Находим блок с кнопками "Вход" и "Регистрация"
    const tabsHeader = document.querySelector('.modal__tabs');

    // 2. Если переходим в 'forgot', скрываем шапку, иначе показываем
    if (tabName === 'forgot' || tabName === 'registrationVerify') {
        if (tabsHeader) tabsHeader.style.display = 'none';
    } else {
        if (tabsHeader) tabsHeader.style.display = 'flex';
    }

    // Стандартная логика переключения
    document.querySelectorAll('.modal__tab-content').forEach(c => c.classList.remove('active'));
    document.getElementById(tabName + 'Tab').classList.add('active');

    document.querySelectorAll('.modal__tab').forEach(t => t.classList.remove('active'));
    const activeBtn = document.getElementById(tabName + 'TabBtn');
    if (activeBtn) activeBtn.classList.add('active');
}

window.onclick = function(event) {
    let modal = document.getElementById('authModal');
    if (event.target == modal) closeAuthModal();
}

// 2. Показ/скрытие пароля
function togglePass(inputOrId) {
    let input, btn;

    if (typeof inputOrId === 'string') {
        // Логика для старых форм (Вход/Регистрация)
        input = document.getElementById(inputOrId);
        btn = input.nextElementSibling;
    } else {
        // Логика для новой формы (через 'this')
        btn = inputOrId;
        input = btn.parentElement.querySelector('input');
    }

    if (input.type === 'password') {
        input.type = 'text';
        btn.textContent = '◉';
    } else {
        input.type = 'password';
        btn.textContent = '○';
    }
}

// 3. Основная логика
document.addEventListener('DOMContentLoaded', function() {
    const regForm = document.getElementById('registerForm');
    const nameInput = document.getElementById('regName');
    const emailInput = document.getElementById('regEmail');
    const passInput = document.getElementById('regPassword');
    const confirmInput = document.getElementById('confirmPassword');
    const agreeCheckbox = document.getElementById('agreeTerms');

    const nameError = document.getElementById('regNameError');
    const emailError = document.getElementById('regEmailError');
    const passError = document.getElementById('passwordError');
    const agreeError = document.getElementById('agreeError');

    // Очистка ошибок при вводе
    [nameInput, emailInput, passInput, confirmInput, agreeCheckbox].forEach(el => {
        if (!el) return;
        el.addEventListener('input', () => {
            el.style.borderColor = 'var(--border)';
            if (el.id === 'regName') nameError.style.display = 'none';
            if (el.id === 'regEmail') emailError.style.display = 'none';
            if (el.id === 'regPassword' || el.id === 'confirmPassword') {
                passError.style.display = 'none';
                passInput.style.borderColor = 'var(--border)';
                confirmInput.style.borderColor = 'var(--border)';
            }
            if (el.id === 'agreeTerms') agreeError.style.display = 'none';
        });
    });

    if (regForm) {
        regForm.addEventListener('submit', function(e) {
            e.preventDefault();
            let hasError = false;
            const emailRegex = /^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$/;

            // Сбрасываем старые ошибки
            [nameInput, emailInput, passInput, confirmInput].forEach(input => {
                if (input) input.style.borderColor = 'var(--border)';
            });
            [nameError, emailError, passError, agreeError].forEach(err => {
                if (err) err.style.display = 'none';
            });

            // 1. Проверка Имени
            if (nameInput.value.trim() === "") {
                nameError.textContent = "Введите имя";
                nameError.style.display = 'block';
                nameInput.style.borderColor = 'var(--danger)';
                hasError = true;
            }

            // 2. Проверка Email
            if (emailInput.value.trim() === "") {
                emailError.textContent = "Введите почту";
                emailError.style.display = 'block';
                emailInput.style.borderColor = 'var(--danger)';
                hasError = true;
            } else if (!emailRegex.test(emailInput.value)) {
                emailError.textContent = "Некорректный формат почты";
                emailError.style.display = 'block';
                emailInput.style.borderColor = 'var(--danger)';
                hasError = true;
            }

            // 3. Проверка Паролей
            const p1 = passInput.value;
            const p2 = confirmInput.value;

            if (p1 === "") {
                passError.textContent = "Введите пароль";
                passError.style.display = 'block';
                passInput.style.borderColor = 'var(--danger)';
                confirmInput.style.borderColor = 'var(--danger)';
                hasError = true;
            } else if (p1.length < 8) {
                passError.textContent = "Минимум 8 символов";
                passError.style.display = 'block';
                passInput.style.borderColor = 'var(--danger)';
                confirmInput.style.borderColor = 'var(--danger)';
                hasError = true;
            } else if (p1 !== p2) {
                passError.textContent = "Пароли не совпадают";
                passError.style.display = 'block';
                passInput.style.borderColor = 'var(--danger)';
                confirmInput.style.borderColor = 'var(--danger)';
                hasError = true;
            }

            // 4. Проверка согласия
            if (!agreeCheckbox.checked) {
                agreeError.style.display = 'block';
                hasError = true;
            }

            if (hasError) return;

            const btn = document.getElementById('regSubmitBtn');
            if (btn) {
                btn.disabled = true;
                btn.textContent = 'Отправка кода...';
            }

            const formData = new FormData(regForm);
            fetch('/send_registration_code', { method: 'POST', body: formData })
            .then(async response => {
                const data = await response.json();
                if (!response.ok) throw new Error(data.message || 'Ошибка сервера');
                return data;
            })
            .then(() => {
                window.renderRegistrationCodeForm(emailInput.value.trim());
            })
            .catch(err => {
                const serverErr = document.getElementById('serverErrorMessage');
                if (serverErr) {
                    serverErr.textContent = err.message;
                    serverErr.style.display = 'block';
                    serverErr.style.top = (document.querySelector('.modal__content').getBoundingClientRect().top - document.getElementById('authModal').getBoundingClientRect().top - serverErr.offsetHeight - 10) + 'px';
                }
                if (err.message.includes('Email')) {
                    emailError.textContent = err.message;
                    emailError.style.display = 'block';
                    emailInput.style.borderColor = 'var(--danger)';
                }
                if (btn) {
                    btn.disabled = false;
                    btn.textContent = 'Зарегистрироваться';
                }
            });
        });
    }

window.renderRegistrationCodeForm = function(email) {
    const verifyTab = document.getElementById('registrationVerifyTab');
    const tabsHeader = document.querySelector('.modal__tabs');
    if (!verifyTab) return;

    resetRegistrationButton();
    if (tabsHeader) tabsHeader.style.display = 'none';

    document.querySelectorAll('.modal__tab-content').forEach(c => c.classList.remove('active'));
    verifyTab.classList.add('active');
    document.querySelectorAll('.modal__tab').forEach(t => t.classList.remove('active'));

    verifyTab.innerHTML = `
        <div class="auth-form" style="text-align: center;">
            <h2 class="modal__title">Код отправлен!</h2>
            <p class="modal__subtitle">Проверьте почту <b>${email}</b></p>
            <div class="input-group">
                <input type="text" id="registrationCode" class="form-input" placeholder="Код из письма"
                       inputmode="numeric" maxlength="6"
                       style="text-align: center; font-size: 1.2rem; letter-spacing: 3px;">
                <div id="registrationCodeError" class="side-error"></div>
            </div>
            <button type="button" class="btn btn--primary btn--full" onclick="verifyRegistrationCode()">Подтвердить</button>
            <p id="regResendTimer" style="margin-top: 12px; font-size: 0.85rem; color: var(--text-muted);">
                Отправить ещё раз через <span id="regTimerCount">60</span> сек
            </p>
            <a id="regResendLink" href="javascript:void(0)" onclick="resendRegistrationCode()"
               style="display: none; margin-top: 12px; font-size: 0.85rem; color: var(--accent); text-decoration: none;">
                Отправить ещё раз
            </a>
        </div>
    `;

    const codeInput = document.getElementById('registrationCode');
    if (codeInput) {
        codeInput.focus();
        codeInput.addEventListener('input', () => {
            codeInput.value = codeInput.value.replace(/\D/g, '');
            const err = document.getElementById('registrationCodeError');
            if (err) err.style.display = 'none';
            codeInput.style.borderColor = 'var(--border)';
        });
    }

    startRegistrationResendTimer();
};

window.startRegistrationResendTimer = function() {
    if (_resendInterval) clearInterval(_resendInterval);
    let seconds = 60;
    _resendInterval = setInterval(() => {
        seconds--;
        const counter = document.getElementById('regTimerCount');
        if (counter) counter.textContent = seconds;
        if (seconds <= 0) {
            clearInterval(_resendInterval);
            _resendInterval = null;
            const timer = document.getElementById('regResendTimer');
            const link = document.getElementById('regResendLink');
            if (timer) timer.style.display = 'none';
            if (link) link.style.display = 'block';
        }
    }, 1000);
};

window.verifyRegistrationCode = function() {
    const codeInput = document.getElementById('registrationCode');
    const errorBlock = document.getElementById('registrationCodeError');
    if (!codeInput) return;

    const code = codeInput.value.trim();
    if (!code) {
        errorBlock.textContent = 'Введите код';
        errorBlock.style.display = 'block';
        codeInput.style.borderColor = 'var(--danger)';
        return;
    }

    const formData = new FormData();
    formData.append('code', code);

    fetch('/verify_registration_code', { method: 'POST', body: formData })
    .then(async response => {
        const data = await response.json();
        if (!response.ok) throw new Error(data.message || 'Неверный код');
        return data;
    })
    .then(data => {
        window.location.href = data.redirect || '/';
    })
    .catch(err => {
        errorBlock.textContent = err.message;
        errorBlock.style.display = 'block';
        codeInput.style.borderColor = 'var(--danger)';
    });
};

window.resendRegistrationCode = function() {
    const link = document.getElementById('regResendLink');
    const timer = document.getElementById('regResendTimer');
    const counter = document.getElementById('regTimerCount');
    const errorBlock = document.getElementById('registrationCodeError');

    if (link) link.style.display = 'none';
    if (timer) timer.style.display = 'block';
    if (counter) counter.textContent = '...';
    if (errorBlock) errorBlock.style.display = 'none';

    fetch('/resend_registration_code', { method: 'POST' })
    .then(async response => {
        const data = await response.json();
        if (!response.ok) throw new Error(data.message || 'Ошибка сервера');
        return data;
    })
    .then(() => startRegistrationResendTimer())
    .catch(err => {
        if (timer) timer.style.display = 'none';
        if (link) link.style.display = 'block';
        if (errorBlock) {
            errorBlock.textContent = err.message;
            errorBlock.style.display = 'block';
        }
    });
};

    // ЭЛЕМЕНТЫ ВХОДА
    const loginForm = document.getElementById('loginForm');
    const loginEmail = document.getElementById('loginEmail');
    const loginPass = document.getElementById('loginPassword');
    const loginEmailError = document.getElementById('loginEmailError');
    const loginPassError = document.getElementById('loginPasswordError');

    // Очистка ошибок входа при вводе
    [loginEmail, loginPass].forEach(el => {
        if (!el) return;
        el.addEventListener('input', () => {
            el.style.borderColor = 'var(--border)';
            if (el.id === 'loginEmail') loginEmailError.style.display = 'none';
            if (el.id === 'loginPassword') loginPassError.style.display = 'none';
        });
    });

    // Валидация формы входа
    if (loginForm) {
        loginForm.addEventListener('submit', function(e) {
            let hasError = false;
            const emailRegex = /^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$/;

            if (!loginEmail.value.trim()) {
                loginEmailError.textContent = "Введите почту";
                loginEmailError.style.display = 'block';
                loginEmail.style.borderColor = 'var(--danger)';
                hasError = true;
            } else if (!emailRegex.test(loginEmail.value)) {
                loginEmailError.textContent = "Некорректный формат почты";
                loginEmailError.style.display = 'block';
                loginEmail.style.borderColor = 'var(--danger)';
                hasError = true;
            }

            if (!loginPass.value.trim()) {
                loginPassError.textContent = "Введите пароль";
                loginPassError.style.display = 'block';
                loginPass.style.borderColor = 'var(--danger)';
                hasError = true;
            }

            if (hasError) e.preventDefault();
        });
    }

    // Автооткрытие модалки по параметру ?login=1 (кнопка «Войдите чтобы купить билет»)
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.get('login') === '1') {
        const authModal = document.getElementById('authModal');
        if (authModal) {
            authModal.style.display = 'flex';
            if (typeof showTab === 'function') {
                showTab('login'); // Переключаем на вкладку "Вход"
            }

            // КРАЙНЕ ВАЖНО: Очищаем URL от параметра ?login=1 без перезагрузки страницы
            const cleanUrl = window.location.protocol + "//" + window.location.host + window.location.pathname;
            window.history.replaceState({ path: cleanUrl }, '', cleanUrl);
        }
    }
});