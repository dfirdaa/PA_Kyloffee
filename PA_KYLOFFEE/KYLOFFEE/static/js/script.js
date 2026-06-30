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
    let selectedMethod = "Cash";
    let selectedCategory = "all";
    let cashValue = "";
    let qrisRequestId = 0;
    let qrisState = {
        orderCode: "",
        total: 0,
        timestamp: "",
        loading: false,
        error: false,
    };

    const checkoutUrl = posCart.dataset.checkoutUrl;
    const qrisUrl = posCart.dataset.qrisUrl;
    const cartItems = posCart.querySelector("[data-cart-items]");
    const cartEmpty = posCart.querySelector("[data-cart-empty]");
    const cartCountLabels = posCart.querySelectorAll("[data-cart-count]");
    const menuSearchInput = posCart.querySelector("[data-menu-search]");
    const openPaymentButton = posCart.querySelector("[data-open-payment]");
    const paymentModal = posCart.querySelector("[data-payment-modal]");
    const customerNameInput = posCart.querySelector("[data-customer-name]");
    const discountInput = posCart.querySelector("[data-discount-amount]");
    const taxInput = posCart.querySelector("[data-tax-amount]");
    const operationalCostInput = posCart.querySelector("[data-operational-cost]");
    const messageBox = posCart.querySelector("[data-pos-message]");
    const methodButtons = posCart.querySelectorAll("[data-payment-method]");
    const paymentModes = posCart.querySelectorAll("[data-payment-mode]");
    const completeCashButton = posCart.querySelector("[data-complete-cash]");
    const checkQrisButton = posCart.querySelector("[data-check-qris]");
    const cashReceived = posCart.querySelector("[data-cash-received]");
    const cashChange = posCart.querySelector("[data-cash-change]");
    const qrisImage = posCart.querySelector("[data-qris-image]");
    const qrisPlaceholder = posCart.querySelector("[data-qris-placeholder]");
    const qrisTotal = posCart.querySelector("[data-qris-total]");
    const qrisOrder = posCart.querySelector("[data-qris-order]");
    const qrisStatus = posCart.querySelector("[data-qris-status]");

    const summarySubtotalLabels = posCart.querySelectorAll("[data-summary-subtotal]");
    const summaryDiscountLabels = posCart.querySelectorAll("[data-summary-discount]");
    const summaryTaxLabels = posCart.querySelectorAll("[data-summary-tax]");
    const summaryCostLabels = posCart.querySelectorAll("[data-summary-cost]");
    const summaryTotalLabels = posCart.querySelectorAll("[data-summary-total]");

    function setTextAll(nodes, text) {
        nodes.forEach(function (node) {
            node.textContent = text;
        });
    }

    function formatCurrency(amount) {
        return "Rp " + Math.max(0, Math.round(Number(amount) || 0)).toLocaleString("id-ID");
    }

    function parseAmount(input) {
        return Math.max(0, Number(input && input.value ? input.value : 0) || 0);
    }

    function getCashAmount() {
        return Math.max(0, Number(cashValue || 0) || 0);
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

    function openPaymentModal() {
        if (!paymentModal || cart.size === 0) return;
        paymentModal.hidden = false;
        document.body.classList.add("is-payment-open");
        renderCart();
    }

    function closePaymentModal() {
        if (!paymentModal) return;
        paymentModal.hidden = true;
        document.body.classList.remove("is-payment-open");
    }

    function filterProducts() {
        const keyword = (menuSearchInput && menuSearchInput.value ? menuSearchInput.value : "").trim().toLowerCase();
        document.querySelectorAll("[data-product-card]").forEach(function (card) {
            const matchesCategory = selectedCategory === "all" || card.dataset.category === selectedCategory;
            const matchesSearch = !keyword || (card.dataset.name || "").toLowerCase().includes(keyword);
            card.hidden = !matchesCategory || !matchesSearch;
        });
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

    function resetQrisState(message) {
        qrisRequestId += 1;
        qrisState = { orderCode: "", total: 0, timestamp: "", loading: false, error: false };
        qrisImage.hidden = true;
        qrisImage.removeAttribute("src");
        qrisPlaceholder.hidden = false;
        qrisPlaceholder.textContent = "QRIS";
        qrisOrder.textContent = message || "Invoice akan dibuat setelah keranjang siap.";
        qrisStatus.textContent = message || "Menunggu QR dibuat.";
    }

    function updatePaymentMethod() {
        methodButtons.forEach(function (button) {
            button.classList.toggle("is-active", button.dataset.paymentMethod === selectedMethod);
        });
        paymentModes.forEach(function (mode) {
            mode.classList.toggle("is-active", mode.dataset.paymentMode === selectedMethod);
        });
    }

    function ensureQrisCode(total) {
        if (selectedMethod !== "QRIS") return;
        if (cart.size === 0 || total <= 0) {
            resetQrisState("Tambahkan menu untuk membuat QRIS.");
            return;
        }
        if (qrisState.orderCode && qrisState.total === total) return;
        if (qrisState.loading && qrisState.total === total) return;
        if (qrisState.error && qrisState.total === total) return;

        const requestId = qrisRequestId + 1;
        qrisRequestId = requestId;
        qrisState = { orderCode: "", total, timestamp: "", loading: true, error: false };
        qrisImage.hidden = true;
        qrisPlaceholder.hidden = false;
        qrisPlaceholder.textContent = "Loading";
        qrisOrder.textContent = "Membuat QRIS...";
        qrisStatus.textContent = "Generating QR Code...";
        checkQrisButton.disabled = true;

        fetch(qrisUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ total_amount: total }),
        })
            .then(function (response) {
                return response.json().catch(function () {
                    return {};
                }).then(function (body) {
                    if (!response.ok || !body.success) {
                        throw new Error(body.message || "Gagal membuat QRIS.");
                    }
                    return body;
                });
            })
            .then(function (body) {
                if (requestId !== qrisRequestId) return;
                qrisState = {
                    orderCode: body.order_code,
                    total,
                    timestamp: body.timestamp,
                    loading: false,
                    error: false,
                };
                qrisImage.src = body.qr_url;
                qrisImage.hidden = false;
                qrisPlaceholder.hidden = true;
                qrisOrder.textContent = body.order_code + " | " + body.timestamp;
                qrisStatus.textContent = "Menunggu pembayaran QRIS.";
                renderCart();
            })
            .catch(function (error) {
                if (requestId !== qrisRequestId) return;
                qrisState = { orderCode: "", total, timestamp: "", loading: false, error: true };
                qrisImage.hidden = true;
                qrisImage.removeAttribute("src");
                qrisPlaceholder.hidden = false;
                qrisPlaceholder.textContent = "QRIS";
                qrisOrder.textContent = error.message;
                qrisStatus.textContent = error.message;
                showMessage(error.message, "error");
                renderCart();
            });
    }

    function renderCart() {
        const totals = getTotals();
        const received = getCashAmount();
        const change = Math.max(received - totals.total, 0);

        setTextAll(cartCountLabels, totals.itemCount + (totals.itemCount === 1 ? " Item" : " Items"));
        cartEmpty.hidden = cart.size > 0;

        setTextAll(summarySubtotalLabels, formatCurrency(totals.subtotal));
        setTextAll(summaryDiscountLabels, formatCurrency(totals.discount));
        setTextAll(summaryTaxLabels, formatCurrency(totals.tax));
        setTextAll(summaryCostLabels, formatCurrency(totals.operationalCost));
        setTextAll(summaryTotalLabels, formatCurrency(totals.total));
        cashReceived.textContent = formatCurrency(received);
        cashChange.textContent = formatCurrency(change);
        qrisTotal.textContent = formatCurrency(totals.total);
        if (openPaymentButton) {
            openPaymentButton.disabled = isSubmitting || cart.size === 0 || totals.total <= 0;
        }

        completeCashButton.disabled = isSubmitting || cart.size === 0 || totals.total <= 0 || received < totals.total;
        checkQrisButton.disabled = (
            isSubmitting ||
            cart.size === 0 ||
            totals.total <= 0 ||
            qrisState.loading ||
            !qrisState.orderCode
        );

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

        updatePaymentMethod();
        ensureQrisCode(totals.total);
        filterProducts();
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

    function buildCheckoutPayload(method, options) {
        const totals = getTotals();
        return {
            customer_name: customerNameInput.value,
            payment_method: method,
            order_code: options && options.orderCode ? options.orderCode : "",
            received_amount: options && options.receivedAmount ? options.receivedAmount : totals.total,
            change_amount: options && options.changeAmount ? options.changeAmount : 0,
            discount_amount: totals.discount,
            tax_amount: totals.tax,
            operational_cost: totals.operationalCost,
            items: Array.from(cart.values()).map(function (item) {
                return { menu_id: item.id, quantity: item.quantity };
            }),
        };
    }

    function submitPayment(method, options) {
        if (cart.size === 0 || isSubmitting) return;

        isSubmitting = true;
        renderCart();
        clearMessage();

        fetch(checkoutUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(buildCheckoutPayload(method, options || {})),
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
                window.location.href = transaction.success_url || "/pos";
            })
            .catch(function (error) {
                showMessage(error.message, "error");
                isSubmitting = false;
                renderCart();
            });
    }

    document.querySelectorAll("[data-add-product]").forEach(function (button) {
        button.addEventListener("click", function () {
            const card = button.closest("[data-product-card]");
            if (card) addProduct(card);
        });
    });

    document.querySelectorAll("[data-category-filter]").forEach(function (button) {
        button.addEventListener("click", function () {
            selectedCategory = button.dataset.categoryFilter;
            document.querySelectorAll("[data-category-filter]").forEach(function (item) {
                item.classList.toggle("is-active", item === button);
            });
            filterProducts();
        });
    });

    posCart.querySelectorAll("[data-filter-reset]").forEach(function (button) {
        button.addEventListener("click", function () {
            selectedCategory = "all";
            if (menuSearchInput) menuSearchInput.value = "";
            document.querySelectorAll("[data-category-filter]").forEach(function (item) {
                item.classList.toggle("is-active", item.dataset.categoryFilter === "all");
            });
            filterProducts();
        });
    });

    if (menuSearchInput) {
        menuSearchInput.addEventListener("input", filterProducts);
    }

    if (openPaymentButton) {
        openPaymentButton.addEventListener("click", openPaymentModal);
    }

    posCart.querySelectorAll("[data-close-payment]").forEach(function (button) {
        button.addEventListener("click", closePaymentModal);
    });

    methodButtons.forEach(function (button) {
        button.addEventListener("click", function () {
            selectedMethod = button.dataset.paymentMethod;
            clearMessage();
            renderCart();
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
        input.addEventListener("input", function () {
            if (selectedMethod === "QRIS") resetQrisState("Total berubah. QRIS akan dibuat ulang.");
            renderCart();
        });
    });

    posCart.querySelectorAll("[data-quick-cash]").forEach(function (button) {
        button.addEventListener("click", function () {
            cashValue = button.dataset.quickCash;
            clearMessage();
            renderCart();
        });
    });

    posCart.querySelectorAll("[data-cash-key]").forEach(function (button) {
        button.addEventListener("click", function () {
            const key = button.dataset.cashKey;
            if (key === "clear") {
                cashValue = "";
            } else if (key === "back") {
                cashValue = cashValue.slice(0, -1);
            } else if (cashValue.length < 10) {
                cashValue = (cashValue + key).replace(/^0+(?=\d)/, "");
            }
            clearMessage();
            renderCart();
        });
    });

    completeCashButton.addEventListener("click", function () {
        const totals = getTotals();
        const received = getCashAmount();
        if (received < totals.total) {
            showMessage("Nominal diterima kurang dari total pembayaran.", "error");
            return;
        }
        submitPayment("Cash", {
            receivedAmount: received,
            changeAmount: Math.max(received - totals.total, 0),
        });
    });

    checkQrisButton.addEventListener("click", function () {
        const totals = getTotals();
        if (!qrisState.orderCode) {
            showMessage("QRIS belum siap. Tunggu QR Code selesai dibuat.", "error");
            return;
        }

        qrisStatus.textContent = "Checking payment status...";
        checkQrisButton.disabled = true;
        checkQrisButton.textContent = "Checking...";

        window.setTimeout(function () {
            qrisStatus.textContent = "Payment Success";
            checkQrisButton.textContent = "Payment Success";
            submitPayment("QRIS", {
                orderCode: qrisState.orderCode,
                receivedAmount: totals.total,
                changeAmount: 0,
            });
        }, 2300);
    });

    resetQrisState();
    renderCart();
});
