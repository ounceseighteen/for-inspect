// ─────────────────────────────────────────────
// Покупка билетов с главной / страницы фильмов:
// модалка выбора фильма/даты/времени и схема зала.
// Подключается из templates/user/index.html
// (второй блок) и templates/user/films.html.
// ─────────────────────────────────────────────

let cfDatesData = {};
let cfSelectedSeatId = null;
let cfSelectedSessionId = null;
let cfCurrentSessionPrice = 0;
const cfIsLoggedIn = window.CF_IS_LOGGED_IN === true;
const cfIsClient = window.CF_IS_CLIENT === true;

function toggleFavorite(event, filmId, btn) {
    event.stopPropagation();
    if (!cfIsClient) return;
    fetch(`/api/favorite/${filmId}`, { method: 'POST' })
    .then(r => r.json())
    .then(data => {
        btn.classList.toggle('active', !!data.favorite);
        btn.textContent = data.favorite ? '★' : '☆';
    })
    .catch(() => {});
}

function openFilmModal(filmId, todayOnly) {
    cfSelectedSeatId = null;
    cfSelectedSessionId = null;
    document.getElementById('cfTimesBlock').style.display = 'none';
    document.getElementById('cfDates').innerHTML = '';
    document.getElementById('cfTimes').innerHTML = '';
    document.getElementById('cfPickerModal').style.display = 'flex';

    fetch(`/api/film/${filmId}`)
    .then(r => r.json())
    .then(data => {
        cfDatesData = data.dates;

        if (data.poster) {
            document.getElementById('cfPoster').src = `/static/posters/${data.poster}`;
            document.getElementById('cfPoster').style.display = 'block';
            document.getElementById('cfNoPoster').style.display = 'none';
        } else {
            document.getElementById('cfPoster').style.display = 'none';
            document.getElementById('cfNoPoster').style.display = 'flex';
            document.getElementById('cfNoPoster').textContent = data.title[0].toUpperCase();
        }

        document.getElementById('cfTitle').textContent = data.title;
        document.getElementById('cfGenre').textContent = data.genre;
        document.getElementById('cfDuration').textContent = data.duration + ' мин.';
        document.getElementById('cfDesc').textContent = data.description;

        const now = new Date();
        const today = `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')}`;

        if (todayOnly) {
            document.getElementById('cfDatesBlock').style.display = 'none';
            if (data.dates[today]) {
                cfShowTimes(data.dates[today].sessions);
            } else {
                document.getElementById('cfTimesBlock').style.display = 'block';
                document.getElementById('cfTimes').innerHTML = '<p style="color:var(--text-muted); font-size:0.85rem;">Сеансов на сегодня нет</p>';
            }
        } else {
            document.getElementById('cfDatesBlock').style.display = 'block';
            const datesDiv = document.getElementById('cfDates');
            datesDiv.innerHTML = '';
            if (!Object.keys(data.dates).length) {
                datesDiv.innerHTML = '<p style="color:var(--text-muted); font-size:0.85rem;">Нет доступных сеансов</p>';
                return;
            }
            Object.entries(data.dates).forEach(([dateStr, info]) => {
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.innerHTML = `<span style="font-size:0.9rem; font-weight:600;">${info.label}</span><br>
                                 <span style="font-size:0.7rem; color:var(--text-muted);">${info.weekday}</span>`;
                btn.style.cssText = 'padding:8px 12px; border:1px solid var(--border); border-radius:var(--radius); background:var(--bg-card); color:var(--text); cursor:pointer; text-align:center; min-width:60px;';
                btn.onclick = () => {
                    document.querySelectorAll('#cfDates button').forEach(b => {
                        b.style.borderColor = 'var(--border)';
                        b.style.background = 'var(--bg-card)';
                    });
                    btn.style.borderColor = 'var(--accent)';
                    btn.style.background = 'rgba(124,92,255,0.12)';
                    cfShowTimes(info.sessions);
                };
                datesDiv.appendChild(btn);
            });
        }
    });
}

function cfShowTimes(sessions) {
    const timesDiv = document.getElementById('cfTimes');
    timesDiv.innerHTML = '';
    document.getElementById('cfTimesBlock').style.display = 'block';

    sessions.forEach(s => {
        const wrap = document.createElement('div');
        wrap.style.cssText = 'display:flex; flex-direction:column; align-items:center; gap:2px;';
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.innerHTML = `<span style="font-size:0.95rem; font-weight:600;">${escapeHtml(s.time)}</span><br>
                         <span style="font-size:0.7rem; color:var(--text-muted);">${escapeHtml(s.price)} ₽</span>`;
        btn.style.cssText = 'padding:8px 14px; border:1px solid var(--border); border-radius:var(--radius); background:var(--bg-card); color:var(--text); cursor:pointer; text-align:center; min-width:80px;';
        btn.onclick = () => {
            document.querySelectorAll('#cfTimes button').forEach(b => {
                b.style.borderColor = 'var(--border)';
                b.style.background = 'var(--bg-card)';
            });
            btn.style.borderColor = 'var(--accent)';
            btn.style.background = 'rgba(124,92,255,0.12)';
            cfOpenSeatModal(s);
        };
        const fmt = document.createElement('div');
        fmt.textContent = s.format;
        fmt.style.cssText = 'font-size:0.7rem; color:var(--text-muted); text-align:center;';
        wrap.appendChild(btn);
        wrap.appendChild(fmt);
        timesDiv.appendChild(wrap);
    });
}

function cfOpenSeatModal(sessionData) {
    cfSelectedSessionId = sessionData.id;
    cfSelectedSeatId = null;
    window.cfSelectedSeats = []; // массив выбранных мест
    cfCurrentSessionData = sessionData;
    cfCurrentSessionPrice = Number(sessionData.price || 0);
    document.getElementById('cfBtnPay').disabled = true;
    document.getElementById('cfBtnBook').disabled = true;
    document.getElementById('cfSelectedSeatText').style.display = 'none';
    document.getElementById('cfSelectedSeatText').textContent = '';
    document.getElementById('cfPromoSelect').value = '';
    const paymentMethod = document.getElementById('cfPaymentMethod');
    if (paymentMethod) paymentMethod.value = '';
    cfUpdateTotalPrice();
    document.getElementById('cfMsg').style.display = 'none';
    document.getElementById('cfSeatMap').innerHTML = '<p style="color:var(--text-muted)">Загрузка...</p>';

    const title = document.getElementById('cfTitle').textContent;
    document.getElementById('cfSeatTitle').textContent = title;

    // Формат даты дд.мм.гггг в чч:мм
    const dateParts = sessionData.date ? sessionData.date.split('-').reverse().join('.') : '';
    // A05-5 аудита: данные зала/времени/цены приходят с сервера, но могут
    // быть изменены админом или БД — экранируем перед вставкой в innerHTML.
    document.getElementById('cfSeatInfo').innerHTML =
        `<b>${escapeHtml(sessionData.hall_name)}</b><br>${escapeHtml(dateParts)} в ${escapeHtml(sessionData.time)}<br>Цена: <b style="color:var(--accent)">${escapeHtml(sessionData.price)} ₽</b>`;

    document.getElementById('cfPickerModal').style.display = 'none';
    document.getElementById('cfSeatModal').style.display = 'flex';

    fetch(`/api/session_seats/${sessionData.id}`)
    .then(r => r.json())
    .then(data => {
        const map = document.getElementById('cfSeatMap');
        map.innerHTML = '';
        const sortedRows = Object.keys(data.rows).map(Number).sort((a,b) => a-b);
        sortedRows.forEach(rowNum => {
            const rowDiv = document.createElement('div');
            rowDiv.style.cssText = 'display:flex; gap:4px; align-items:center; width:100%;';
            const lbl = document.createElement('span');
            lbl.textContent = rowNum;
            lbl.style.cssText = 'font-size:0.75rem; color:var(--text-muted); width:16px; text-align:right; flex-shrink:0;';
            rowDiv.appendChild(lbl);
            const seatsWrap = document.createElement('div');
            seatsWrap.style.cssText = 'display:flex; gap:4px; flex:1; justify-content:center;';
            data.rows[rowNum].forEach(seat => {
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.textContent = seat.num;
                btn.style.cssText = 'width:28px; height:28px; font-size:0.7rem; border-radius:4px; border:1px solid var(--accent); cursor:pointer; background:var(--bg-card); color:var(--text);';
                if (seat.booked) {
                    btn.style.background = 'var(--border)';
                    btn.style.borderColor = 'var(--border)';
                    btn.style.color = 'var(--text-muted)';
                    btn.style.cursor = 'not-allowed';
                } else {
                    btn.dataset.seatId = seat.id;
                    btn.dataset.rowNum = rowNum;
                    btn.dataset.seatNum = seat.num;
                    btn.onclick = () => {
                        const idx = window.cfSelectedSeats.findIndex(s => s.id === seat.id);
                        if (idx === -1) {
                            window.cfSelectedSeats.push({id: seat.id, row: rowNum, num: seat.num});
                            btn.style.background = 'var(--accent)';
                            btn.style.color = 'var(--bg)';
                        } else {
                            window.cfSelectedSeats.splice(idx, 1);
                            btn.style.background = 'var(--bg-card)';
                            btn.style.color = 'var(--text)';
                        }
                        const hasSeats = window.cfSelectedSeats.length > 0;
                        document.getElementById('cfBtnPay').disabled = !hasSeats;
                        document.getElementById('cfBtnBook').disabled = !hasSeats;
                        if (hasSeats) {
                            const txt = window.cfSelectedSeats.map(s => `Ряд ${s.row}, место ${s.num}`).join(' | ');
                            document.getElementById('cfSelectedSeatText').textContent = txt;
                            document.getElementById('cfSelectedSeatText').style.display = 'block';
                        } else {
                            document.getElementById('cfSelectedSeatText').style.display = 'none';
                        }
                        cfUpdateTotalPrice();
                    };
                }
                seatsWrap.appendChild(btn);
            });
            rowDiv.appendChild(seatsWrap);
            const lbl2 = document.createElement('span');
            lbl2.textContent = rowNum;
            lbl2.style.cssText = 'font-size:0.75rem; color:var(--text-muted); width:16px; flex-shrink:0;';
            rowDiv.appendChild(lbl2);
            map.appendChild(rowDiv);
        });
    });
}

function cfApplyPromo() {
    cfUpdateTotalPrice();
}

function cfUpdateTotalPrice() {
    const priceText = document.getElementById('cfFinalPriceText');
    if (!priceText) return;

    const select = document.getElementById('cfPromoSelect');
    const discount = select ? parseInt(select.options[select.selectedIndex]?.dataset.discount || '0', 10) : 0;
    const basePrice = Number(window.cfCurrentSessionData?.price || cfCurrentSessionPrice || 0);
    const seatCount = window.cfSelectedSeats ? window.cfSelectedSeats.length : 0;
    const oneSeatPrice = Math.round(basePrice * (1 - discount / 100));
    const totalPrice = oneSeatPrice * seatCount;

    priceText.textContent = `${totalPrice} ₽`;
    priceText.style.display = 'block';
    priceText.style.color = totalPrice > 0 ? 'var(--accent)' : 'var(--text-muted)';
}

function cfConfirm(action) {
    if (!cfIsLoggedIn) return;
    if (!window.cfSelectedSeats || window.cfSelectedSeats.length === 0 || !cfSelectedSessionId) return;

    const msg = document.getElementById('cfMsg');
    const paymentMethod = document.getElementById('cfPaymentMethod');
    const payBtn = document.getElementById('cfBtnPay');
    const bookBtn = document.getElementById('cfBtnBook');
    const oldPayText = payBtn ? payBtn.textContent : '';
    const oldBookText = bookBtn ? bookBtn.textContent : '';

    function showCfMessage(text, color) {
        if (!msg) return;
        msg.textContent = text;
        msg.style.color = color;
        msg.style.display = 'block';
    }

    function setCfSeatsLocked(locked) {
        document.querySelectorAll('#cfSeatMap button').forEach(seatBtn => {
            seatBtn.disabled = locked;
            if (locked) {
                seatBtn.style.opacity = '0.65';
                seatBtn.style.pointerEvents = 'none';
            } else {
                seatBtn.style.opacity = '';
                seatBtn.style.pointerEvents = '';
            }
        });
    }

    function restoreCfButtons() {
        if (payBtn) {
            payBtn.disabled = false;
            payBtn.textContent = oldPayText || 'Купить билет';
        }
        if (bookBtn) {
            bookBtn.disabled = false;
            bookBtn.textContent = oldBookText || 'Забронировать';
        }
        setCfSeatsLocked(false);
        cfUpdatePayButtons();
    }

    if (action === 'pay' && paymentMethod && !paymentMethod.value) {
        showCfMessage('Выберите способ оплаты', 'var(--danger)');
        return;
    }

    if (payBtn) payBtn.disabled = true;
    if (bookBtn) bookBtn.disabled = true;
    setCfSeatsLocked(true);
    if (action === 'pay' && payBtn) payBtn.textContent = 'Отправка билетов...';
    if (action === 'book' && bookBtn) bookBtn.textContent = 'Бронирование...';
    if (msg) msg.style.display = 'none';

    const fd = new FormData();
    fd.append('session_id', cfSelectedSessionId);
    fd.append('action', action);
    const promoSelect = document.getElementById('cfPromoSelect');
    if (promoSelect) fd.append('promo_id', promoSelect.value);
    if (paymentMethod) fd.append('payment_method', paymentMethod.value);
    window.cfSelectedSeats.forEach(s => fd.append('seat_ids', s.id));

    fetch('/client/book', { method: 'POST', body: fd })
    .then(async r => {
        const data = await r.json();
        if (!r.ok || !data.ok) throw new Error(data.error || 'Ошибка');
        return data;
    })
    .then(data => {
        showCfMessage(
            action === 'pay' ? 'Билеты оплачены и отправлены на вашу почту' : (data.message || 'Места забронированы'),
            'var(--success)'
        );

        if (action === 'pay') {
            setTimeout(() => {
                const seatModal = document.getElementById('cfSeatModal');
                if (seatModal) seatModal.style.display = 'none';
            }, 900);
        }
    })
    .catch(err => {
        showCfMessage(err.message || 'Ошибка', 'var(--danger)');
        restoreCfButtons();
    });
}

function cfClosePickerModal(e) {
    if (!e || e.target === document.getElementById('cfPickerModal'))
        document.getElementById('cfPickerModal').style.display = 'none';
}

function cfCloseSeatModal(e) {
    if (!e || e.target === document.getElementById('cfSeatModal'))
        document.getElementById('cfSeatModal').style.display = 'none';
}

function closeClientFilmModal(e) {
    if (!e || e.target === document.getElementById('clientFilmModal')) {
        document.getElementById('clientFilmModal').style.display = 'none';
    }
}