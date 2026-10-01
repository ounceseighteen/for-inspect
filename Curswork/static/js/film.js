// ─────────────────────────────────────────────
// Страница фильма: отзывы, оценка, избранное.
// Подключается из templates/user/film.html
// ─────────────────────────────────────────────

function bindRatingStars() {
    document.querySelectorAll('#ratingStars .rate-star').forEach(star => {
        star.addEventListener('click', function () {
            const val = parseInt(this.dataset.val, 10);
            document.querySelectorAll('#ratingStars .rate-star').forEach((s, i) => {
                s.style.color = i < val ? 'var(--accent)' : 'var(--border)';
            });
            window.selectedRating = val;
        });
    });
}

function submitReview(filmId) {
    const msg = document.getElementById('reviewMsg');
    const rating = window.selectedRating || 0;
    const comment = document.getElementById('reviewComment').value.trim();
    if (!rating) {
        msg.textContent = 'Поставьте оценку от 1 до 5';
        msg.style.color = 'var(--danger)';
        msg.style.display = 'block';
        return;
    }
    const fd = new FormData();
    fd.append('film_id', filmId);
    fd.append('rating', rating);
    fd.append('comment', comment);
    fetch('/api/review', { method: 'POST', body: fd })
        .then(r => r.json())
        .then(data => {
            if (!data.ok) throw new Error(data.error || 'Ошибка');
            msg.textContent = 'Отзыв сохранён. Спасибо!';
            msg.style.color = 'var(--success)';
            msg.style.display = 'block';
            setTimeout(() => location.reload(), 900);
        })
        .catch(e => {
            msg.textContent = e.message || 'Ошибка';
            msg.style.color = 'var(--danger)';
            msg.style.display = 'block';
        });
}

function toggleFavorite(filmId) {
    fetch('/api/favorite/' + filmId, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            const btn = document.getElementById('favBtn');
            if (data.favorite) {
                btn.textContent = '♥ В избранном';
                btn.style.color = 'var(--accent)';
                btn.style.borderColor = 'var(--accent)';
                btn.style.background = 'rgba(124,92,255,0.15)';
            } else {
                btn.textContent = '♡ Хочу посмотреть';
                btn.style.color = 'var(--text)';
                btn.style.borderColor = 'var(--border)';
                btn.style.background = 'var(--bg-card)';
            }
        });
}

document.addEventListener('DOMContentLoaded', function () {
    bindRatingStars();
    const preRating = window.FILM_PRE_RATING || 0;
    if (preRating) window.selectedRating = preRating;
});