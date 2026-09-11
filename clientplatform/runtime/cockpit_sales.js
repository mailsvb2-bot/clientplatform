(() => {
  "use strict";

  const view = document.getElementById("sales-view");
  const listPanel = document.getElementById("sales-list-panel");
  const detail = document.getElementById("sales-detail");
  const select = document.getElementById("business-select");
  const refresh = document.getElementById("sales-refresh");
  const more = document.getElementById("sales-more");
  const advanced = document.getElementById("sales-manage");
  const meta = document.getElementById("sales-meta");
  const handoff = document.getElementById("sales-handoff");
  const list = document.getElementById("sales-list");
  const empty = document.getElementById("sales-empty");
  const limitations = document.getElementById("sales-limitations");
  const detailBack = document.getElementById("sales-detail-back");
  const detailName = document.getElementById("sales-detail-name");
  const detailMeta = document.getElementById("sales-detail-meta");
  const detailMessage = document.getElementById("sales-detail-message");
  const openCustomerButton = document.getElementById("sales-open-customer");
  const assignment = document.getElementById("sales-assignment");
  const stageBlock = document.getElementById("sales-stage-block");
  const stageActions = document.getElementById("sales-stage-actions");
  const nextBlock = document.getElementById("sales-next-block");
  const nextForm = document.getElementById("sales-next-form");
  const nextAction = document.getElementById("sales-next-action");
  const nextDue = document.getElementById("sales-next-due");
  const noteBlock = document.getElementById("sales-note-block");
  const noteForm = document.getElementById("sales-note-form");
  const note = document.getElementById("sales-note");
  const noteMessage = document.getElementById("sales-note-message");
  const resultBlock = document.getElementById("sales-result-block");
  const resultReason = document.getElementById("sales-result-reason");
  const won = document.getElementById("sales-won");
  const lost = document.getElementById("sales-lost");
  const reopen = document.getElementById("sales-reopen");
  const tg = window.Telegram && window.Telegram.WebApp;
  const initData = tg && typeof tg.initData === "string" ? tg.initData : "";
  const sourceNames = {telegram: "Telegram", vk: "ВКонтакте", max: "MAX", web: "Сайт"};
  const stageChoices = [
    ["contacted", "Связались"],
    ["qualified", "Есть интерес"],
    ["checkout", "Оформление"],
  ];
  let activeLead = null;
  let activeCustomerId = null;
  let pendingNoteKey = null;
  let pendingNoteText = null;

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
  const businessBody = (businessId) => {
    const body = {init_data: initData};
    if (businessId) body.business_id = businessId;
    return body;
  };

  const setBusy = (busy) => {
    view.classList.toggle("busy", Boolean(busy));
    refresh.disabled = Boolean(busy);
    advanced.disabled = Boolean(busy);
    view.setAttribute("aria-busy", busy ? "true" : "false");
  };

  const post = async (path, fields = {}, businessId = String(select.value || "").trim()) => {
    const response = await fetch(path, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      credentials: "same-origin",
      cache: "no-store",
      body: JSON.stringify(Object.assign(businessBody(businessId), fields)),
    });
    const payload = await response.json().catch(() => ({error: "invalid_response"}));
    if (!response.ok) {
      const error = new Error(payload.error || "sales_unavailable");
      error.code = payload.error || "sales_unavailable";
      throw error;
    }
    return payload;
  };

  const friendlyError = (error) => {
    const code = error && (error.code || error.message);
    if (code === "sales_manage_denied" || code === "sales_access_denied") return "Для Вашей роли изменение продаж недоступно.";
    if (code === "sales_lead_not_found") return "Эта сделка уже недоступна. Обновите очередь.";
    if (code === "sales_change_rejected") return "Состояние сделки уже изменилось или действие сейчас запрещено. Обновите данные.";
    if (code === "invalid_sales_change") return "Проверьте введённые данные. Для закрытия сделки нужен комментарий, а срок должен быть корректным временем бизнеса.";
    return "Не удалось сохранить изменение. Данные сделки не потеряны — обновите и попробуйте ещё раз.";
  };

  const show = () => {
    const api = controller();
    if (api && typeof api.enterSales === "function") api.enterSales();
    else view.hidden = false;
  };

  const confirmAction = (message, action) => {
    if (tg && typeof tg.showConfirm === "function") { tg.showConfirm(message, (confirmed) => { if (confirmed) action(); }); return; }
    if (window.confirm(message)) action();
  };

  const showList = () => {
    activeLead = null;
    activeCustomerId = null;
    detail.hidden = true;
    listPanel.hidden = false;
    refresh.hidden = false;
    advanced.hidden = false;
    text(detailMessage, "");
    text(noteMessage, "");
  };

  const openCustomer = (customerId) => {
    const customers = window.ClientPlatformCustomers;
    if (customers && typeof customers.openCustomer === "function") customers.openCustomer(customerId, "sales");
  };

  const renderList = (payload) => {
    list.replaceChildren();
    text(meta, `${payload.business_name} · активные продажи`);
    const handoffCount = Number(payload.handoff_count || 0);
    text(handoff, handoffCount ? `Нужно личное участие сотрудника: ${handoffCount}` : "Срочных передач сотруднику сейчас нет.");
    handoff.classList.toggle("attention-card", handoffCount > 0);
    const items = payload.items || [];
    const recentLost = payload.recent_lost || [];
    const appendCard = (item, reopenable) => {
      const button = document.createElement("button");
      const top = document.createElement("div");
      const name = document.createElement("strong");
      const stage = document.createElement("span");
      const action = document.createElement("p");
      const details = document.createElement("small");
      button.type = "button";
      button.className = `sales-card${item.overdue ? " overdue" : ""}`;
      top.className = "sales-card-top";
      stage.className = "sales-stage";
      text(name, item.customer_name || "Клиент");
      text(stage, item.stage_label || item.stage || "В работе");
      text(
        action,
        reopenable
          ? "Можно открыть сделку и вернуть её в работу"
          : (item.next_action ? `Дальше: ${item.next_action}` : "Следующий шаг ещё не задан"),
      );
      const parts = [];
      if (item.due_display) parts.push(`${item.overdue ? "Просрочено" : "Срок"}: ${item.due_display}`);
      if (item.source_kind) parts.push(sourceNames[item.source_kind] || item.source_kind);
      text(details, parts.join(" · "));
      top.append(name, stage);
      button.append(top, action, details);
      button.addEventListener("click", () => { void openLead(item.lead_id); });
      list.appendChild(button);
    };
    for (const item of items) appendCard(item, false);
    if (recentLost.length) {
      const heading = document.createElement("h3");
      const hint = document.createElement("p");
      heading.className = "sales-recent-heading";
      hint.className = "muted sales-recent-hint";
      text(heading, "Недавно не состоялись");
      text(hint, "Эти сделки можно открыть и вернуть в работу, если клиент снова появился.");
      list.append(heading, hint);
      for (const item of recentLost) appendCard(item, true);
    }
    text(
      empty,
      items.length
        ? ""
        : (recentLost.length
          ? "Активных продаж сейчас нет. Ниже доступны недавние не состоявшиеся сделки."
          : "Активных продаж сейчас нет. Новые лиды появятся здесь автоматически."),
    );
    text(limitations, payload.has_more ? "Показаны 20 ближайших по сроку продаж. Полная очередь и дополнительные действия остаются доступны в боте." : "");
  };

  const renderStages = (payload) => {
    stageActions.replaceChildren();
    if (payload.closed) return;
    for (const [value, label] of stageChoices) {
      const button = document.createElement("button");
      button.type = "button";
      const active = payload.stage === value;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
      text(button, label);
      button.addEventListener("click", () => { void changeStage(value, null); });
      stageActions.appendChild(button);
    }
  };

  const renderDetail = (payload) => {
    activeLead = payload.lead_id;
    activeCustomerId = payload.customer_id;
    listPanel.hidden = true;
    detail.hidden = false;
    refresh.hidden = true;
    advanced.hidden = false;
    text(detailName, payload.customer_name || "Клиент");
    const metaParts = [payload.stage_label || payload.stage || "В работе"];
    if (payload.source_kind) metaParts.push(sourceNames[payload.source_kind] || payload.source_kind);
    if (payload.closed && payload.closure_reason) metaParts.push(`Причина: ${payload.closure_reason}`);
    text(detailMeta, metaParts.join(" · "));
    renderStages(payload);
    stageBlock.hidden = Boolean(payload.closed);
    nextBlock.hidden = Boolean(payload.closed);
    resultBlock.hidden = Boolean(payload.closed);
    assignment.hidden = Boolean(payload.closed);
    reopen.hidden = !payload.can_reopen;
    if (!payload.closed) {
      assignment.dataset.action = payload.assigned_to_me ? "unassign" : "assign";
      text(assignment, payload.assigned_to_me ? "Снять себя с обращения" : (payload.assigned ? "Взять обращение себе" : "Взять обращение"));
      nextAction.value = payload.next_action || "";
      nextDue.value = payload.due_local_value || "";
    }
    resultReason.value = "";
    noteBlock.open = false; resultBlock.open = false;
    text(detailMessage, payload.closed ? "Сделка закрыта. Заметки по ней по-прежнему можно добавлять." : "Изменения сохраняются в общей истории продаж и сразу видны сотрудникам.");
  };

  const refreshQueue = async (snapshot) => {
    const payload = await post("/clientplatform/cockpit/sales", {limit: 20}, snapshot.businessId);
    assertCurrent(snapshot, payload);
    renderList(payload);
    return payload;
  };

  const fail = (error) => {
    list.replaceChildren();
    text(meta, "Не удалось обновить продажи");
    text(handoff, "");
    text(empty, friendlyError(error));
    text(limitations, "Клиенты и история продаж не изменялись.");
    showList();
    show();
  };

  const load = async () => {
    const snapshot = captureContext();
    show(); showList(); setBusy(true);
    list.replaceChildren(); text(meta, "Обновляем очередь продаж…"); text(handoff, ""); text(empty, ""); text(limitations, "");
    try { await refreshQueue(snapshot); focusView(); }
    catch (error) { if (!contextChanged(error)) fail(error); }
    finally { if (controller().isBusinessContextCurrent(snapshot)) setBusy(false); }
  };

  const openLead = async (leadId) => {
    if (!leadId) return;
    const snapshot = captureContext();
    setBusy(true);
    text(detailMessage, "Открываем сделку…");
    try {
      const payload = await post("/clientplatform/cockpit/sales/manage", {lead_id: leadId}, snapshot.businessId);
      assertCurrent(snapshot, payload); renderDetail(payload); focusView();
    } catch (error) {
      if (contextChanged(error)) return;
      showList(); text(limitations, friendlyError(error));
    } finally {
      if (controller().isBusinessContextCurrent(snapshot)) setBusy(false);
    }
  };

  const mutate = async (path, fields, successText) => {
    if (!activeLead) return null;
    const snapshot = captureContext();
    setBusy(true);
    try {
      const payload = await post(path, Object.assign({lead_id: activeLead}, fields), snapshot.businessId);
      assertCurrent(snapshot, payload); renderDetail(payload); text(detailMessage, successText);
      try { await refreshQueue(snapshot); } catch (error) { if (!contextChanged(error)) { /* detail is already canonical */ } }
      assertCurrent(snapshot);
      return payload;
    } catch (error) {
      if (contextChanged(error)) return null;
      text(detailMessage, friendlyError(error)); return null;
    } finally {
      if (controller().isBusinessContextCurrent(snapshot)) setBusy(false);
    }
  };

  const changeStage = async (stage, reason) => mutate(
    "/clientplatform/cockpit/sales/stage",
    {stage, reason},
    "Этап продажи обновлён.",
  );

  const noteKey = (value) => {
    if (pendingNoteKey && pendingNoteText === value) return pendingNoteKey;
    pendingNoteText = value;
    pendingNoteKey = (window.crypto && typeof window.crypto.randomUUID === "function")
      ? window.crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    return pendingNoteKey;
  };

  refresh.addEventListener("click", () => { void load(); });
  more.addEventListener("click", () => {
    const api = controller();
    if (api && typeof api.showNavigation === "function") api.showNavigation();
  });
  detailBack.addEventListener("click", () => { showList(); });
  openCustomerButton.addEventListener("click", () => { if (activeCustomerId) openCustomer(activeCustomerId); });
  assignment.addEventListener("click", () => {
    const action = assignment.dataset.action === "unassign" ? "unassign" : "assign";
    void mutate("/clientplatform/cockpit/sales/assignment", {action}, action === "assign" ? "Обращение назначено Вам." : "Ответственный снят.");
  });
  nextForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const action = nextAction.value.trim();
    const due = nextDue.value.trim();
    void mutate(
      "/clientplatform/cockpit/sales/next-action",
      {next_action: action || null, due_local: due || null},
      action ? "Следующий шаг сохранён." : "Следующий шаг очищен.",
    );
  });
  noteForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const value = note.value.trim();
    if (!value || !activeLead) return;
    const snapshot = captureContext();
    setBusy(true);
    const key = noteKey(value);
    try {
      const payload = await post("/clientplatform/cockpit/sales/note", {lead_id: activeLead, note: value, interaction_key: key}, snapshot.businessId);
      assertCurrent(snapshot, payload); renderDetail(payload);
      note.value = "";
      pendingNoteKey = null;
      pendingNoteText = null;
      text(noteMessage, "Заметка добавлена в общую историю сделки.");
    } catch (error) {
      if (!contextChanged(error)) text(noteMessage, friendlyError(error));
    } finally {
      if (controller().isBusinessContextCurrent(snapshot)) setBusy(false);
    }
  });
  won.addEventListener("click", () => {
    const reason = resultReason.value.trim();
    if (!reason) { text(detailMessage, "Для результата укажите короткий комментарий — например, что оплатил клиент."); return; }
    confirmAction("Подтвердить результат «Клиент оплатил»? Этап сделки будет закрыт как успешный.", () => { void changeStage("won", reason); });
  });
  lost.addEventListener("click", () => {
    const reason = resultReason.value.trim();
    if (!reason) { text(detailMessage, "Укажите, почему продажа не состоялась. Это сохранится в истории."); return; }
    confirmAction("Подтвердить «Не состоялось»? Сделка будет закрыта, но её можно будет вернуть в работу.", () => { void changeStage("lost", reason); });
  });
  reopen.addEventListener("click", () => { void mutate("/clientplatform/cockpit/sales/reopen", {}, "Сделка возвращена в работу."); });
  advanced.addEventListener("click", () => {
    const api = controller();
    if (api && typeof api.openCanonicalSection === "function") api.openCanonicalSection("sales", advanced);
  });

  const open = () => { void load(); };
  const handleBack = () => {
    if (!detail.hidden) { showList(); return; }
    const api = controller();
    if (api && typeof api.showHome === "function") api.showHome();
  };

  window.addEventListener("clientplatform:business-context-changing", () => {
    activeLead = null; activeCustomerId = null; pendingNoteKey = null; pendingNoteText = null;
    list.replaceChildren(); stageActions.replaceChildren(); showList(); text(meta, ""); text(handoff, ""); text(empty, ""); text(limitations, "");
  });

  window.ClientPlatformSales = Object.freeze({open, back: handleBack});
})();
