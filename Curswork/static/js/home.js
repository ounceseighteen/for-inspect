// ─────────────────────────────────────────────
// Главная страница: ротатор акций и карусель
// фильмов. Подключается из templates/user/index.html
// ─────────────────────────────────────────────

// Ротатор промо-баннеров
document.addEventListener('DOMContentLoaded', function () {
    const items = Array.from(document.querySelectorAll('.promo-rotator-item'));
    if (items.length <= 1) return;

    let currentPromoIndex = 0;

    setInterval(function () {
        items[currentPromoIndex].style.display = 'none';
        currentPromoIndex = (currentPromoIndex + 1) % items.length;
        items[currentPromoIndex].style.display = 'flex';
    }, 17000);
});

// Карусель «Сегодня в кино»
const allCards = Array.from(document.querySelectorAll('#filmsCarousel .film-sell-card'));
const arrowLeftHome = document.getElementById('arrowLeft');
const arrowRightHome = document.getElementById('arrowRight');
const VISIBLE = 5;
let currentPage = 0;
const totalPages = Math.ceil(allCards.length / VISIBLE);

function renderPage() {
    allCards.forEach((card, i) => {
        card.style.display = (i >= currentPage * VISIBLE && i < (currentPage + 1) * VISIBLE) ? '' : 'none';
    });
    arrowLeftHome.style.display = currentPage > 0 ? 'flex' : 'none';
    arrowRightHome.style.display = currentPage < totalPages - 1 ? 'flex' : 'none';

    [arrowLeftHome, arrowRightHome].forEach(btn => {
        btn.style.alignItems = 'center';
        btn.style.justifyContent = 'center';
    });
}

function scrollCarousel(dir) {
    currentPage = Math.max(0, Math.min(totalPages - 1, currentPage + dir));
    renderPage();
}

if (allCards.length > 0) renderPage();