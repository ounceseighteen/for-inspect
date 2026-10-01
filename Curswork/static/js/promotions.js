// ─────────────────────────────────────────────
// Акции: открытие модалки подробной информации.
// Подключается из templates/user/promotions.html
// ─────────────────────────────────────────────

function openPromoDetail(id) {
    const p = window.promosData[id];
    if (!p) return;
    document.getElementById('promoDetailTitle').textContent = p.title;
    document.getElementById('promoDetailDesc').textContent = p.description;
    const disc = document.getElementById('promoDetailDiscount');
    if (p.discount) {
        disc.textContent = '−' + p.discount + '%';
        disc.style.display = 'inline-block';
    } else {
        disc.style.display = 'none';
    }
    const imgBlock = document.getElementById('promoDetailImg');
    if (p.image) {
        document.getElementById('promoDetailImgSrc').src = p.image;
        imgBlock.style.display = 'block';
    } else {
        imgBlock.style.display = 'none';
    }
    document.getElementById('promoDetailModal').style.display = 'flex';
}

function closePromoDetail(e) {
    if (!e || e.target === document.getElementById('promoDetailModal'))
        document.getElementById('promoDetailModal').style.display = 'none';
}