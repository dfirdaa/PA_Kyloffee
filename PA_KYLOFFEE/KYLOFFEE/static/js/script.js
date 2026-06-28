document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-toggle-password]").forEach(function (button) {
        button.addEventListener("click", function () {
            const targetId = button.getAttribute("data-toggle-password");
            const input = document.getElementById(targetId);
            if (!input) return;

            const isPassword = input.type === "password";
            input.type = isPassword ? "text" : "password";
            button.textContent = isPassword ? "🙈" : "👁";
            button.setAttribute("aria-label", isPassword ? "Sembunyikan password" : "Lihat password");
        });
    });
});
