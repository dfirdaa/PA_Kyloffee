(function () {
    const page = document.querySelector(".owner-page");
    const sidebar = document.querySelector("[data-owner-sidebar]");
    if (!page || !sidebar) return;

    const stateKey = "kyloffee_owner_sidebar_collapsed";
    const mobileQuery = window.matchMedia("(max-width: 760px)");
    const toggleButtons = document.querySelectorAll("[data-owner-sidebar-toggle]");
    const overlay = document.querySelector("[data-owner-sidebar-overlay]");

    let collapsed = localStorage.getItem(stateKey) === "1";
    let mobileOpen = false;

    function setToggleLabels() {
        toggleButtons.forEach(function (button) {
            const expanded = mobileQuery.matches ? mobileOpen : !collapsed;
            button.setAttribute("aria-expanded", String(expanded));
            button.setAttribute(
                "aria-label",
                mobileQuery.matches
                    ? (mobileOpen ? "Tutup sidebar Owner" : "Buka sidebar Owner")
                    : (collapsed ? "Perbesar sidebar Owner" : "Perkecil sidebar Owner")
            );
        });
    }

    function applyState(options) {
        const shouldPersist = !options || options.persist !== false;
        page.classList.toggle("owner-sidebar-collapsed", collapsed && !mobileQuery.matches);
        page.classList.toggle("owner-sidebar-open", mobileOpen && mobileQuery.matches);
        sidebar.classList.toggle("owner-sidebar--collapsed", collapsed && !mobileQuery.matches);

        if (overlay) {
            overlay.hidden = !(mobileOpen && mobileQuery.matches);
        }

        if (shouldPersist && !mobileQuery.matches) {
            localStorage.setItem(stateKey, collapsed ? "1" : "0");
        }

        setToggleLabels();
    }

    toggleButtons.forEach(function (button) {
        button.addEventListener("click", function () {
            if (mobileQuery.matches) {
                mobileOpen = !mobileOpen;
            } else {
                collapsed = !collapsed;
            }
            applyState();
        });
    });

    if (overlay) {
        overlay.addEventListener("click", function () {
            mobileOpen = false;
            applyState({ persist: false });
        });
    }

    function handleViewportChange() {
        mobileOpen = false;
        applyState({ persist: false });
    }

    if (mobileQuery.addEventListener) {
        mobileQuery.addEventListener("change", handleViewportChange);
    } else if (mobileQuery.addListener) {
        mobileQuery.addListener(handleViewportChange);
    }

    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && mobileOpen) {
            mobileOpen = false;
            applyState({ persist: false });
        }
    });

    applyState({ persist: false });
})();
