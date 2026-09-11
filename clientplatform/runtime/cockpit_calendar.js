(() => {
  "use strict";

  const view = document.getElementById("calendar-view");
  const select = document.getElementById("business-select");
  const refresh = document.getElementById("calendar-refresh");
  const more = document.getElementById("calendar-more");
  const manage = document.getElementById("calendar-manage");
  const advanced = document.getElementById("calendar-advanced");
  const panel = document.getElementById("calendar-manage-panel");
  const form = document.getElementById("calendar-form");
  const formTitle = document.getElementById("calendar-form-title");
  const offering = document.getElementById("calendar-offering");
  const start = document.getElementById("calendar-start");
  const duration = document.getElementById("calendar-duration");
  const save = document.getElementById("calendar-save");
  const formCancel = document.getElementById("calendar-form-cancel");
  const manageMessage = document.getElementById("calendar-manage-message");
  const meta = document.getElementById("calendar-meta");
  const list = document.getElementById("calendar-list");
  const empty = document.getElementById("calendar-empty");
  const limitations = document.getElementById("calendar-limitations");
  const tg = window.Telegram && window.Telegram.WebApp;
  const initData = tg && typeof tg.initData === "string" ? tg.initData : "";

  let calendarPayload = null;
  let management = null;
  let managementUnavailable = false;
  let editingSlotId = null;

  const text = (node, value) => { node.textContent = value == null ? "" : String(value); };
  const controller = () => window.ClientPlatformCockpitNavigation;
  const captureContext = () => controller().captureBusinessContext();
  const assertCurrent = (snapshot, payload = null) => {
    controller().assertBusinessContextCurrent(snapshot);
    const payloadBusiness = String(payload && payload.business_id || "").trim();
    if (payloadBusiness && payloadBusiness !== snapshot.businessId) throw new Error("workspace_context_changed");
  };
  const contextChanged = (error) => controller().isContextChangedError(error);
  const focusView = () => controller().focusRegion(view);

  const setBusy = (busy) => {
    view.classList.toggle("busy", Boolean(busy));
    refresh.disabled = Boolean(busy);
    manage.disabled = Boolean(busy);
    advanced.disabled = Boolean(busy);
    save.disabled = Boolean(busy);
    view.setAttribute("aria-busy", busy ? "true" : "false");
  };

  const post = async (path, extra = {}, businessId = null) => {
    const body = {init_data: initData, ...extra};
    const targetBusiness = businessId === null ? String(select.value || "").trim() : businessId;
    if (targetBusiness) body.business_id = targetBusiness;
    const response = await fetch(path, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      credentials: "same-origin",
      cache: "no-store",
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({error: "invalid_response"}));
    if (!response.ok) throw new Error(payload.error || "calendar_unavailable");
    return payload;
  };

  const toWallClock = (value) => {
    const match = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/);
    return match ? `${match[3]}.${match[2]}.${match[1]} ${match[4]}:${match[5]}` : null;
  };

  const toInputClock = (value) => {
    const match = String(value || "").match(/^(\d{2})\.(\d{2})\.(\d{4}) (\d{2}):(\d{2})$/);
    return match ? `${match[3]}-${match[2]}-${match[1]}T${match[4]}:${match[5]}` : "";
  };

  const show = () => {
    const api = controller();
    if (api && typeof api.enterCalendar === "function") api.enterCalendar();
    else view.hidden = false;
  };

  const notifySuccess = () => {
    if (tg && tg.HapticFeedback && typeof tg.HapticFeedback.notificationOccurred === "function") {
      tg.HapticFeedback.notificationOccurred("success");
    }
  };

  const confirmAction = (message, action) => {
    if (tg && typeof tg.showConfirm === "function") {
      tg.showConfirm(message, (confirmed) => { if (confirmed) action(); });
      return;
    }
    if (window.confirm(message)) action();
  };

  const resetForm = () => {
    editingSlotId = null;
    text(formTitle, "Добавить свободное время");
    text(save, "Опубликовать время");
    offering.disabled = false;
    start.value = "";
    duration.value = "60";
    formCancel.hidden = true;
    text(manageMessage, management && management.offerings && management.offerings.length
      ? `Время вводится в часовом поясе бизнеса: ${management.timezone_name}.`
      : "Сначала нужна активная услуга. Существующие настройки услуг не изменялись.");
  };

  const renderManagement = () => {
    offering.replaceChildren();
    manage.hidden = !management;
    if (!management) {
      panel.hidden = true;
      return;
    }
    for (const item of management.offerings || []) {
      const option = document.createElement("option");
      option.value = item.id;
      text(option, item.title);
      offering.appendChild(option);
    }
    form.hidden = !(management.offerings || []).length && !editingSlotId;
    if (!editingSlotId) resetForm();
  };

  const mutationErrorText = (error) => {
    const code = error && error.message;
    if (code === "calendar_manage_denied") return "Для Вашей роли изменение расписания недоступно.";
    if (code === "calendar_slot_not_found") return "Это время уже изменилось или исчезло. Обновите расписание.";
    if (code === "calendar_change_rejected") return "Изменение отклонено: время могло быть занято клиентом, пересекаться с другим окном или измениться параллельно. Обновите расписание и проверьте ещё раз.";
    if (code === "invalid_calendar_change") return "Проверьте дату, время и длительность. Время должно быть будущим и корректным для часового пояса бизнеса.";
    return "Не удалось изменить расписание. Данные не были изменены; обновите экран и попробуйте снова.";
  };

  const mutate = async (path, payload, successText) => {
    const snapshot = captureContext();
    setBusy(true);
    text(manageMessage, "Сохраняем изменение…");
    try {
      await post(path, payload, snapshot.businessId);
      assertCurrent(snapshot);
      notifySuccess();
      editingSlotId = null;
      await load();
      if (controller().isBusinessContextCurrent(snapshot)) text(manageMessage, successText);
    } catch (error) {
      if (contextChanged(error)) return;
      text(manageMessage, mutationErrorText(error));
    } finally {
      if (controller().isBusinessContextCurrent(snapshot)) setBusy(false);
    }
  };

  const beginEdit = (item) => {
    if (!management) return;
    editingSlotId = item.slot_id;
    panel.hidden = false;
    form.hidden = false;
    text(formTitle, `Изменить свободное время · ${item.offering_title || "Услуга"}`);
    text(save, "Сохранить новое время");
    offering.disabled = true;
    start.value = toInputClock(item.local_start);
    duration.value = String(item.duration_minutes || 60);
    formCancel.hidden = false;
    text(manageMessage, `Услуга остаётся прежней. Новое время вводится в часовом поясе бизнеса: ${management.timezone_name}.`);
    start.focus();
  };

  const cancelSlot = (item) => {
    confirmAction(
      `Снять свободное время ${item.local_start} с публикации? Клиенты больше не увидят это окно.`,
      () => { void mutate("/clientplatform/cockpit/calendar/cancel", {slot_id: item.slot_id}, "Время снято с публикации."); },
    );
  };

  const render = (payload) => {
    calendarPayload = payload;
    list.replaceChildren();
    text(meta, `${payload.business_name} · ближайшее время`);
    const items = payload.items || [];
    for (const item of items) {
      const card = document.createElement("div");
      const top = document.createElement("div");
      const when = document.createElement("strong");
      const badge = document.createElement("span");
      const title = document.createElement("p");
      const slotDuration = document.createElement("small");
      card.className = `schedule-card ${item.status === "booked" ? "booked" : "open"}`;
      top.className = "schedule-card-top";
      badge.className = `schedule-status ${item.status === "booked" ? "booked" : "open"}`;
      text(when, item.local_start);
      text(badge, item.status === "booked" ? "Записано" : "Свободно");
      text(title, item.offering_title || "Услуга");
      text(slotDuration, item.duration_minutes ? `${item.duration_minutes} мин.` : "");
      top.append(when, badge);
      card.append(top, title, slotDuration);
      if (management && item.status === "open") {
        const actions = document.createElement("div");
        const edit = document.createElement("button");
        const remove = document.createElement("button");
        actions.className = "schedule-actions";
        edit.type = "button";
        remove.type = "button";
        text(edit, "Изменить");
        text(remove, "Снять");
        edit.addEventListener("click", () => beginEdit(item));
        remove.addEventListener("click", () => cancelSlot(item));
        actions.append(edit, remove);
        card.appendChild(actions);
      }
      list.appendChild(card);
    }
    text(empty, items.length ? "" : "Ближайших открытых или занятых окон пока нет.");
    const notes = [];
    if (payload.has_more) notes.push("Показаны ближайшие 30 окон.");
    if (managementUnavailable) notes.push("Просмотр работает, но управление расписанием сейчас временно недоступно.");
    text(limitations, notes.join(" "));
  };

  const fail = (error) => {
    list.replaceChildren();
    text(meta, "Не удалось обновить расписание");
    text(empty, error && error.message === "calendar_access_denied"
      ? "Для Вашей роли расписание недоступно."
      : "Расписание временно недоступно. Нажмите «Обновить».");
    text(limitations, "Ваши записи и настройки не изменялись.");
    panel.hidden = true;
    manage.hidden = true;
    show();
  };

  const load = async () => {
    const snapshot = captureContext();
    show(); setBusy(true);
    list.replaceChildren(); panel.hidden = true; manage.hidden = true;
    text(meta, "Обновляем ближайшие записи…"); text(empty, ""); text(limitations, "");
    try {
      const calendar = await post("/clientplatform/cockpit/calendar", {limit: 30}, snapshot.businessId);
      assertCurrent(snapshot, calendar);
      management = null;
      managementUnavailable = false;
      try {
        const managePayload = await post("/clientplatform/cockpit/calendar/manage", {}, snapshot.businessId);
        assertCurrent(snapshot, managePayload);
        management = managePayload;
      } catch (error) {
        if (contextChanged(error)) throw error;
        if (!error || error.message !== "calendar_manage_denied") managementUnavailable = true;
      }
      assertCurrent(snapshot);
      renderManagement(); render(calendar); focusView();
    } catch (error) {
      if (!contextChanged(error)) fail(error);
    } finally {
      if (controller().isBusinessContextCurrent(snapshot)) setBusy(false);
    }
  };

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!management) return;
    const localStart = toWallClock(start.value);
    const minutes = Number(duration.value);
    if (!localStart || !Number.isInteger(minutes)) {
      text(manageMessage, "Проверьте дату, время и длительность.");
      return;
    }
    if (editingSlotId) {
      void mutate(
        "/clientplatform/cockpit/calendar/replace",
        {slot_id: editingSlotId, local_start: localStart, duration_minutes: minutes},
        "Свободное время изменено.",
      );
      return;
    }
    if (!offering.value) {
      text(manageMessage, "Сначала выберите активную услугу.");
      return;
    }
    void mutate(
      "/clientplatform/cockpit/calendar/create",
      {offering_id: offering.value, local_start: localStart, duration_minutes: minutes},
      "Новое свободное время опубликовано.",
    );
  });

  manage.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    if (!panel.hidden && !editingSlotId) resetForm();
  });
  formCancel.addEventListener("click", () => {
    resetForm();
    form.hidden = !(management && (management.offerings || []).length);
  });
  refresh.addEventListener("click", () => { void load(); });
  more.addEventListener("click", () => {
    const api = controller();
    if (api && typeof api.showNavigation === "function") api.showNavigation();
  });
  advanced.addEventListener("click", () => {
    const api = controller();
    if (api && typeof api.openCanonicalSection === "function") api.openCanonicalSection("calendar", advanced);
  });

  const open = () => { void load(); };
  const handleBack = () => {
    if (!panel.hidden) {
      panel.hidden = true;
      resetForm();
      if (calendarPayload) render(calendarPayload);
      return;
    }
    const api = controller();
    if (api && typeof api.showHome === "function") api.showHome();
  };

  window.addEventListener("clientplatform:business-context-changing", () => {
    calendarPayload = null; management = null; managementUnavailable = false; editingSlotId = null;
    list.replaceChildren(); offering.replaceChildren(); panel.hidden = true; manage.hidden = true;
    text(meta, ""); text(empty, ""); text(limitations, ""); text(manageMessage, "");
  });

  window.ClientPlatformCalendar = Object.freeze({open, back: handleBack});
})();
