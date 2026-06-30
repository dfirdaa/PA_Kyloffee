document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-toggle-password]").forEach(function (button) {
        button.addEventListener("click", function () {
            const targetId = button.getAttribute("data-toggle-password");
            const input = document.getElementById(targetId);
            if (!input) return;

            const isPassword = input.type === "password";
            input.type = isPassword ? "text" : "password";
            button.classList.toggle("is-visible", isPassword);
            button.setAttribute("aria-label", isPassword ? "Sembunyikan password" : "Lihat password");
        });
    });

    const posCart = document.querySelector("[data-pos-cart]");
    if (!posCart) return;

    const cart = new Map();
    let isSubmitting = false;

    const checkoutUrl = posCart.dataset.checkoutUrl;
    const cartItems = posCart.querySelector("[data-cart-items]");
    const cartEmpty = posCart.querySelector("[data-cart-empty]");
    const cartCount = posCart.querySelector("[data-cart-count]");
    const checkoutButton = posCart.querySelector("[data-checkout-button]");
    const customerNameInput = posCart.querySelector("[data-customer-name]");
    const paymentMethodInput = posCart.querySelector("[data-payment-method]");
    const discountInput = posCart.querySelector("[data-discount-amount]");
    const taxInput = posCart.querySelector("[data-tax-amount]");
    const operationalCostInput = posCart.querySelector("[data-operational-cost]");
    const messageBox = posCart.querySelector("[data-pos-message]");

    const summarySubtotal = posCart.querySelector("[data-summary-subtotal]");
    const summaryDiscount = posCart.querySelector("[data-summary-discount]");
    const summaryTax = posCart.querySelector("[data-summary-tax]");
    const summaryCost = posCart.querySelector("[data-summary-cost]");
    const summaryTotal = posCart.querySelector("[data-summary-total]");

    function formatCurrency(amount) {
        return "Rp " + Math.max(0, Math.round(Number(amount) || 0)).toLocaleString("id-ID");
    }

    function parseAmount(input) {
        return Math.max(0, Number(input && input.value ? input.value : 0) || 0);
    }

    function escapeHtml(value) {
        return String(value).replace(/[&<>"']/g, function (char) {
            return {
                "&": "&amp;",
                "<": "&lt;",
                ">": "&gt;",
                '"': "&quot;",
                "'": "&#039;",
            }[char];
        });
    }

    function showMessage(text, tone) {
        messageBox.hidden = false;
        messageBox.textContent = text;
        messageBox.className = "pos-message pos-message--" + tone;
    }

    function clearMessage() {
        messageBox.hidden = true;
        messageBox.textContent = "";
        messageBox.className = "pos-message";
    }

    function getTotals() {
        let subtotal = 0;
        let itemCount = 0;

        cart.forEach(function (item) {
            subtotal += item.price * item.quantity;
            itemCount += item.quantity;
        });

        const discount = parseAmount(discountInput);
        const tax = parseAmount(taxInput);
        const operationalCost = parseAmount(operationalCostInput);
        const total = Math.max(0, subtotal - discount + tax);

        return { subtotal, itemCount, discount, tax, operationalCost, total };
    }

    function renderCart() {
        const totals = getTotals();
        cartCount.textContent = totals.itemCount + " item";
        cartEmpty.hidden = cart.size > 0;
        checkoutButton.disabled = isSubmitting || cart.size === 0;

        summarySubtotal.textContent = formatCurrency(totals.subtotal);
        summaryDiscount.textContent = formatCurrency(totals.discount);
        summaryTax.textContent = formatCurrency(totals.tax);
        summaryCost.textContent = formatCurrency(totals.operationalCost);
        summaryTotal.textContent = formatCurrency(totals.total);

        cartItems.innerHTML = Array.from(cart.values()).map(function (item) {
            return [
                '<article class="pos-cart-item">',
                    '<div>',
                        '<strong>' + escapeHtml(item.name) + '</strong>',
                        '<small>' + formatCurrency(item.price) + ' / item</small>',
                    '</div>',
                    '<div class="cart-qty-control">',
                        '<button type="button" data-cart-action="minus" data-menu-id="' + item.id + '">-</button>',
                        '<span>' + item.quantity + '</span>',
                        '<button type="button" data-cart-action="plus" data-menu-id="' + item.id + '" ' + (item.quantity >= item.stock ? "disabled" : "") + '>+</button>',
                    '</div>',
                    '<strong>' + formatCurrency(item.price * item.quantity) + '</strong>',
                    '<button type="button" class="cart-remove" data-cart-action="remove" data-menu-id="' + item.id + '">x</button>',
                '</article>',
            ].join("");
        }).join("");
    }

    function addProduct(card) {
        const id = Number(card.dataset.menuId);
        const stock = Number(card.dataset.stock || 0);
        if (!id || stock <= 0) return;

        const existing = cart.get(id);
        if (existing && existing.quantity >= stock) {
            showMessage("Stok " + existing.name + " tidak cukup.", "error");
            return;
        }

        cart.set(id, {
            id,
            name: card.dataset.name || "Menu",
            price: Number(card.dataset.price || 0),
            stock,
            quantity: existing ? existing.quantity + 1 : 1,
        });
        clearMessage();
        renderCart();
    }

    function updateProductStock(menuId, stockRemaining) {
        const card = document.querySelector('[data-product-card][data-menu-id="' + menuId + '"]');
        if (!card) return;

        const nextStock = Number(stockRemaining || 0);
        card.dataset.stock = String(nextStock);

        const stockLabel = card.querySelector("[data-stock-label]");
        if (stockLabel) stockLabel.textContent = "Stok: " + nextStock;

        const addButton = card.querySelector("[data-add-product]");
        if (addButton) {
            addButton.disabled = nextStock <= 0;
            addButton.textContent = nextStock <= 0 ? "Stok Habis" : "Tambah";
        }
        card.classList.toggle("is-out", nextStock <= 0);
    }

    document.querySelectorAll("[data-add-product]").forEach(function (button) {
        button.addEventListener("click", function () {
            const card = button.closest("[data-product-card]");
            if (card) addProduct(card);
        });
    });

    document.querySelectorAll("[data-category-filter]").forEach(function (button) {
        button.addEventListener("click", function () {
            const selectedCategory = button.dataset.categoryFilter;
            document.querySelectorAll("[data-category-filter]").forEach(function (item) {
                item.classList.toggle("is-active", item === button);
            });
            document.querySelectorAll("[data-product-card]").forEach(function (card) {
                card.hidden = selectedCategory !== "all" && card.dataset.category !== selectedCategory;
            });
        });
    });

    cartItems.addEventListener("click", function (event) {
        const button = event.target.closest("[data-cart-action]");
        if (!button) return;

        const id = Number(button.dataset.menuId);
        const item = cart.get(id);
        if (!item) return;

        const action = button.dataset.cartAction;
        if (action === "plus" && item.quantity < item.stock) {
            item.quantity += 1;
        } else if (action === "minus") {
            item.quantity -= 1;
            if (item.quantity <= 0) cart.delete(id);
        } else if (action === "remove") {
            cart.delete(id);
        }
        clearMessage();
        renderCart();
    });

    [discountInput, taxInput, operationalCostInput].forEach(function (input) {
        input.addEventListener("input", renderCart);
    });

    checkoutButton.addEventListener("click", function () {
        if (cart.size === 0 || isSubmitting) return;

        const totals = getTotals();
        const payload = {
            customer_name: customerNameInput.value,
            payment_method: paymentMethodInput.value,
            discount_amount: totals.discount,
            tax_amount: totals.tax,
            operational_cost: totals.operationalCost,
            items: Array.from(cart.values()).map(function (item) {
                return { menu_id: item.id, quantity: item.quantity };
            }),
        };

        isSubmitting = true;
        checkoutButton.textContent = "Menyimpan...";
        renderCart();
        clearMessage();

        fetch(checkoutUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        })
            .then(function (response) {
                return response.json().catch(function () {
                    return {};
                }).then(function (body) {
                    if (!response.ok || !body.success) {
                        throw new Error(body.message || "Transaksi gagal disimpan.");
                    }
                    return body;
                });
            })
            .then(function (body) {
                const transaction = body.transaction || {};
                (transaction.items || []).forEach(function (item) {
                    updateProductStock(item.menu_id, item.stock_remaining);
                });

                cart.clear();
                customerNameInput.value = "";
                discountInput.value = 0;
                taxInput.value = 0;
                operationalCostInput.value = 0;
                showMessage((body.message || "Transaksi berhasil disimpan.") + " Total: " + (transaction.total_display || formatCurrency(totals.total)), "success");
            })
            .catch(function (error) {
                showMessage(error.message, "error");
            })
            .finally(function () {
                isSubmitting = false;
                checkoutButton.textContent = "Simpan Transaksi";
                renderCart();
            });
    });

    renderCart();
});
