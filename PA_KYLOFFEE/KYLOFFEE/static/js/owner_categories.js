(function () {
    const modal = document.querySelector("[data-category-modal]");
    const form = document.querySelector("[data-category-form]");
    if (!modal || !form) return;

    const title = document.getElementById("categoryModalTitle");
    const nameInput = document.querySelector("[data-category-name]");
    const descriptionInput = document.querySelector("[data-category-description]");
    const submitButton = document.querySelector("[data-category-submit]");
    const openButtons = document.querySelectorAll("[data-open-category-modal]");
    const closeButtons = document.querySelectorAll("[data-close-category-modal]");
    const createAction = form.dataset.createAction;
    const editActionTemplate = form.dataset.editActionTemplate;
    let currentId = form.dataset.currentId || "";

    function normalizeName(value) {
        return String(value || "").trim().replace(/\s+/g, " ");
    }

    function nameKey(value) {
        return normalizeName(value).toLowerCase();
    }

    function categoryNames() {
        return Array.from(document.querySelectorAll("[data-open-category-modal][data-id]")).map(function (button) {
            return {
                id: String(button.dataset.id || ""),
                name: button.dataset.name || "",
            };
        });
    }

    function setFieldError(message) {
        let error = form.querySelector(".field-error");
        if (!message) {
            if (error) error.remove();
            return;
        }

        if (!error) {
            error = document.createElement("small");
            error.className = "field-error";
            nameInput.insertAdjacentElement("afterend", error);
        }
        error.textContent = message;
    }

    function openModal(mode, data) {
        currentId = data && data.id ? String(data.id) : "";
        form.dataset.currentId = currentId;
        form.action = mode === "edit"
            ? editActionTemplate.replace("/0/", "/" + currentId + "/")
            : createAction;

        if (title) title.textContent = mode === "edit" ? "Edit Category" : "Add Category";
        if (submitButton) submitButton.textContent = mode === "edit" ? "Simpan Perubahan" : "Simpan Kategori";
        if (nameInput) nameInput.value = data && data.name ? data.name : "";
        if (descriptionInput) descriptionInput.value = data && data.description ? data.description : "";

        setFieldError("");
        modal.hidden = false;
        window.setTimeout(function () {
            if (nameInput) nameInput.focus();
        }, 0);
    }

    function closeModal() {
        modal.hidden = true;
        form.classList.remove("is-loading");
        if (submitButton) submitButton.disabled = false;
    }

    openButtons.forEach(function (button) {
        button.addEventListener("click", function () {
            openModal(button.dataset.mode || "create", button.dataset);
        });
    });

    closeButtons.forEach(function (button) {
        button.addEventListener("click", closeModal);
    });

    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && !modal.hidden) {
            closeModal();
        }
    });

    form.addEventListener("submit", function (event) {
        const cleanName = normalizeName(nameInput && nameInput.value);
        const duplicate = categoryNames().some(function (category) {
            return category.id !== String(currentId || "") && nameKey(category.name) === nameKey(cleanName);
        });

        if (!cleanName) {
            event.preventDefault();
            setFieldError("Nama kategori wajib diisi.");
            if (nameInput) nameInput.focus();
            return;
        }

        if (duplicate) {
            event.preventDefault();
            setFieldError("Nama kategori sudah digunakan.");
            if (nameInput) nameInput.focus();
            return;
        }

        nameInput.value = cleanName;
        form.classList.add("is-loading");
        if (submitButton) submitButton.disabled = true;
    });
})();
