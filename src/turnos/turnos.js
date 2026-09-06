(function () {
  "use strict";

  var EA = "https://turnos.allitto.com/index.php/booking";
  var WRITE_RE = /booking\/register|book_appointment/i;
  // Single place to point the reservation POST. Change this at deploy.
  var BOOKING_URL = "https://turnos.allitto.com/api/reservar";
  var WHATSAPP_URL = "https://wa.me/5491132595130";
  var MONTHS = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"
  ];
  var WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"];

  var SEDES = [
    {
      id: "ituzaingo",
      name: "Ituzaingó",
      address: "José Pacífico Otero 826",
      map: "https://www.google.com/maps/search/?api=1&query=Jos%C3%A9+Pac%C3%ADfico+Otero+826,+Ituzaing%C3%B3",
      serviceId: 1,
      providerId: 5,
      duration: 30
    },
    {
      id: "monte-castro",
      name: "Monte Castro, CABA",
      address: "Pje. Dr. David Peña 4235",
      map: "https://www.google.com/maps/search/?api=1&query=Pasaje+Dr.+David+Pe%C3%B1a+4235,+Monte+Castro",
      serviceId: 2,
      providerId: 7,
      duration: 30
    }
  ];

  var state = {
    step: 1,
    sede: SEDES[0],
    year: null,
    month: null,
    date: null,
    hour: null,
    unavailable: [],
    monthUnavailable: false,
    loadFailed: false,
    hours: [],
    monthFetch: null,
    hoursFetch: null,
    patient: { firstName: "", lastName: "", email: "", phone: "", notes: "" },
    formStartedAt: null,
    idempotencyKey: null,
    idempotencyFp: null,
    submitting: false,
    booked: false
  };

  var els = {
    stepLabel: document.getElementById("step-label"),
    live: document.getElementById("live"),
    panels: document.querySelectorAll(".panel"),
    sedes: document.getElementById("sedes"),
    sedeError: document.getElementById("sede-error"),
    ctaSede: document.getElementById("cta-sede"),
    dayContext: document.getElementById("day-context"),
    calendar: document.getElementById("calendar"),
    calMonth: document.getElementById("cal-month"),
    calGrid: document.getElementById("cal-grid"),
    calPrev: document.getElementById("cal-prev"),
    calNext: document.getElementById("cal-next"),
    calLegend: document.getElementById("cal-legend"),
    dayError: document.getElementById("day-error"),
    ctaDay: document.getElementById("cta-day"),
    hourContext: document.getElementById("hour-context"),
    hours: document.getElementById("hours"),
    hourHint: document.getElementById("hour-hint"),
    hourError: document.getElementById("hour-error"),
    ctaHour: document.getElementById("cta-hour"),
    form: document.getElementById("datos-form"),
    errorSummary: document.getElementById("error-summary"),
    errorTitle: document.getElementById("error-summary-title"),
    errorList: document.getElementById("error-summary-list"),
    summary: document.getElementById("summary"),
    bookingResult: document.getElementById("booking-result"),
    ctaConfirm: document.getElementById("cta-confirm"),
    confirmError: document.getElementById("confirm-error"),
    checks: document.querySelector(".checks")
  };

  function todayAR() {
    var parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "America/Buenos_Aires",
      year: "numeric",
      month: "2-digit",
      day: "2-digit"
    }).formatToParts(new Date());
    var get = function (t) { return parts.find(function (p) { return p.type === t; }).value; };
    return { y: Number(get("year")), m: Number(get("month")), d: Number(get("day")) };
  }

  function pad(n) { return String(n).padStart(2, "0"); }
  function iso(y, m, d) { return y + "-" + pad(m) + "-" + pad(d); }
  function parseIso(s) {
    var p = s.split("-");
    return { y: Number(p[0]), m: Number(p[1]), d: Number(p[2]) };
  }
  function daysInMonth(y, m) { return new Date(y, m, 0).getDate(); }
  function mondayIndex(y, m, d) {
    var js = new Date(y, m - 1, d).getDay();
    return (js + 6) % 7;
  }
  function formatLong(isoDate) {
    var p = parseIso(isoDate);
    var idx = mondayIndex(p.y, p.m, p.d);
    return WEEKDAYS[idx] + " " + p.d + " de " + MONTHS[p.m - 1];
  }
  function titleCaseMonth(y, m) {
    var name = MONTHS[m - 1];
    return name.charAt(0).toUpperCase() + name.slice(1) + " " + y;
  }
  function announce(msg) { els.live.textContent = msg; }

  var FETCH_TIMEOUT_MS = 11000;

  function eaFetch(url, opts) {
    if (WRITE_RE.test(String(url))) {
      throw new Error("Blocked write to production agenda");
    }
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, FETCH_TIMEOUT_MS);
    var merged = Object.assign({ cache: "no-store" }, opts || {}, { signal: controller.signal });
    return fetch(url, merged).finally(function () { clearTimeout(timer); });
  }

  function getUnavailable(sede, y, m) {
    var url = new URL(EA + "/get_unavailable_dates");
    url.searchParams.set("provider_id", String(sede.providerId));
    url.searchParams.set("service_id", String(sede.serviceId));
    url.searchParams.set("selected_date", iso(y, m, 1));
    url.searchParams.set("manage_mode", "0");
    return eaFetch(url).then(function (res) {
      if (!res.ok) throw new Error("HTTP " + res.status);
      return res.json();
    }).then(function (data) {
      if (data && data.is_month_unavailable === true) {
        return { monthUnavailable: true, dates: [] };
      }
      if (Array.isArray(data)) {
        return { monthUnavailable: false, dates: data };
      }
      throw new Error("Unexpected unavailable payload");
    });
  }

  function monthKey(sede, y, m) {
    return String(sede.providerId) + ":" + String(sede.serviceId) + ":" + y + ":" + m;
  }

  function beginMonthFetch(sede, y, m) {
    var key = monthKey(sede, y, m);
    var entry = { key: key, settled: false, promise: null };
    entry.promise = getUnavailable(sede, y, m).then(function (result) {
      entry.settled = true;
      return result;
    }, function (err) {
      entry.settled = true;
      throw err;
    });
    state.monthFetch = entry;
    return entry;
  }

  function prefetchSelectedSede() {
    var t = todayAR();
    beginMonthFetch(state.sede, t.y, t.m);
  }

  function getHours(sede, date) {
    var body = new URLSearchParams({
      service_id: String(sede.serviceId),
      provider_id: String(sede.providerId),
      selected_date: date,
      service_duration: String(sede.duration),
      manage_mode: "0"
    });
    return eaFetch(EA + "/get_available_hours", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body
    }).then(function (res) {
      if (!res.ok) throw new Error("HTTP " + res.status);
      return res.json();
    }).then(function (data) {
      if (!Array.isArray(data)) throw new Error("Unexpected hours payload");
      return data;
    });
  }

  function hoursKey(sede, date) {
    return String(sede.providerId) + ":" + String(sede.serviceId) + ":" + date;
  }

  function beginHoursFetch(sede, date) {
    var key = hoursKey(sede, date);
    var entry = { key: key, settled: false, promise: null };
    entry.promise = getHours(sede, date).then(function (result) {
      entry.settled = true;
      return result;
    }, function (err) {
      entry.settled = true;
      throw err;
    });
    state.hoursFetch = entry;
    return entry;
  }

  function prefetchSelectedDay() {
    if (!state.date) return;
    beginHoursFetch(state.sede, state.date);
  }

  function setStep(n, silent) {
    state.step = n;
    var labels = {
      1: "Paso 1 de 5 — Sede",
      2: "Paso 2 de 5 — Día",
      3: "Paso 3 de 5 — Hora",
      4: "Paso 4 de 5 — Datos",
      5: "Paso 5 de 5 — Confirmación"
    };
    els.stepLabel.textContent = labels[n];
    if (n === 4 && state.formStartedAt == null) {
      state.formStartedAt = Date.now();
    }
    if (n !== 5 && !state.submitting) {
      state.booked = false;
      els.bookingResult.hidden = true;
      els.bookingResult.textContent = "";
      els.ctaConfirm.hidden = false;
      els.ctaConfirm.disabled = false;
      els.ctaConfirm.textContent = "Pedir la evaluación";
      els.ctaConfirm.setAttribute("aria-busy", "false");
      if (els.checks) els.checks.hidden = false;
      var title5 = document.getElementById("title-5");
      if (title5) title5.textContent = "Reservá tu turno";
    }
    els.panels.forEach(function (p) {
      var step = Number(p.getAttribute("data-step"));
      p.hidden = step !== n;
    });
    var title = document.getElementById("title-" + n);
    if (title) title.focus({ preventScroll: true });
    if (!silent) {
      announce("Paso " + n + " de 5. " + title.textContent);
      scrollFormIntoView();
    }
  }

  // Bring the FORM back into view on a step change - never the document top.
  // This markup is mounted twice: standalone at /turnos/ (form at the top) and
  // embedded in the home inside #turnos, far below the hero. A window.scrollTo(0, 0)
  // reads fine standalone and throws the patient back up to the hero on every
  // single step when embedded. Scroll the form's own container instead, and offset
  // by the sticky header so the step title does not hide under it.
  function scrollFormIntoView() {
    var root = document.querySelector(".turnos-root") || document.getElementById("app");
    if (!root || typeof root.getBoundingClientRect !== "function") return;
    var offset = 12;
    var header = document.querySelector("header");
    if (header) {
      var pos = window.getComputedStyle(header).position;
      if (pos === "fixed" || pos === "sticky") {
        offset += header.getBoundingClientRect().height;
      }
    }
    var top = root.getBoundingClientRect().top + window.pageYOffset - offset;
    if (top < 0) top = 0;
    var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    try {
      window.scrollTo({ top: top, behavior: reduce ? "auto" : "smooth" });
    } catch (e) {
      window.scrollTo(0, top);
    }
  }

  function renderSedes() {
    els.sedes.innerHTML = "";
    SEDES.forEach(function (sede) {
      var card = document.createElement("div");
      card.className = "sede" + (state.sede.id === sede.id ? " is-selected" : "");
      card.setAttribute("role", "radio");
      card.tabIndex = 0;
      card.setAttribute("aria-checked", state.sede.id === sede.id ? "true" : "false");
      card.innerHTML =
        '<span class="sede-name">' + sede.name + "</span>" +
        '<span class="radio" aria-hidden="true"></span>' +
        '<span class="sede-addr">' + sede.address + "</span>";
      var map = document.createElement("a");
      map.className = "sede-map";
      map.href = sede.map;
      map.target = "_blank";
      map.rel = "noopener noreferrer";
      map.textContent = "Ver en el mapa";
      map.addEventListener("click", function (e) { e.stopPropagation(); });
      card.appendChild(map);
      function selectSede() {
        var changed = state.sede.id !== sede.id;
        state.sede = sede;
        state.date = null;
        state.hour = null;
        state.hoursFetch = null;
        if (changed || !state.monthFetch) prefetchSelectedSede();
        renderSedes();
      }
      card.addEventListener("click", selectSede);
      card.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          selectSede();
        }
      });
      els.sedes.appendChild(card);
    });
  }

  function availableDates() {
    if (state.loadFailed || state.monthUnavailable) return [];
    var n = daysInMonth(state.year, state.month);
    var blocked = {};
    state.unavailable.forEach(function (d) { blocked[d] = true; });
    var out = [];
    for (var d = 1; d <= n; d++) {
      var key = iso(state.year, state.month, d);
      if (!blocked[key]) out.push(key);
    }
    return out;
  }

  function renderCalendar() {
    els.calMonth.textContent = titleCaseMonth(state.year, state.month);
    els.calGrid.innerHTML = "";
    var firstDow = mondayIndex(state.year, state.month, 1);
    var n = daysInMonth(state.year, state.month);
    var blocked = {};
    state.unavailable.forEach(function (d) { blocked[d] = true; });
    var cells = 42;
    els.calendar.removeAttribute("aria-busy");
    els.calGrid.removeAttribute("aria-busy");
    for (var i = 0; i < cells; i++) {
      var dayNum = i - firstDow + 1;
      var cell = document.createElement("button");
      cell.type = "button";
      cell.className = "cal-day";
      if (dayNum < 1 || dayNum > n) {
        cell.disabled = true;
        cell.setAttribute("aria-hidden", "true");
        cell.tabIndex = -1;
        els.calGrid.appendChild(cell);
        continue;
      }
      var key = iso(state.year, state.month, dayNum);
      var isOff = state.loadFailed || state.monthUnavailable || !!blocked[key];
      cell.textContent = String(dayNum);
      cell.setAttribute("aria-label", formatLong(key) + (isOff ? ", sin turno" : ""));
      if (isOff) {
        cell.disabled = true;
      } else {
        cell.classList.add("is-available");
        cell.setAttribute("aria-pressed", state.date === key ? "true" : "false");
        if (state.date === key) cell.classList.add("is-selected");
        cell.addEventListener("click", function (picked) {
          return function () {
            var changed = state.date !== picked;
            state.date = picked;
            state.hour = null;
            if (changed || !state.hoursFetch) prefetchSelectedDay();
            clearError(els.dayError);
            renderCalendar();
            updateDayContext();
          };
        }(key));
      }
      els.calGrid.appendChild(cell);
    }
    els.calLegend.textContent = state.monthUnavailable
      ? "Este mes no tiene turnos disponibles. Los días en gris no tienen turno."
      : "Los días en gris no tienen turno.";
  }

  function updateDayContext() {
    var open = availableDates();
    var next = state.date || open[0];
    if (next) {
      els.dayContext.textContent = state.sede.name + " · Próximo turno: " + formatLong(next) + ".";
    } else {
      els.dayContext.textContent = state.sede.name + " · No hay días con turno en este mes.";
    }
  }

  function renderCalendarSkeleton() {
    els.calendar.setAttribute("aria-busy", "true");
    els.calGrid.setAttribute("aria-busy", "true");
    els.calMonth.textContent = titleCaseMonth(state.year, state.month);
    els.calGrid.innerHTML = "";
    for (var i = 0; i < 42; i++) {
      var cell = document.createElement("span");
      cell.className = "cal-day is-skeleton";
      cell.setAttribute("aria-hidden", "true");
      els.calGrid.appendChild(cell);
    }
    els.calLegend.textContent = "Los días en gris no tienen turno.";
  }

  function loadMonth() {
    var requestedYear = state.year;
    var requestedMonth = state.month;
    var requestedSedeId = state.sede.id;
    var key = monthKey(state.sede, requestedYear, requestedMonth);
    var pending = state.monthFetch;
    var request;
    if (pending && pending.key === key) {
      request = pending.promise;
    } else {
      request = getUnavailable(state.sede, requestedYear, requestedMonth);
    }
    state.monthFetch = null;
    els.dayError.hidden = true;
    if (!(pending && pending.key === key && pending.settled)) {
      renderCalendarSkeleton();
    }
    return request.then(function (result) {
      if (state.year !== requestedYear || state.month !== requestedMonth || state.sede.id !== requestedSedeId) return;
      state.loadFailed = false;
      state.monthUnavailable = result.monthUnavailable;
      state.unavailable = result.dates;
      if (state.date) {
        var p = parseIso(state.date);
        if (p.y !== state.year || p.m !== state.month) state.date = null;
        if (state.monthUnavailable || result.dates.indexOf(state.date) !== -1) state.date = null;
      }
      if (!state.date) {
        var open = availableDates();
        if (open[0]) state.date = open[0];
      }
      renderCalendar();
      updateDayContext();
    }).catch(function () {
      if (state.year !== requestedYear || state.month !== requestedMonth || state.sede.id !== requestedSedeId) return;
      state.unavailable = [];
      state.monthUnavailable = false;
      state.loadFailed = true;
      state.date = null;
      renderCalendar();
      showError(els.dayError, "No pudimos cargar los días.", function () { loadMonth(); });
    });
  }

  function renderHours() {
    els.hours.innerHTML = "";
    if (!state.hours.length) {
      var empty = document.createElement("p");
      empty.className = "empty";
      empty.id = "hours-empty";
      empty.textContent = "Este día no tiene horarios disponibles. Elegí otro día.";
      els.hours.appendChild(empty);
      els.hourHint.textContent = "Turnos de 30 minutos.";
      return;
    }
    els.hourHint.textContent = "Turnos de 30 minutos.";
    state.hours.forEach(function (h) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "hour" + (state.hour === h ? " is-selected" : "");
      btn.setAttribute("role", "option");
      btn.setAttribute("aria-selected", state.hour === h ? "true" : "false");
      btn.textContent = h;
      btn.addEventListener("click", function () {
        state.hour = h;
        clearError(els.hourError);
        renderHours();
      });
      els.hours.appendChild(btn);
    });
  }

  function loadHours() {
    var requestedDate = state.date;
    var requestedSedeId = state.sede.id;
    var key = hoursKey(state.sede, requestedDate);
    var pending = state.hoursFetch;
    var request;
    if (pending && pending.key === key) {
      request = pending.promise;
    } else {
      request = getHours(state.sede, requestedDate);
    }
    state.hoursFetch = null;
    els.hourError.hidden = true;
    els.hourContext.textContent = formatLong(state.date) + " · " + state.sede.name;
    if (!(pending && pending.key === key && pending.settled)) {
      els.hours.innerHTML = "<p class=\"empty\">Cargando horarios…</p>";
    }
    return request.then(function (hours) {
      if (state.date !== requestedDate || state.sede.id !== requestedSedeId) return;
      state.hours = hours;
      if (state.hour && hours.indexOf(state.hour) === -1) state.hour = null;
      if (!state.hour && hours[0]) state.hour = hours[0];
      renderHours();
    }).catch(function () {
      if (state.date !== requestedDate || state.sede.id !== requestedSedeId) return;
      state.hours = [];
      els.hours.innerHTML = "";
      showError(els.hourError, "No pudimos cargar los horarios. Reintentá.");
    });
  }

  function showError(node, msg, retry) {
    node.setAttribute("role", "alert");
    node.hidden = false;
    node.textContent = "";
    node.appendChild(document.createTextNode(msg));
    if (typeof retry === "function") {
      node.appendChild(document.createTextNode(" "));
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "text-link";
      btn.textContent = "Reintentar";
      btn.addEventListener("click", retry);
      node.appendChild(btn);
    }
    announce(msg);
  }
  function clearError(node) {
    node.hidden = true;
    node.textContent = "";
  }

  var FIELD_DOM = {
    first: "first-name",
    last: "last-name",
    email: "email",
    "email-confirm": "email-confirm",
    phone: "phone"
  };
  var PHONE_LETTERS_MSG = "El celular no puede tener letras. Ejemplo: 11 5555 5555";

  function fieldWrap(id) { return document.getElementById("field-" + id); }
  function setFieldError(id, msg) {
    var wrap = fieldWrap(id);
    var inputId = FIELD_DOM[id] || id;
    var input = document.getElementById(inputId);
    var err = document.getElementById(inputId + "-error");
    if (wrap) wrap.classList.toggle("is-error", !!msg);
    if (input) {
      input.setAttribute("aria-invalid", msg ? "true" : "false");
    }
    if (err) {
      err.hidden = !msg;
      err.textContent = msg || "";
    }
    announce(msg || "");
  }

  function hasLetters(s) { return /[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]/.test(s); }
  function phoneDigits(s) { return (s.match(/\d/g) || []).join(""); }

  var KNOWN_DOMAINS = [
    "gmail.com", "hotmail.com", "outlook.com", "yahoo.com.ar", "yahoo.com", "live.com", "icloud.com"
  ];

  function levenshtein(a, b) {
    var prev = [];
    var i, j;
    for (j = 0; j <= b.length; j++) prev[j] = j;
    for (i = 1; i <= a.length; i++) {
      var row = [i];
      for (j = 1; j <= b.length; j++) {
        var cost = a.charAt(i - 1) === b.charAt(j - 1) ? 0 : 1;
        row[j] = Math.min(prev[j] + 1, row[j - 1] + 1, prev[j - 1] + cost);
      }
      prev = row;
    }
    return prev[b.length];
  }

  function suggestEmail(email) {
    var at = email.lastIndexOf("@");
    if (at < 1) return null;
    var local = email.slice(0, at);
    var domain = email.slice(at + 1).toLowerCase();
    if (!domain || KNOWN_DOMAINS.indexOf(domain) !== -1) return null;
    var best = null;
    var bestDist = 3;
    KNOWN_DOMAINS.forEach(function (known) {
      var dist = levenshtein(domain, known);
      if (dist > 0 && dist < bestDist) {
        bestDist = dist;
        best = known;
      }
    });
    return best ? local + "@" + best : null;
  }

  function renderEmailSuggest() {
    var box = document.getElementById("email-suggest");
    var email = document.getElementById("email").value.trim();
    var suggested = messageEmail(email) ? null : suggestEmail(email);
    if (!suggested) {
      box.hidden = true;
      return;
    }
    document.getElementById("email-suggest-lead").textContent = "¿Quisiste decir ";
    document.getElementById("email-suggest-accept").textContent = suggested;
    document.getElementById("email-suggest-tail").textContent = "?";
    box.hidden = false;
  }

  function messageFirst(v) { return v ? "" : "Escribí tu nombre."; }
  function messageLast(v) { return v ? "" : "Escribí tu apellido."; }
  function messageEmail(v) {
    if (!v) return "Escribí tu email.";
    if (v.indexOf("@") === -1 || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v)) {
      return "El email no es válido. Ejemplo: maria.lopez@gmail.com";
    }
    return "";
  }
  function messageEmailConfirm(v) {
    var email = document.getElementById("email").value.trim();
    if (!v) return "Confirmá tu email.";
    if (v.toLowerCase() !== email.toLowerCase()) return "Los emails no coinciden.";
    return "";
  }
  function messagePhone(v) {
    if (!v) return "Escribí tu celular.";
    if (hasLetters(v)) return PHONE_LETTERS_MSG;
    if (phoneDigits(v).length < 8) return "El celular es demasiado corto.";
    return "";
  }

  var FIELD_VALIDATORS = {
    "first-name": { key: "first", read: function () { return document.getElementById("first-name").value.trim(); }, message: messageFirst },
    "last-name": { key: "last", read: function () { return document.getElementById("last-name").value.trim(); }, message: messageLast },
    "email": { key: "email", read: function () { return document.getElementById("email").value.trim(); }, message: messageEmail },
    "email-confirm": { key: "email-confirm", read: function () { return document.getElementById("email-confirm").value.trim(); }, message: messageEmailConfirm },
    "phone": { key: "phone", read: function () { return document.getElementById("phone").value.trim(); }, message: messagePhone }
  };

  function validateField(domId) {
    var spec = FIELD_VALIDATORS[domId];
    if (!spec) return "";
    var msg = spec.message(spec.read());
    setFieldError(spec.key, msg);
    return msg;
  }

  function validateDatos() {
    var errors = [];
    Object.keys(FIELD_VALIDATORS).forEach(function (domId) {
      var msg = validateField(domId);
      if (msg) errors.push({ id: domId, msg: msg });
    });
    state.patient = {
      firstName: FIELD_VALIDATORS["first-name"].read(),
      lastName: FIELD_VALIDATORS["last-name"].read(),
      email: FIELD_VALIDATORS["email"].read(),
      phone: FIELD_VALIDATORS["phone"].read(),
      notes: document.getElementById("notes").value.trim()
    };
    return errors;
  }

  function renderErrorSummary(errors) {
    if (!errors.length) {
      els.errorSummary.hidden = true;
      return;
    }
    els.errorTitle.textContent = errors.length === 1 ? "Hay 1 error" : "Hay " + errors.length + " errores";
    els.errorList.innerHTML = "";
    errors.forEach(function (e) {
      var li = document.createElement("li");
      var a = document.createElement("a");
      a.href = "#" + e.id;
      a.textContent = e.msg;
      a.addEventListener("click", function (ev) {
        ev.preventDefault();
        document.getElementById(e.id).focus();
      });
      li.appendChild(a);
      els.errorList.appendChild(li);
    });
    els.errorSummary.hidden = false;
    els.errorSummary.focus();
  }

  function renderSummary() {
    function row(key, value, step) {
      var change = step
        ? '<button type="button" class="change" data-goto="' + step + '">Cambiar</button>'
        : "";
      return '<div class="summary-row"><div><dt>' + key + "</dt><dd>" + value +
        "</dd></div>" + change + "</div>";
    }
    els.summary.innerHTML =
      row("Servicio", "Evaluación de pisada") +
      row("Sede", state.sede.name, 1) +
      row("Fecha", formatLong(state.date), 2) +
      row("Hora", state.hour, 3) +
      row("Duración", "30 minutos") +
      row("Seña", "$20.000");
    els.summary.querySelectorAll(".change").forEach(function (btn) {
      btn.addEventListener("click", function () {
        setStep(Number(btn.getAttribute("data-goto")));
      });
    });
  }

  function payload() {
    return {
      service_id: state.sede.serviceId,
      provider_id: state.sede.providerId,
      selected_date: state.date,
      selected_hour: state.hour,
      service_duration: state.sede.duration,
      customer: {
        first_name: state.patient.firstName,
        last_name: state.patient.lastName,
        email: state.patient.email,
        phone_number: state.patient.phone,
        notes: state.patient.notes
      },
      terms_accepted: document.getElementById("terms").checked,
      privacy_accepted: document.getElementById("privacy").checked,
      website: (document.getElementById("website") || {}).value || "",
      form_started_at: state.formStartedAt,
      idempotency_key: ensureIdempotencyKey()
    };
  }

  function turnoFingerprint() {
    return String(state.sede.serviceId) + ":" + String(state.sede.providerId) + ":" +
      String(state.date) + ":" + String(state.hour);
  }

  function uuidv4() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    var bytes = new Uint8Array(16);
    if (window.crypto && window.crypto.getRandomValues) {
      window.crypto.getRandomValues(bytes);
    } else {
      for (var i = 0; i < 16; i++) bytes[i] = Math.floor(Math.random() * 256);
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    var hex = Array.prototype.map.call(bytes, function (b) {
      return (b + 256).toString(16).slice(1);
    }).join("");
    return hex.slice(0, 8) + "-" + hex.slice(8, 12) + "-" + hex.slice(12, 16) +
      "-" + hex.slice(16, 20) + "-" + hex.slice(20);
  }

  function ensureIdempotencyKey() {
    var fp = turnoFingerprint();
    if (!state.idempotencyKey || state.idempotencyFp !== fp) {
      state.idempotencyKey = uuidv4();
      state.idempotencyFp = fp;
    }
    return state.idempotencyKey;
  }

  function setConfirmBusy(busy) {
    state.submitting = busy;
    els.ctaConfirm.disabled = busy || state.booked;
    els.ctaConfirm.setAttribute("aria-busy", busy ? "true" : "false");
    els.ctaConfirm.textContent = busy ? "Reservando…" : "Pedir la evaluación";
  }

  function appendErrorLink(node, href, label) {
    node.appendChild(document.createTextNode(" "));
    var a = document.createElement("a");
    a.href = href;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.className = "text-link";
    a.textContent = label;
    node.appendChild(a);
  }

  function showConfirmError(msg, opts) {
    opts = opts || {};
    var retry = opts.retry;
    var retryLabel = opts.retryLabel;
    showError(els.confirmError, msg, retry ? function () {
      retry();
    } : undefined);
    if (retry && retryLabel) {
      var btn = els.confirmError.querySelector("button.text-link");
      if (btn) btn.textContent = retryLabel;
    }
    if (opts.whatsapp) {
      appendErrorLink(els.confirmError, WHATSAPP_URL, "Escribinos por WhatsApp");
    }
  }

  function parseJsonSafe(text) {
    if (!text) return {};
    try { return JSON.parse(text); } catch (e) { return {}; }
  }

  function fieldTarget(name) {
    var key = String(name || "").replace(/^customer\./, "");
    var map = {
      first_name: { step: 4, id: "first-name", key: "first", msg: "Revisá tu nombre." },
      last_name: { step: 4, id: "last-name", key: "last", msg: "Revisá tu apellido." },
      email: { step: 4, id: "email", key: "email", msg: "Revisá tu email." },
      phone: { step: 4, id: "phone", key: "phone", msg: "Revisá tu celular." },
      phone_number: { step: 4, id: "phone", key: "phone", msg: "Revisá tu celular." },
      notes: { step: 4, id: "notes" },
      selected_date: { step: 2, msg: "Elegí otro día." },
      selected_hour: { step: 3, msg: "Elegí otro horario." },
      terms_accepted: { step: 5, id: "terms", msg: "Tenés que aceptar los Términos y Condiciones." },
      privacy_accepted: { step: 5, id: "privacy", msg: "Tenés que aceptar la Política de Privacidad." }
    };
    return map[key] || null;
  }

  function goToField(target) {
    if (!target) return;
    if (target.step && target.step !== state.step) {
      setStep(target.step);
      if (target.step === 2) loadMonth();
      if (target.step === 3) loadHours();
    }
    if (target.id) {
      var el = document.getElementById(target.id);
      if (el) el.focus();
    }
  }

  function businessMessage(code) {
    if (code === "requested_hour_is_unavailable") {
      return "El horario que elegiste se acaba de ocupar, elegí otro.";
    }
    if (code === "customer_is_already_booked") {
      return "Ya tenés un turno en ese horario. Elegí otro, o escribinos si necesitás cambiarlo.";
    }
    if (code === "patient_already_booked_that_day") {
      return "Ya tenés un turno este día. Si es para otra persona, usá su nombre.";
    }
    if (code === "invalid_request") {
      return "Revisá los datos e intentá de nuevo.";
    }
    return "No pudimos reservar ese turno. Elegí otro horario o escribinos.";
  }

  function showSuccess(appointmentId) {
    state.booked = true;
    setConfirmBusy(false);
    els.ctaConfirm.hidden = true;
    els.ctaConfirm.disabled = true;
    if (els.checks) els.checks.hidden = true;
    clearError(els.confirmError);
    document.getElementById("title-5").textContent = "Turno reservado";
    els.bookingResult.textContent = "";
    var title = document.createElement("p");
    title.className = "pay-title";
    title.textContent = "Turno reservado";
    var when = document.createElement("p");
    when.textContent = formatLong(state.date) + " · " + state.hour + " · " + state.sede.name;
    var who = document.createElement("p");
    who.textContent = state.patient.firstName + " " + state.patient.lastName;
    var num = document.createElement("p");
    num.textContent = "Número de reserva: " + appointmentId + ".";
    els.bookingResult.appendChild(title);
    els.bookingResult.appendChild(when);
    els.bookingResult.appendChild(who);
    els.bookingResult.appendChild(num);
    els.bookingResult.hidden = false;
    announce("Turno reservado. " + when.textContent + ". Número de reserva: " + appointmentId + ".");
    els.bookingResult.focus();
  }

  function handleBookingFailure(status, body) {
    setConfirmBusy(false);
    var code = body && body.code;
    var fields = (body && Array.isArray(body.fields)) ? body.fields : [];

    if (status === 409 || code === "requested_hour_is_unavailable" ||
        code === "customer_is_already_booked" || code === "patient_already_booked_that_day") {
      showConfirmError(businessMessage(code), {
        retry: function () {
          clearError(els.confirmError);
          setStep(3);
          loadHours();
        },
        retryLabel: "Elegí otro horario"
      });
      return;
    }

    if (status === 400 || code === "invalid_request") {
      if (fields.length) {
        var first = fieldTarget(fields[0]);
        fields.forEach(function (name) {
          var t = fieldTarget(name);
          if (t && t.key) setFieldError(t.key, t.msg);
          if (t && (t.id === "terms" || t.id === "privacy")) {
            showError(document.getElementById(t.id + "-error"), t.msg);
          }
        });
        showConfirmError(first && first.msg ? first.msg : businessMessage("invalid_request"));
        goToField(first);
        return;
      }
      showConfirmError(businessMessage(code || "invalid_request"));
      return;
    }

    showConfirmError(
      "No pudimos reservar el turno. El problema es nuestro, no tuyo.",
      { whatsapp: true }
    );
  }

  function postReservation(data) {
    if (WRITE_RE.test(String(BOOKING_URL))) {
      throw new Error("Blocked write to production agenda");
    }
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, FETCH_TIMEOUT_MS);
    return fetch(BOOKING_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify(data),
      cache: "no-store",
      signal: controller.signal
    }).then(function (res) {
      return res.text().then(function (text) {
        return { res: res, body: parseJsonSafe(text) };
      });
    }).finally(function () { clearTimeout(timer); });
  }

  els.ctaSede.addEventListener("click", function () {
    var t = todayAR();
    state.year = t.y;
    state.month = t.m;
    state.date = null;
    state.hour = null;
    state.hoursFetch = null;
    setStep(2);
    loadMonth();
  });

  els.calPrev.addEventListener("click", function () {
    state.month -= 1;
    if (state.month < 1) { state.month = 12; state.year -= 1; }
    state.date = null;
    state.hoursFetch = null;
    loadMonth();
  });
  els.calNext.addEventListener("click", function () {
    state.month += 1;
    if (state.month > 12) { state.month = 1; state.year += 1; }
    state.date = null;
    state.hoursFetch = null;
    loadMonth();
  });

  els.ctaDay.addEventListener("click", function () {
    if (!state.date) {
      showError(els.dayError, "Elegí un día con turno.");
      return;
    }
    setStep(3);
    loadHours();
  });

  els.ctaHour.addEventListener("click", function () {
    if (!state.hour) {
      showError(els.hourError, "Elegí un horario.");
      return;
    }
    setStep(4);
  });

  els.form.addEventListener("submit", function (e) {
    e.preventDefault();
    var errors = validateDatos();
    renderErrorSummary(errors);
    if (errors.length) {
      document.getElementById(errors[0].id).focus();
      return;
    }
    renderSummary();
    els.bookingResult.hidden = true;
    clearError(els.confirmError);
    setStep(5);
  });

  ["first-name", "last-name", "email", "email-confirm", "phone"].forEach(function (id) {
    var input = document.getElementById(id);
    input.addEventListener("blur", function () {
      validateField(id);
      if (id === "email") renderEmailSuggest();
    });
  });

  document.getElementById("email").addEventListener("input", renderEmailSuggest);
  document.getElementById("email-suggest-accept").addEventListener("click", function () {
    var suggested = this.textContent;
    var emailInput = document.getElementById("email");
    var confirmInput = document.getElementById("email-confirm");
    var old = emailInput.value;
    emailInput.value = suggested;
    if (!confirmInput.value.trim() || confirmInput.value.trim() === old.trim()) {
      confirmInput.value = suggested;
    }
    document.getElementById("email-suggest").hidden = true;
    validateField("email");
    validateField("email-confirm");
  });

  document.getElementById("phone").addEventListener("input", function () {
    if (hasLetters(this.value)) {
      setFieldError("phone", PHONE_LETTERS_MSG);
    } else if (this.value.trim()) {
      setFieldError("phone", "");
    }
  });

  els.ctaConfirm.addEventListener("click", function () {
    if (state.submitting || state.booked) return;
    var ok = true;
    if (!document.getElementById("terms").checked) {
      showError(document.getElementById("terms-error"), "Tenés que aceptar los Términos y Condiciones.");
      document.getElementById("terms").setAttribute("aria-invalid", "true");
      ok = false;
    } else {
      clearError(document.getElementById("terms-error"));
      document.getElementById("terms").setAttribute("aria-invalid", "false");
    }
    if (!document.getElementById("privacy").checked) {
      showError(document.getElementById("privacy-error"), "Tenés que aceptar la Política de Privacidad.");
      document.getElementById("privacy").setAttribute("aria-invalid", "true");
      ok = false;
    } else {
      clearError(document.getElementById("privacy-error"));
      document.getElementById("privacy").setAttribute("aria-invalid", "false");
    }
    if (!ok) {
      document.getElementById("terms").focus();
      return;
    }
    clearError(els.confirmError);
    els.bookingResult.hidden = true;
    var data = payload();
    setConfirmBusy(true);
    announce("Reservando tu turno.");
    postReservation(data).then(function (result) {
      if (result.res.status === 201 && result.body && result.body.ok === true &&
          result.body.appointment_id != null) {
        showSuccess(result.body.appointment_id);
        return;
      }
      handleBookingFailure(result.res.status, result.body || {});
    }).catch(function () {
      setConfirmBusy(false);
      showConfirmError(
        "No pudimos reservar el turno. El problema es nuestro, no tuyo.",
        { whatsapp: true }
      );
    });
  });

  document.querySelectorAll("[data-back]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var next = Math.max(1, state.step - 1);
      if (next === 1) {
        state.monthFetch = null;
        state.hoursFetch = null;
        prefetchSelectedSede();
      }
      setStep(next);
    });
  });

  document.querySelectorAll("[data-open]").forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      document.getElementById(btn.getAttribute("data-open")).showModal();
    });
  });

  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    document.querySelectorAll("dialog[open]").forEach(function (d) { d.close(); });
  });

  renderSedes();
  setStep(1, true); // primer render: no scrollear, el paciente recien abrio la pagina
  prefetchSelectedSede();
})();
