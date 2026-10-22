(() => {
  "use strict";

  const form = document.getElementById("login-form");
  const errorBox = document.getElementById("login-error");
  const password = document.getElementById("password");
  const submit = document.getElementById("login-submit");
  const toggle = document.getElementById("toggle-password");
  const year = document.getElementById("login-year");

  if (year) year.textContent = String(new Date().getFullYear());

  if (toggle) {
    toggle.addEventListener("click", () => {
      const visible = password.type === "text";
      password.type = visible ? "password" : "text";
      toggle.setAttribute("aria-label", visible ? "Mostrar contraseña" : "Ocultar contraseña");
      toggle.setAttribute("title", visible ? "Mostrar contraseña" : "Ocultar contraseña");
      password.focus();
      const end = password.value.length;
      try {
        password.setSelectionRange(end, end);
      } catch {
        /* not supported */
      }
    });
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    errorBox.textContent = "";
    const value = password.value;
    if (!value) {
      errorBox.textContent = "Escribí la contraseña.";
      password.focus();
      return;
    }
    submit.disabled = true;
    const label = submit.textContent;
    submit.textContent = "Entrando…";
    try {
      const response = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ password: value }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        if (response.status === 429) {
          throw new Error("Demasiados intentos. Esperá un minuto y volvé a probar.");
        }
        throw new Error(data.error || "No se pudo entrar.");
      }
      window.location.replace("/");
    } catch (error) {
      errorBox.textContent = error.message || "No se pudo contactar al servidor.";
      submit.disabled = false;
      submit.textContent = label;
      password.select();
    }
  });
})();
