(function () {
    const input = document.getElementById("imageInput");
    const box = document.querySelector("[data-upload-box]");
    const preview = document.querySelector("[data-upload-preview]");
    const placeholder = document.querySelector("[data-upload-placeholder]");
    const statusToggle = document.getElementById("statusToggle");
    const statusValue = document.getElementById("isActiveValue");

    function showPreview(file) {
        if (!file || !preview || !placeholder) return;

        const allowedTypes = ["image/png", "image/jpeg", "image/jpg", "image/webp"];
        const maxSize = 5 * 1024 * 1024;

        if (!allowedTypes.includes(file.type)) {
            alert("Format gambar tidak valid. Gunakan PNG, JPG, JPEG, atau WEBP.");
            input.value = "";
            return;
        }

        if (file.size > maxSize) {
            alert("Ukuran gambar maksimal 5MB.");
            input.value = "";
            return;
        }

        const reader = new FileReader();
        reader.onload = function (event) {
            preview.src = event.target.result;
            preview.hidden = false;
            placeholder.hidden = true;
            if (box) box.classList.add("has-image");
        };
        reader.readAsDataURL(file);
    }

    if (input) {
        input.addEventListener("change", function () {
            showPreview(this.files && this.files[0]);
        });
    }

    if (box) {
        ["dragenter", "dragover"].forEach(function (eventName) {
            box.addEventListener(eventName, function (event) {
                event.preventDefault();
                box.classList.add("is-dragging");
            });
        });

        ["dragleave", "drop"].forEach(function (eventName) {
            box.addEventListener(eventName, function (event) {
                event.preventDefault();
                box.classList.remove("is-dragging");
            });
        });

        box.addEventListener("drop", function (event) {
            const file = event.dataTransfer.files && event.dataTransfer.files[0];
            if (!file || !input) return;

            const dataTransfer = new DataTransfer();
            dataTransfer.items.add(file);
            input.files = dataTransfer.files;
            showPreview(file);
        });
    }

    if (statusToggle && statusValue) {
        statusToggle.addEventListener("click", function () {
            const nextState = !statusToggle.classList.contains("is-on");
            statusToggle.classList.toggle("is-on", nextState);
            statusToggle.setAttribute("aria-pressed", String(nextState));
            statusValue.value = nextState ? "1" : "0";
        });
    }
})();
