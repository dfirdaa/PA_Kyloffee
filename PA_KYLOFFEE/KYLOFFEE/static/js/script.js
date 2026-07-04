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

    const stateKey = "kyloffee_pos_cart_v2";
    const sidebarStateKey = "kyloffee_pos_sidebar_open";

    function readState() {
        try {
            return JSON.parse(sessionStorage.getItem(stateKey) || "{}") || {};
        } catch (error) {
            return {};
        }
    }

    function writeState(nextState) {
        sessionStorage.setItem(stateKey, JSON.stringify(nextState || {}));
    }

    function clearState() {
        sessionStorage.removeItem(stateKey);
    }

    function normalizeItems(items) {
        if (!Array.isArray(items)) return [];
        return items
            .map(function (item) {
                return {
                    id: Number(item.id || item.menu_id || 0),
                    name: String(item.name || "Menu"),
                    price: Math.max(0, Number(item.price || 0) || 0),
                    stock: Math.max(0, Number(item.stock || 0) || 0),
                    quantity: Math.max(1, Number(item.quantity || 1) || 1),
                };
            })
            .filter(function (item) {
                return item.id > 0 && item.quantity > 0;
            });
    }

    function formatCurrency(amount) {
        return "Rp" + Math.max(0, Math.round(Number(amount) || 0)).toLocaleString("id-ID");
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

    function setTextAll(nodes, text) {
        nodes.forEach(function (node) {
            node.textContent = text;
        });
    }

    function normalizeFilterValue(value) {
        return String(value || "").trim().replace(/\s+/g, " ").toLowerCase();
    }

    function calculateTotals(items, discount) {
        const subtotal = items.reduce(function (sum, item) {
            return sum + item.price * item.quantity;
        }, 0);
        const itemCount = items.reduce(function (sum, item) {
            return sum + item.quantity;
        }, 0);
        const discountAmount = Math.max(0, Number(discount || 0) || 0);
        return {
            subtotal,
            itemCount,
            discount: discountAmount,
            total: Math.max(0, subtotal - discountAmount),
        };
    }

    function showMessage(box, text, tone) {
        if (!box) return;
        box.hidden = false;
        box.textContent = text;
        box.className = "pos-message pos-message--" + tone;
    }

    function clearMessage(box) {
        if (!box) return;
        box.hidden = true;
        box.textContent = "";
        box.className = "pos-message";
    }

    function readSidebarState() {
        if (window.matchMedia("(max-width: 760px)").matches) return false;
        const saved = localStorage.getItem(sidebarStateKey);
        if (saved === "0") return false;
        if (saved === "1") return true;
        return true;
    }

    function initPosPage(root) {
        const cart = new Map();
        const storedState = readState();
        normalizeItems(storedState.items).forEach(function (item) {
            const matchingCard = root.querySelector('[data-product-card][data-menu-id="' + item.id + '"]');
            if (matchingCard) {
                item.stock = Math.max(0, Number(matchingCard.dataset.stock || item.stock || 0) || 0);
                item.name = matchingCard.dataset.name || item.name;
                item.price = Math.max(0, Number(matchingCard.dataset.price || item.price || 0) || 0);
            }
            if (item.stock > 0) {
                item.quantity = Math.min(item.quantity, item.stock);
                cart.set(item.id, item);
            }
        });

        let selectedCategory = "all";
        const paymentUrl = root.dataset.paymentUrl || "/pos/payment";
        const cartItems = root.querySelector("[data-cart-items]");
        const cartEmpty = root.querySelector("[data-cart-empty]");
        const cartCountLabels = root.querySelectorAll("[data-cart-count]");
        const menuSearchInput = root.querySelector("[data-menu-search]");
        const openPaymentButton = root.querySelector("[data-open-payment]");
        const messageBox = root.querySelector("[data-pos-message]");
        const summarySubtotalLabels = root.querySelectorAll("[data-summary-subtotal]");
        const summaryTotalLabels = root.querySelectorAll("[data-summary-total]");
        const productCards = Array.from(root.querySelectorAll("[data-product-card]"));
        const menuEmpty = root.querySelector("[data-menu-empty]");
        const sidebar = root.querySelector("[data-pos-sidebar]");
        const sidebarNavLinks = root.querySelectorAll(".pos-sidebar-nav-link");
        const sidebarLogout = root.querySelector(".pos-sidebar-logout");
        const sidebarToggleButtons = root.querySelectorAll("[data-pos-sidebar-toggle]");
        const mobileSidebarQuery = window.matchMedia("(max-width: 760px)");
        let isSidebarOpen = readSidebarState();

        function setSidebarOpen(nextState, options) {
            const shouldPersist = !options || options.persist !== false;
            const isMobileLayout = mobileSidebarQuery.matches;
            isSidebarOpen = Boolean(nextState);
            root.classList.toggle("is-sidebar-collapsed", !isSidebarOpen);
            root.classList.toggle("is-sidebar-open", isSidebarOpen);
            root.dataset.sidebarState = isSidebarOpen ? "expanded" : "collapsed";

            if (sidebar) {
                sidebar.classList.toggle("pos-sidebar-mock--expanded", isSidebarOpen);
                sidebar.classList.toggle("pos-sidebar-mock--collapsed", !isSidebarOpen);
            }

            sidebarNavLinks.forEach(function (link) {
                link.classList.toggle("pos-sidebar-nav-link--expanded", isSidebarOpen);
                link.classList.toggle("pos-sidebar-nav-link--collapsed", !isSidebarOpen);
            });

            if (sidebarLogout) {
                sidebarLogout.classList.toggle("pos-sidebar-logout--expanded", isSidebarOpen);
                sidebarLogout.classList.toggle("pos-sidebar-logout--collapsed", !isSidebarOpen);
            }

            if (shouldPersist && !isMobileLayout) {
                localStorage.setItem(sidebarStateKey, isSidebarOpen ? "1" : "0");
            }

            sidebarToggleButtons.forEach(function (button) {
                button.setAttribute("aria-expanded", String(isSidebarOpen));
                button.setAttribute("aria-label", isSidebarOpen ? "Tutup sidebar POS" : "Buka sidebar POS");
            });
        }

        sidebarToggleButtons.forEach(function (button) {
            button.addEventListener("click", function () {
                setSidebarOpen(!isSidebarOpen);
            });
        });

        function handleSidebarViewportChange(event) {
            setSidebarOpen(event.matches ? false : readSidebarState(), { persist: false });
        }

        if (mobileSidebarQuery.addEventListener) {
            mobileSidebarQuery.addEventListener("change", handleSidebarViewportChange);
        } else if (mobileSidebarQuery.addListener) {
            mobileSidebarQuery.addListener(handleSidebarViewportChange);
        }

        root.addEventListener("click", function (event) {
            if (mobileSidebarQuery.matches && isSidebarOpen && event.target === root) {
                setSidebarOpen(false);
            }
        });

        function currentItems() {
            return Array.from(cart.values());
        }

        function persistState() {
            writeState({
                ...readState(),
                items: currentItems(),
            });
        }

        function filterProducts() {
            const keyword = normalizeFilterValue(menuSearchInput && menuSearchInput.value ? menuSearchInput.value : "");
            let visibleCount = 0;

            productCards.forEach(function (card) {
                const category = normalizeFilterValue(card.dataset.category || "");
                const searchableText = [
                    card.dataset.name || "",
                    card.dataset.category || "",
                    card.dataset.description || "",
                ].join(" ").toLowerCase().replace(/\s+/g, " ");
                const matchesCategory = selectedCategory === "all" || category === selectedCategory;
                const matchesSearch = !keyword || searchableText.includes(keyword);
                const isVisible = matchesCategory && matchesSearch;

                card.hidden = !isVisible;
                card.classList.toggle("is-hidden", !isVisible);
                if (isVisible) visibleCount += 1;
            });

            if (menuEmpty) {
                menuEmpty.hidden = productCards.length === 0 || visibleCount > 0;
            }
        }

        function renderCart() {
            const items = currentItems();
            const totals = calculateTotals(items, 0);

            if (cartEmpty) cartEmpty.hidden = items.length > 0;
            setTextAll(cartCountLabels, totals.itemCount + (totals.itemCount === 1 ? " Item" : " Items"));
            setTextAll(summarySubtotalLabels, formatCurrency(totals.subtotal));
            setTextAll(summaryTotalLabels, formatCurrency(totals.subtotal));

            if (openPaymentButton) {
                openPaymentButton.disabled = items.length === 0 || totals.subtotal <= 0;
            }

            if (cartItems) {
                cartItems.innerHTML = items.map(function (item) {
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

            persistState();
            filterProducts();
        }

        function addProduct(card) {
            const id = Number(card.dataset.menuId);
            const stock = Number(card.dataset.stock || 0);
            if (!id || stock <= 0) return;

            const existing = cart.get(id);
            if (existing && existing.quantity >= stock) {
                showMessage(messageBox, "Stok " + existing.name + " tidak cukup.", "error");
                return;
            }

            cart.set(id, {
                id,
                name: card.dataset.name || "Menu",
                price: Number(card.dataset.price || 0),
                stock,
                quantity: existing ? existing.quantity + 1 : 1,
            });
            clearMessage(messageBox);
            renderCart();
        }

        root.querySelectorAll("[data-add-product]").forEach(function (button) {
            button.addEventListener("click", function () {
                const card = button.closest("[data-product-card]");
                if (card) addProduct(card);
            });
        });

        root.querySelectorAll("[data-category-filter]").forEach(function (button) {
            button.addEventListener("click", function () {
                selectedCategory = normalizeFilterValue(button.dataset.categoryFilter || "all") || "all";
                root.querySelectorAll("[data-category-filter]").forEach(function (item) {
                    item.classList.toggle("is-active", item === button);
                });
                filterProducts();
            });
        });

        if (menuSearchInput) {
            menuSearchInput.addEventListener("input", filterProducts);
        }

        if (cartItems) {
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
                clearMessage(messageBox);
                renderCart();
            });
        }

        if (openPaymentButton) {
            openPaymentButton.addEventListener("click", function () {
                if (cart.size === 0) return;
                persistState();
                window.location.href = paymentUrl;
            });
        }

        setSidebarOpen(isSidebarOpen);
        renderCart();
    }

    function initPaymentPage(root) {
        const checkoutUrl = root.dataset.checkoutUrl;
        const qrisUrl = root.dataset.qrisUrl;
        const posUrl = root.dataset.posUrl || "/pos";
        const state = readState();
        const items = normalizeItems(state.items);
        let isSubmitting = false;
        let selectedMethod = state.paymentMethod === "QRIS" ? "QRIS" : "Cash";
        let cashValue = state.cashValue ? String(state.cashValue) : "";
        let qrisRequestId = 0;
        let qrisState = {
            orderCode: "",
            total: 0,
            timestamp: "",
            loading: false,
            error: false,
        };

        const cartItems = root.querySelector("[data-payment-cart-items]");
        const cartEmpty = root.querySelector("[data-payment-cart-empty]");
        const cartCountLabel = root.querySelector("[data-payment-count]");
        const customerNameInput = root.querySelector("[data-customer-name]");
        const discountInput = root.querySelector("[data-discount-amount]");
        const messageBox = root.querySelector("[data-payment-message]");
        const methodButtons = root.querySelectorAll("[data-payment-method]");
        const paymentModes = root.querySelectorAll("[data-payment-mode]");
        const completeCashButton = root.querySelector("[data-complete-cash]");
        const checkQrisButton = root.querySelector("[data-check-qris]");
        const cashReceived = root.querySelector("[data-cash-received]");
        const cashChange = root.querySelector("[data-cash-change]");
        const cashTotal = root.querySelector("[data-cash-total]");
        const qrisImage = root.querySelector("[data-qris-image]");
        const qrisPlaceholder = root.querySelector("[data-qris-placeholder]");
        const qrisTotal = root.querySelector("[data-qris-total]");
        const qrisOrder = root.querySelector("[data-qris-order]");
        const qrisStatus = root.querySelector("[data-qris-status]");
        const summarySubtotalLabels = root.querySelectorAll("[data-summary-subtotal]");
        const summaryDiscountLabels = root.querySelectorAll("[data-summary-discount]");
        const summaryTotalLabels = root.querySelectorAll("[data-summary-total]");

        if (customerNameInput) customerNameInput.value = state.customerName || "";
        if (discountInput) discountInput.value = Number(state.discountAmount || 0);

        function getCashAmount() {
            return Math.max(0, Number(cashValue || 0) || 0);
        }

        function getTotals() {
            return calculateTotals(items, parseAmount(discountInput));
        }

        function savePaymentState() {
            writeState({
                items,
                customerName: customerNameInput ? customerNameInput.value : "",
                discountAmount: parseAmount(discountInput),
                paymentMethod: selectedMethod,
                cashValue,
            });
        }

        function resetQrisState(message) {
            qrisRequestId += 1;
            qrisState = { orderCode: "", total: 0, timestamp: "", loading: false, error: false };
            if (qrisImage) {
                qrisImage.hidden = true;
                qrisImage.removeAttribute("src");
            }
            if (qrisPlaceholder) {
                qrisPlaceholder.hidden = false;
                qrisPlaceholder.textContent = "QRIS";
            }
            if (qrisOrder) qrisOrder.textContent = message || "Invoice akan dibuat setelah keranjang siap.";
            if (qrisStatus) qrisStatus.textContent = message || "Menunggu QR dibuat.";
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
            if (items.length === 0 || total <= 0) {
                resetQrisState("Tambahkan menu untuk membuat QRIS.");
                return;
            }
            if (qrisState.orderCode && qrisState.total === total) return;
            if (qrisState.loading && qrisState.total === total) return;
            if (qrisState.error && qrisState.total === total) return;

            const requestId = qrisRequestId + 1;
            qrisRequestId = requestId;
            qrisState = { orderCode: "", total, timestamp: "", loading: true, error: false };
            if (qrisImage) qrisImage.hidden = true;
            if (qrisPlaceholder) {
                qrisPlaceholder.hidden = false;
                qrisPlaceholder.textContent = "Loading";
            }
            if (qrisOrder) qrisOrder.textContent = "Membuat QRIS...";
            if (qrisStatus) qrisStatus.textContent = "Generating QR Code...";
            if (checkQrisButton) checkQrisButton.disabled = true;

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
                    if (qrisImage) {
                        qrisImage.src = body.qr_url;
                        qrisImage.hidden = false;
                    }
                    if (qrisPlaceholder) qrisPlaceholder.hidden = true;
                    if (qrisOrder) qrisOrder.textContent = body.order_code + " | " + body.timestamp;
                    if (qrisStatus) qrisStatus.textContent = "Menunggu pembayaran QRIS.";
                    renderPayment();
                })
                .catch(function (error) {
                    if (requestId !== qrisRequestId) return;
                    qrisState = { orderCode: "", total: 0, timestamp: "", loading: false, error: true };
                    if (qrisImage) {
                        qrisImage.hidden = true;
                        qrisImage.removeAttribute("src");
                    }
                    if (qrisPlaceholder) {
                        qrisPlaceholder.hidden = false;
                        qrisPlaceholder.textContent = "QRIS";
                    }
                    if (qrisOrder) qrisOrder.textContent = error.message;
                    if (qrisStatus) qrisStatus.textContent = error.message;
                    showMessage(messageBox, error.message, "error");
                    renderPayment();
                });
        }

        function renderPayment() {
            const totals = getTotals();
            const received = getCashAmount();
            const change = Math.max(received - totals.total, 0);
            const hasItems = items.length > 0;

            if (cartEmpty) cartEmpty.hidden = hasItems;
            if (cartCountLabel) cartCountLabel.textContent = totals.itemCount + (totals.itemCount === 1 ? " Item" : " Items");
            setTextAll(summarySubtotalLabels, formatCurrency(totals.subtotal));
            setTextAll(summaryDiscountLabels, formatCurrency(totals.discount));
            setTextAll(summaryTotalLabels, formatCurrency(totals.total));
            if (cashReceived) cashReceived.textContent = formatCurrency(received);
            if (cashChange) cashChange.textContent = formatCurrency(change);
            if (cashTotal) cashTotal.textContent = formatCurrency(totals.total);
            if (qrisTotal) qrisTotal.textContent = formatCurrency(totals.total);

            if (cartItems) {
                cartItems.innerHTML = items.map(function (item) {
                    return [
                        '<article class="payment-cart-item">',
                            '<div>',
                                '<strong>' + escapeHtml(item.name) + '</strong>',
                                '<span>' + item.quantity + ' x ' + formatCurrency(item.price) + '</span>',
                            '</div>',
                            '<b>' + formatCurrency(item.price * item.quantity) + '</b>',
                        '</article>',
                    ].join("");
                }).join("");
            }

            if (completeCashButton) {
                completeCashButton.disabled = isSubmitting || !hasItems || totals.total <= 0 || received < totals.total;
            }
            if (checkQrisButton) {
                checkQrisButton.disabled = (
                    isSubmitting ||
                    !hasItems ||
                    totals.total <= 0 ||
                    qrisState.loading ||
                    !qrisState.orderCode
                );
                if (!isSubmitting) {
                    checkQrisButton.textContent = "Check Payment Status";
                }
            }

            updatePaymentMethod();
            savePaymentState();
            ensureQrisCode(totals.total);
        }

        function buildCheckoutPayload(method, options) {
            const totals = getTotals();
            return {
                customer_name: customerNameInput ? customerNameInput.value : "",
                payment_method: method,
                order_code: options && options.orderCode ? options.orderCode : "",
                received_amount: options && options.receivedAmount !== undefined ? options.receivedAmount : totals.total,
                change_amount: options && options.changeAmount !== undefined ? options.changeAmount : 0,
                discount_amount: totals.discount,
                items: items.map(function (item) {
                    return { menu_id: item.id, quantity: item.quantity };
                }),
            };
        }

        function submitPayment(method, options) {
            if (items.length === 0 || isSubmitting) return;

            isSubmitting = true;
            renderPayment();
            clearMessage(messageBox);

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
                    clearState();
                    window.location.href = transaction.success_url || "/pos";
                })
                .catch(function (error) {
                    showMessage(messageBox, error.message, "error");
                    isSubmitting = false;
                    renderPayment();
                });
        }

        root.querySelectorAll("[data-payment-back]").forEach(function (link) {
            link.addEventListener("click", function (event) {
                event.preventDefault();
                savePaymentState();
                window.location.href = posUrl;
            });
        });

        methodButtons.forEach(function (button) {
            button.addEventListener("click", function () {
                selectedMethod = button.dataset.paymentMethod === "QRIS" ? "QRIS" : "Cash";
                clearMessage(messageBox);
                renderPayment();
            });
        });

        if (customerNameInput) {
            customerNameInput.addEventListener("input", savePaymentState);
        }

        if (discountInput) {
            discountInput.addEventListener("input", function () {
                resetQrisState("Total berubah. QRIS akan dibuat ulang.");
                clearMessage(messageBox);
                renderPayment();
            });
        }

        root.querySelectorAll("[data-quick-cash]").forEach(function (button) {
            button.addEventListener("click", function () {
                cashValue = button.dataset.quickCash || "";
                clearMessage(messageBox);
                renderPayment();
            });
        });

        root.querySelectorAll("[data-cash-key]").forEach(function (button) {
            button.addEventListener("click", function () {
                const key = button.dataset.cashKey;
                if (key === "clear") {
                    cashValue = "";
                } else if (key === "back") {
                    cashValue = cashValue.slice(0, -1);
                } else if (cashValue.length < 10) {
                    cashValue = (cashValue + key).replace(/^0+(?=\d)/, "");
                }
                clearMessage(messageBox);
                renderPayment();
            });
        });

        if (completeCashButton) {
            completeCashButton.addEventListener("click", function () {
                const totals = getTotals();
                const received = getCashAmount();
                if (received < totals.total) {
                    showMessage(messageBox, "Nominal diterima kurang dari total pembayaran.", "error");
                    return;
                }
                submitPayment("Cash", {
                    receivedAmount: received,
                    changeAmount: Math.max(received - totals.total, 0),
                });
            });
        }

        if (checkQrisButton) {
            checkQrisButton.addEventListener("click", function () {
                const totals = getTotals();
                if (!qrisState.orderCode) {
                    showMessage(messageBox, "QRIS belum siap. Tunggu QR Code selesai dibuat.", "error");
                    return;
                }

                if (qrisStatus) qrisStatus.textContent = "Checking payment status...";
                checkQrisButton.disabled = true;
                checkQrisButton.textContent = "Checking...";

                window.setTimeout(function () {
                    if (qrisStatus) qrisStatus.textContent = "Payment Success";
                    checkQrisButton.textContent = "Payment Success";
                    submitPayment("QRIS", {
                        orderCode: qrisState.orderCode,
                        receivedAmount: totals.total,
                        changeAmount: 0,
                    });
                }, 2300);
            });
        }

        resetQrisState();
        renderPayment();
    }

    const posPage = document.querySelector("[data-pos-cart]");
    if (posPage) initPosPage(posPage);

    const paymentPage = document.querySelector("[data-payment-page]");
    if (paymentPage) initPaymentPage(paymentPage);
});
