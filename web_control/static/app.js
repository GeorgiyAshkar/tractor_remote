"use strict";

(() => {
  const ACTIONS = Object.freeze({
    left_forward: "левая гусеница вперёд",
    left_backward: "левая гусеница назад",
    right_forward: "правая гусеница вперёд",
    right_backward: "правая гусеница назад",
    forward: "обе гусеницы вперёд",
    tower_left: "башня влево",
    tower_right: "башня вправо",
    boom_up: "стрела вверх",
    boom_down: "стрела вниз",
  });
  const KEY_ACTIONS = Object.freeze({
    KeyW: "forward", KeyQ: "left_forward", KeyA: "left_backward",
    KeyE: "right_forward", KeyD: "right_backward", KeyJ: "tower_left",
    KeyL: "tower_right", KeyI: "boom_up", KeyK: "boom_down",
  });
  const BOARD_COPY = Object.freeze({
    missing: ["Нет платы", "Подключите RF-Nano к USB", "Пульт найдёт плату автоматически после подключения."],
    connecting: ["Подключение…", "Подключаем плату", "Подождите немного — устанавливаем связь с платой."],
    ready: ["Плата готова", "Плата готова", "USB подключён. Подтвердите питание модели для управления."],
    firmware_required: ["Нужна прошивка", "Нужна прошивка пульта", "Подключённой плате нужна совместимая прошивка пульта."],
    error: ["Ошибка подключения", "Не удалось подключить плату", "Проверьте USB-подключение платы."],
  });
  const el = (id) => document.getElementById(id);
  const buttons = [...document.querySelectorAll("[data-action]")];
  const powerInput = el("tractorPower");
  const randomBytes = new Uint8Array(16);
  crypto.getRandomValues(randomBytes);
  const clientId = [...randomBytes].map((value) => value.toString(16).padStart(2, "0")).join("");

  let sequence = 0;
  let status = null;
  let csrfToken = "";
  let reachable = false;
  let haveStatus = false;
  let polling = null;
  let powerPending = false;
  let intent = null;
  let activeHold = null;
  let holdTimer = null;
  let intentVersion = 0;
  let stopping = false;
  let lastBoardState = null;
  let lastBoardToken;
  let lastPort = null;
  let lastToast = { text: "", at: 0 };
  let suppressCheckboxSpace = false;
  const heldKeys = new Set();
  const seenEvents = new Set();
  const recentEvents = [];

  const nextSequence = () => ++sequence;
  const boardToken = () => status?.board?.token;
  const tokenExists = (value) => (typeof value === "string" && value.length > 0)
    || (typeof value === "number" && Number.isFinite(value));
  const sameToken = (a, b) => typeof a === typeof b && a === b;
  const canControl = () => reachable && tokenExists(boardToken())
    && status?.board?.state === "ready" && status?.tractor?.confirmed === true
    && status?.control?.enabled === true && !powerPending;

  function toast(text, level = "info") {
    if (!text || (lastToast.text === text && Date.now() - lastToast.at < 5000)) return;
    lastToast = { text, at: Date.now() };
    const node = document.createElement("div");
    node.className = "toast";
    node.dataset.level = level;
    node.textContent = text;
    el("toasts").append(node);
    while (el("toasts").children.length > 3) el("toasts").firstElementChild.remove();
    setTimeout(() => node.remove(), 5500);
  }

  function renderEvents() {
    const list = el("eventList");
    list.replaceChildren();
    if (!recentEvents.length) {
      const node = document.createElement("li");
      node.textContent = "Событий пока нет.";
      list.append(node);
      return;
    }
    for (const item of recentEvents.slice(-5).reverse()) {
      const row = document.createElement("li");
      const time = document.createElement("time");
      const date = new Date(item.time || Date.now());
      time.textContent = Number.isNaN(date.getTime()) ? "—" : date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
      row.append(time, document.createTextNode(String(item.message || "Событие подключения")));
      list.append(row);
    }
  }

  function ingestEvents(events, initial) {
    if (!Array.isArray(events)) return;
    for (const item of events) {
      if (item?.id === undefined || item?.id === null) continue;
      const id = String(item.id);
      if (seenEvents.has(id)) continue;
      seenEvents.add(id);
      recentEvents.push(item);
      if (!initial && item.message && ["warning", "error"].includes(item.level)) {
        toast(String(item.message), item.level);
      }
    }
    while (recentEvents.length > 12) recentEvents.shift();
    // Event ids are process-local; retain enough history to avoid poll repeats.
    if (seenEvents.size > 2000) {
      const latest = [...seenEvents].slice(-1000);
      seenEvents.clear();
      latest.forEach((id) => seenEvents.add(id));
    }
    renderEvents();
  }

  function render() {
    const boardState = reachable ? (status?.board?.state || "connecting") : (haveStatus ? "unavailable" : "connecting");
    const copy = BOARD_COPY[boardState] || BOARD_COPY.error;
    const unavailable = boardState === "unavailable";
    el("connectionPill").dataset.state = boardState;
    el("connectionLabel").textContent = unavailable ? "Сервис недоступен" : copy[0];
    el("boardTitle").textContent = unavailable ? "Нет связи с локальным пультом" : copy[1];
    el("boardDetail").textContent = unavailable
      ? "Проверьте, что локальный сервис запущен. Управление временно отключено."
      : (status?.board?.message || copy[2]);
    const demo = status?.mode === "demo";
    el("demoBadge").hidden = !demo;
    powerInput.disabled = !reachable || boardState !== "ready" || !tokenExists(boardToken()) || powerPending;
    if (!powerPending) powerInput.checked = status?.tractor?.confirmed === true;
    for (const button of buttons) {
      button.disabled = !canControl();
      button.setAttribute("aria-pressed", String(activeHold?.action === button.dataset.action));
    }

    let notice = "Элементы управления станут доступны после подключения платы и подтверждения питания.";
    let kind = "info";
    if (unavailable) {
      notice = "Сервис недоступен. Удержание сброшено; после восстановления нажмите кнопку заново.";
      kind = "error";
    } else if (boardState !== "ready") {
      notice = boardState === "missing" ? "Подключите RF-Nano к USB. Управление пока отключено."
        : boardState === "connecting" ? "Дождитесь готовности платы, затем подтвердите питание трактора."
          : "Управление отключено до восстановления подключения платы.";
      if (boardState === "error" || boardState === "firmware_required") kind = "error";
    } else if (!tokenExists(boardToken())) {
      notice = "Сервис не сообщил состояние подключения платы. Обновите локальный пульт.";
      kind = "error";
    } else if (powerPending) {
      notice = "Сохраняем подтверждение питания…";
    } else if (!status?.tractor?.confirmed) {
      notice = "Включите трактор, выключите штатный пульт и подтвердите питание галочкой выше.";
    } else if (!status?.control?.enabled) {
      notice = "Управление пока недоступно. Дождитесь готовности пульта.";
    } else if (demo) {
      notice = "Деморежим: команды видны на экране, радиопередача не используется.";
      kind = "demo";
    } else {
      notice = "Пульт готов. Для передачи команды удерживайте кнопку; отпустите, чтобы прекратить передачу.";
      kind = "ready";
    }
    el("notice").dataset.kind = kind;
    el("noticeText").textContent = notice;
    const shownAction = activeHold?.action || (reachable ? status?.control?.action : null);
    const transmitting = Boolean(shownAction && ACTIONS[shownAction] && canControl());
    el("commandStatus").dataset.active = String(transmitting);
    el("commandLabel").textContent = stopping ? "Прекращаем передачу…"
      : transmitting ? `${demo ? "Демо-команда" : "Команда"}: ${ACTIONS[shownAction]}` : "Передача неактивна";
  }

  async function request(path, body, { keepalive = false } = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), body === undefined ? 1400 : 900);
    try {
      const response = await fetch(path, {
        method: body === undefined ? "GET" : "POST",
        credentials: "same-origin", cache: "no-store", mode: "same-origin",
        headers: body === undefined ? { Accept: "application/json" }
          : { "Content-Type": "application/json", "X-CSRF-Token": csrfToken, Accept: "application/json" },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        signal: controller.signal, keepalive,
      });
      if (!response.ok) {
        const error = new Error(`Сервис отклонил запрос (${response.status}).`);
        error.httpStatus = response.status;
        throw error;
      }
      return body === undefined ? await response.json() : null;
    } finally {
      clearTimeout(timeout);
    }
  }

  function clearLocalHold() {
    ++intentVersion;
    intent = null;
    activeHold = null;
    if (holdTimer !== null) clearInterval(holdTimer);
    holdTimer = null;
    heldKeys.clear();
    render();
  }

  function bestEffortStop() {
    if (!csrfToken) return Promise.resolve();
    const body = { client_id: clientId, seq: nextSequence() };
    return request("/api/stop", body, { keepalive: true }).catch(() => {});
  }

  function connectionFailure(error, notify = true) {
    const wasReachable = reachable;
    reachable = false;
    haveStatus = true;
    clearLocalHold();
    stopping = false;
    render();
    void bestEffortStop();
    if (wasReachable && notify) toast(error?.httpStatus === 409
      ? "Подключение изменилось. Удержание сброшено — нажмите кнопку заново."
      : "Связь с локальным пультом потеряна. Передача команды прекращается.", "error");
  }

  async function stopAll({ force = false, quiet = false } = {}) {
    const hadIntent = Boolean(intent || activeHold);
    clearLocalHold();
    if ((!hadIntent && !force) || !csrfToken) return;
    stopping = true;
    render();
    const seq = nextSequence();
    const tokenAtStop = boardToken();
    try {
      await request("/api/stop", { client_id: clientId, seq }, { keepalive: true });
      if (status?.control && !intent && seq === sequence && sameToken(tokenAtStop, boardToken())) {
        status.control = { ...status.control, action: null, state: "stopped" };
      }
    } catch (error) {
      connectionFailure(error, !quiet);
    } finally {
      stopping = false;
      render();
    }
  }

  function sendLease(hold) {
    if (activeHold !== hold || intent !== hold || !canControl() || !sameToken(hold.boardToken, boardToken())) return;
    const body = { client_id: clientId, seq: nextSequence(), action: hold.action, board_token: hold.boardToken };
    void request("/api/control", body).catch(connectionFailure);
  }

  async function beginHold(action, gesture) {
    if (!ACTIONS[action] || !canControl()) return;
    if (intent?.kind === gesture.kind && intent?.id === gesture.id) return;
    const hadPrevious = Boolean(intent || activeHold);
    if (holdTimer !== null) clearInterval(holdTimer);
    holdTimer = null;
    activeHold = null;
    const hold = { action, ...gesture, version: ++intentVersion, boardToken: boardToken() };
    intent = hold;
    render();
    if (hadPrevious) {
      try {
        // A change of action is acknowledged as a stop before the next lease.
        await request("/api/stop", { client_id: clientId, seq: nextSequence() }, { keepalive: true });
      } catch (error) {
        connectionFailure(error);
        return;
      }
    }
    if (intent !== hold || hold.version !== intentVersion || !canControl()
        || !sameToken(hold.boardToken, boardToken())) return;
    activeHold = hold;
    sendLease(hold);
    holdTimer = setInterval(() => sendLease(hold), 100);
    render();
  }

  function releaseGesture(kind, id) {
    if (intent?.kind === kind && intent?.id === id) void stopAll();
  }

  async function pollStatus() {
    if (polling) return polling;
    polling = (async () => {
      try {
        const fresh = await request("/api/status");
        if (!fresh || typeof fresh !== "object" || !fresh.board || !fresh.control
            || typeof fresh.csrf_token !== "string" || !fresh.csrf_token) throw new Error("Некорректное состояние сервиса");
        const initial = !haveStatus;
        const recovered = haveStatus && !reachable;
        const newToken = fresh.board.token;
        const tokenChanged = haveStatus && !sameToken(lastBoardToken, newToken);
        const csrfChanged = Boolean(csrfToken && csrfToken !== fresh.csrf_token);
        const portChanged = haveStatus && lastPort !== (fresh.board.port || null);
        const boardBecameUnavailable = fresh.board.state !== "ready";
        const mustRelease = tokenChanged || csrfChanged || portChanged || boardBecameUnavailable
          || fresh.control.enabled !== true;
        const hadHeldInput = Boolean(intent || activeHold);
        if (csrfChanged) { seenEvents.clear(); recentEvents.length = 0; }
        csrfToken = fresh.csrf_token;
        status = fresh;
        reachable = true;
        haveStatus = true;
        if (mustRelease && hadHeldInput) {
          clearLocalHold();
          void bestEffortStop();
          toast("Удержание сброшено. Нажмите кнопку заново.", "info");
        }
        if (!initial && fresh.board.state !== lastBoardState) {
          if (fresh.board.state === "ready") toast("Плата готова. Подтвердите питание трактора.", "success");
          else if (lastBoardState === "ready") toast("Плата отключена. Удержание команды сброшено.", "error");
          else if (fresh.board.state === "firmware_required") toast("Плате нужна прошивка пульта.", "info");
        }
        if (tokenChanged && fresh.board.state === "ready" && lastBoardState === "ready"
            && !fresh.tractor?.confirmed && !hadHeldInput && !powerPending) {
          toast("Готовность управления сброшена. Подтвердите питание трактора.", "info");
        }
        if (recovered) toast("Связь с локальным пультом восстановлена. Нажмите кнопку заново.", "success");
        lastBoardState = fresh.board.state;
        lastBoardToken = newToken;
        lastPort = fresh.board.port || null;
        ingestEvents(fresh.events, initial || csrfChanged);
        render();
      } catch (error) {
        connectionFailure(error);
      } finally {
        polling = null;
      }
    })();
    return polling;
  }

  powerInput.addEventListener("change", async () => {
    const confirmed = powerInput.checked;
    const token = boardToken();
    if (!reachable || !tokenExists(token)) { render(); return; }
    powerPending = true;
    render();
    try {
      if (!confirmed) await stopAll({ force: true });
      await request("/api/power", { confirmed, board_token: token });
      // An already-running poll may predate the power change. Finish it, then
      // fetch a fresh state before making any control available again.
      if (polling) await polling;
      await pollStatus();
    } catch (error) {
      connectionFailure(error);
    } finally {
      powerPending = false;
      render();
    }
  });

  for (const button of buttons) {
    button.setAttribute("aria-pressed", "false");
    button.addEventListener("contextmenu", (event) => event.preventDefault());
    button.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || button.disabled) return;
      event.preventDefault();
      try { button.setPointerCapture(event.pointerId); } catch (_) { /* Window release remains a fallback. */ }
      void beginHold(button.dataset.action, { kind: "pointer", id: event.pointerId });
    });
    button.addEventListener("lostpointercapture", (event) => releaseGesture("pointer", event.pointerId));
    button.addEventListener("keydown", (event) => {
      if (event.code !== "Enter") return;
      event.preventDefault();
      if (event.repeat || button.disabled) return;
      heldKeys.add("Enter");
      void beginHold(button.dataset.action, { kind: "key", id: "Enter" });
    });
  }
  window.addEventListener("pointerup", (event) => releaseGesture("pointer", event.pointerId));
  window.addEventListener("pointercancel", (event) => releaseGesture("pointer", event.pointerId));
  const editable = (target) => target instanceof Element
    && Boolean(target.closest("input:not([type=checkbox]):not([type=radio]):not([type=button]):not([type=submit]):not([type=reset]),textarea,select,[contenteditable]:not([contenteditable=false])"));
  const checkboxTarget = (target) => target instanceof Element
    && Boolean(target.closest('input[type="checkbox"]'));
  window.addEventListener("keydown", (event) => {
    if (event.code === "Escape") {
      event.preventDefault();
      if (!event.repeat) void stopAll({ force: true });
      return;
    }
    if (editable(event.target) || event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.code === "Space") {
      // A focused power checkbox keeps its native Space activation when idle.
      // During a hold Space is STOP, including its repeat and keyup events.
      if (checkboxTarget(event.target)) {
        if (!intent && !activeHold && !suppressCheckboxSpace) return;
        suppressCheckboxSpace = true;
      }
      event.preventDefault();
      if (!event.repeat) void stopAll({ force: true });
      return;
    }
    const action = KEY_ACTIONS[event.code];
    if (!action) return;
    event.preventDefault();
    if (event.repeat || heldKeys.has(event.code)) return;
    heldKeys.add(event.code);
    void beginHold(action, { kind: "key", id: event.code });
  });
  window.addEventListener("keyup", (event) => {
    const wasHeld = heldKeys.delete(event.code);
    const nativeCheckboxSpace = event.code === "Space" && checkboxTarget(event.target)
      && !suppressCheckboxSpace;
    if (event.code === "Space" && suppressCheckboxSpace) {
      event.preventDefault();
      suppressCheckboxSpace = false;
    } else if (!nativeCheckboxSpace && (wasHeld
        || (!editable(event.target) && (KEY_ACTIONS[event.code] || event.code === "Space")))) {
      event.preventDefault();
    }
    releaseGesture("key", event.code);
  });
  el("stopButton").addEventListener("click", () => void stopAll({ force: true }));
  window.addEventListener("blur", () => {
    suppressCheckboxSpace = false;
    void stopAll({ quiet: true });
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) void stopAll({ quiet: true });
    else void pollStatus();
  });
  window.addEventListener("pagehide", () => {
    const hadLocalIntent = Boolean(intent || activeHold);
    clearLocalHold();
    if (hadLocalIntent) void bestEffortStop();
  });
  window.addEventListener("offline", () => connectionFailure(new Error("offline")));
  render();
  void pollStatus();
  setInterval(() => { if (!document.hidden) void pollStatus(); }, 500);
})();
