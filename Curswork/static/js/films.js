// ─────────────────────────────────────────────
// Страница всех фильмов: поиск, фильтры, сортировка.
// Подключается из templates/user/films.html (второй блок).
// ─────────────────────────────────────────────

function setGenre(btn, genre) {
    document.querySelectorAll('.genre-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    filterFilms();
}

function filterFilms() {
    const q = document.getElementById('searchInput').value.toLowerCase();
    const genre = document.getElementById('genreSelect').value;
    const sort = document.getElementById('sortSelect').value;
    const grid = document.getElementById('filmsGrid');
    const cards = Array.from(grid.querySelectorAll('.film-sell-card'));

    let visible = cards.filter(card => {
        const matchTitle = card.dataset.title.includes(q);
        const matchGenre = !genre || card.dataset.genre === genre;
        const show = matchTitle && matchGenre;
        card.style.display = show ? '' : 'none';
        return show;
    });

    visible.sort((a, b) => {
        if (sort === 'popular') {
            return parseInt(b.dataset.tickets) - parseInt(a.dataset.tickets);
        } else if (sort === 'soon') {
            return a.dataset.next.localeCompare(b.dataset.next);
        } else {
            return a.dataset.title.localeCompare(b.dataset.title);
        }
    });

    visible.forEach(card => grid.appendChild(card));

    document.getElementById('noResults').style.display = visible.length === 0 ? 'block' : 'none';
}

function resetFilters() {
    document.getElementById('searchInput').value = '';
    document.getElementById('genreSelect').value = '';
    document.getElementById('sortSelect').value = 'title';
    filterFilms();
}