(function () {
  var toggle = document.querySelector("[data-nav-toggle]");
  var nav = document.querySelector("[data-nav]");
  if (toggle && nav) {
    toggle.addEventListener("click", function () {
      var open = nav.classList.toggle("is-open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && nav.classList.contains("is-open")) {
        nav.classList.remove("is-open");
        toggle.setAttribute("aria-expanded", "false");
        toggle.focus();
      }
    });
  }

  document.querySelectorAll("[data-sub-toggle]").forEach(function (button) {
    button.addEventListener("click", function () {
      var item = button.closest("[data-sub]");
      if (!item) return;
      var open = item.classList.toggle("is-open");
      button.setAttribute("aria-expanded", open ? "true" : "false");
    });
  });

  var endpoint = "https://agii-vapi-bridge-9383.twil.io/web-contact";

  function turnstileToken(form) {
    var el = form.querySelector('[name="cf-turnstile-response"]');
    if (el && el.value) return el.value;
    if (window.turnstile && typeof window.turnstile.getResponse === "function") {
      try {
        return window.turnstile.getResponse() || "";
      } catch (err) {
        return "";
      }
    }
    return "";
  }

  function setSubmitEnabled(form, enabled) {
    var button = form.querySelector("[type=submit]");
    if (!button) return;
    if (enabled && form.getAttribute("data-submitting") === "1") return;
    button.disabled = !enabled;
  }

  function resetTurnstile() {
    if (window.turnstile) window.turnstile.reset();
  }

  window.tsTurnstileSuccess = function () {
    document.querySelectorAll("form[data-ts-form]").forEach(function (form) {
      setSubmitEnabled(form, true);
    });
  };
  window.tsTurnstileExpire = function () {
    document.querySelectorAll("form[data-ts-form]").forEach(function (form) {
      setSubmitEnabled(form, false);
    });
  };
  window.tsTurnstileError = function () {
    document.querySelectorAll("form[data-ts-form]").forEach(function (form) {
      setSubmitEnabled(form, false);
    });
  };

  document.querySelectorAll("form[data-ts-form]").forEach(function (form) {
    setSubmitEnabled(form, !!turnstileToken(form));

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (form.getAttribute("data-submitting") === "1") return;

      var status = form.querySelector("[data-status]");
      var button = form.querySelector("[type=submit]");
      var data = Object.fromEntries(new FormData(form).entries());

      if (data.website) {
        form.reset();
        if (status) status.textContent = "Thanks — we have your note. We'll be in touch shortly.";
        return;
      }
      if (!data.email && !data.phone) {
        if (status) status.textContent = "Please add an email or a phone number so we can reach you.";
        return;
      }

      var token = turnstileToken(form);
      if (!token) {
        if (status) status.textContent = "Please wait for the verification check to finish, then press Send.";
        return;
      }
      data["cf-turnstile-response"] = token;

      var lines = [];
      if (data.for_whom) lines.push("For: " + data.for_whom);
      if (data.inspiration) lines.push("Starting from: " + data.inspiration);
      if (data.inspiration_link) lines.push("Inspiration link: " + data.inspiration_link);
      if (lines.length) data.message = lines.join("\n") + "\n\n" + (data.message || "");
      data.business = "TS";
      data.source = form.getAttribute("data-source") || "travelshertz.com";
      data.request_type = form.getAttribute("data-request") || "contact";

      form.setAttribute("data-submitting", "1");
      if (button) button.disabled = true;
      if (status) status.textContent = "Sending…";

      fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data)
      }).then(function (response) {
        if (!response.ok) throw new Error("Request failed");
        form.reset();
        resetTurnstile();
        if (status) status.textContent = "Thanks — we have your note. We'll be in touch shortly.";
      }).catch(function () {
        resetTurnstile();
        if (status) status.textContent = "Something went wrong. Please call or text (936) 297-6442.";
      }).finally(function () {
        form.removeAttribute("data-submitting");
        if (!window.turnstile) setSubmitEnabled(form, true);
      });
    });
  });

  window.addEventListener("load", function () {
    if (window.turnstile) return;
    document.querySelectorAll("form[data-ts-form]").forEach(function (form) {
      setSubmitEnabled(form, true);
    });
  });
})();
