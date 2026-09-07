(() => {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp;
  const select = document.getElementById("business-select");
  const initData = tg && typeof tg.initData === "string" ? tg.initData : "";
  const controller = () => window.ClientPlatformCockpitNavigation;
  const text = (node, value) => { node.textContent = value == null ? "" : String(value); };

  const servicesView = document.getElementById("services-view");
  const servicesRefresh = document.getElementById("services-refresh");
  const servicesMore = document.getElementById("services-more");
  const servicesMeta = document.getElementById("services-meta");
  const servicesList = document.getElementById("services-list");
  const servicesEmpty = document.getElementById("services-empty");
  const servicesAdd = document.getElementById("services-add");
  const servicesPanel = document.getElementById("services-form-panel");
  const servicesForm = document.getElementById("services-form");
  const servicesCapability = document.getElementById("services-capability");
  const servicesTitle = document.getElementById("services-title");
  const servicesDescription = document.getElementById("services-description");
  const servicesMessage = document.getElementById("services-message");
  const servicesAdvanced = document.getElementById("services-advanced");

  const moneyView = document.getElementById("money-view");
  const moneyRefresh = document.getElementById("money-refresh");
  const moneyMore = document.getElementById("money-more");
  const moneyMeta = document.getElementById("money-meta");
  const moneyTotals = document.getElementById("money-totals");
  const moneySummary = document.getElementById("money-summary");
  const moneyAdd = document.getElementById("money-add");
  const moneyPanel = document.getElementById("money-form-panel");
  const moneyForm = document.getElementById("money-form");
  const moneyAmount = document.getElementById("money-amount");
  const moneyCurrency = document.getElementById("money-currency");
  const moneyCustomer = document.getElementById("money-customer");
  const moneyOffering = document.getElementById("money-offering");
  const moneyNote = document.getElementById("money-note");
  const moneyMessage = document.getElementById("money-message");
  const moneyList = document.getElementById("money-list");
  const moneyEmpty = document.getElementById("money-empty");
  const moneyAdvanced = document.getElementById("money-advanced");

  const growthView = document.getElementById("growth-view");
  const growthRefresh = document.getElementById("growth-refresh");
  const growthMore = document.getElementById("growth-more");
  const growthMeta = document.getElementById("growth-meta");
  const growthMetrics = document.getElementById("growth-metrics");
  const growthSources = document.getElementById("growth-sources");
  const growthAdvertising = document.getElementById("growth-advertising");
  const growthActions = document.getElementById("growth-actions");
  const growthLimitations = document.getElementById("growth-limitations");
  const growthAdvanced = document.getElementById("growth-advanced");

  const analyticsView = document.getElementById("analytics-view");
  const analyticsRefresh = document.getElementById("analytics-refresh");
  const analyticsMore = document.getElementById("analytics-more");
  const analyticsMeta = document.getElementById("analytics-meta");
  const analyticsFunnel = document.getElementById("analytics-funnel");
  const analyticsMoney = document.getElementById("analytics-money");
  const analyticsSources = document.getElementById("analytics-sources");
  const analyticsLimitations = document.getElementById("analytics-limitations");
  const analyticsAdvanced = document.getElementById("analytics-advanced");

  let servicesPayload = null;
  let moneyPayload = null;
  let growthPeriod = 7;
  let analyticsPeriod = 7;

  const post = async (path, extra = {}) => {
    const body = {init_data: initData, ...extra};
    if (select.value) body.business_id = select.value;
    const response = await fetch(path, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      credentials: "same-origin",
      cache: "no-store",
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({error: "invalid_response"}));
    if (!response.ok) throw new Error(payload.error || "workspace_unavailable");
    return payload;
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

  const setBusy = (view, refresh, busy) => {
    view.classList.toggle("busy", Boolean(busy));
    refresh.disabled = Boolean(busy);
    view.setAttribute("aria-busy", busy ? "true" : "false");
  };

  const card = (titleValue, detailValue) => {
    const node = document.createElement("section");
    const heading = document.createElement("strong");
    const detail = document.createElement("p");
    node.className = "workspace-card";
    text(heading, titleValue);
    text(detail, detailValue);
    node.append(heading, detail);
    return node;
  };

  const actionButton = (label, action) => {
    const button = document.createElement("button");
    button.type = "button";
    text(button, label);
    button.addEventListener("click", action);
    return button;
  };

  const openCanonical = (section, button) => {
    const api = controller();
    if (api && typeof api.openCanonicalSection === "function") api.openCanonicalSection(section, button);
  };

  const showView = (name) => {
    const api = controller();
    const method = {
      services: "enterServices",
      money: "enterMoney",
      growth: "enterGrowth",
      analytics: "enterAnalytics",
    }[name];
    if (api && method && typeof api[method] === "function") api[method]();
  };

  const priceEditor = (item) => {
    const wrapper = document.createElement("form");
    const amount = document.createElement("input");
    const currency = document.createElement("input");
    const save = document.createElement("button");
    wrapper.className = "workspace-form";
    amount.inputMode = "decimal";
    amount.maxLength = 40;
    amount.required = true;
    amount.placeholder = "5000";
    amount.value = item.price_input || "";
    currency.maxLength = 3;
    currency.required = true;
    currency.value = item.price_currency || "RUB";
    save.type = "submit";
    save.className = "secondary";
    text(save, "Сохранить цену");
    wrapper.append(amount, currency, save);
    wrapper.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        await post("/clientplatform/cockpit/services/price", {
          offering_id: item.id,
          amount: amount.value,
          currency: currency.value,
        });
        notifySuccess();
        await loadServices();
      } catch (error) {
        text(servicesMessage, error && error.message === "services_write_denied"
          ? "Для Вашей роли изменение цены недоступно."
          : "Не удалось сохранить цену. Проверьте сумму и валюту.");
      }
    });
    return wrapper;
  };

  const renderServices = (payload) => {
    servicesPayload = payload;
    servicesList.replaceChildren();
    servicesCapability.replaceChildren();
    text(servicesMeta, `${payload.business_name} · активные предложения`);
    for (const capability of payload.capabilities || []) {
      const option = document.createElement("option");
      option.value = capability.id;
      text(option, capability.title);
      servicesCapability.appendChild(option);
    }
    servicesAdd.hidden = !payload.can_manage_offerings || !(payload.capabilities || []).length;
    if (servicesAdd.hidden) servicesPanel.hidden = true;
    for (const item of payload.items || []) {
      const detail = item.description || item.capability_title || "Услуга";
      const node = card(item.title, detail);
      const price = document.createElement("small");
      text(price, item.price_display ? `Цена: ${item.price_display}` : (payload.can_view_prices ? "Цена не задана" : "Цена доступна финансовым ролям"));
      node.appendChild(price);
      if (payload.can_manage_offerings || payload.can_manage_prices) {
        const actions = document.createElement("div");
        actions.className = "workspace-actions";
        if (payload.can_manage_prices) {
          const priceButton = actionButton(item.price_display ? "Изменить цену" : "Задать цену", () => {
            const existing = node.querySelector("form");
            if (existing) existing.remove(); else node.appendChild(priceEditor(item));
          });
          actions.appendChild(priceButton);
        }
        if (payload.can_manage_offerings) {
          actions.appendChild(actionButton("Убрать из активных", () => {
            confirmAction(`Убрать «${item.title}» из активных услуг? История и оплаты сохранятся.`, () => {
              void archiveService(item.id);
            });
          }));
        }
        node.appendChild(actions);
      }
      servicesList.appendChild(node);
    }
    text(servicesEmpty, (payload.items || []).length ? "" : "Активных услуг пока нет.");
  };

  const loadServices = async () => {
    showView("services");
    setBusy(servicesView, servicesRefresh, true);
    text(servicesMeta, "Обновляем услуги…");
    try { renderServices(await post("/clientplatform/cockpit/services")); }
    catch (error) {
      servicesList.replaceChildren();
      text(servicesMeta, "Не удалось обновить услуги");
      text(servicesEmpty, error && error.message === "services_access_denied"
        ? "Для Вашей роли услуги недоступны."
        : "Услуги временно недоступны. Данные не изменялись.");
    } finally { setBusy(servicesView, servicesRefresh, false); }
  };

  const archiveService = async (offeringId) => {
    setBusy(servicesView, servicesRefresh, true);
    try {
      await post("/clientplatform/cockpit/services/archive", {offering_id: offeringId});
      notifySuccess();
      await loadServices();
      text(servicesMessage, "Услуга убрана из активных. История и финансовые данные сохранены.");
    } catch (_error) {
      text(servicesMessage, "Не удалось убрать услугу. Обновите экран и попробуйте снова.");
    } finally { setBusy(servicesView, servicesRefresh, false); }
  };

  servicesForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!servicesCapability.value) return;
    setBusy(servicesView, servicesRefresh, true);
    try {
      await post("/clientplatform/cockpit/services/create", {
        capability_id: servicesCapability.value,
        title: servicesTitle.value,
        description: servicesDescription.value,
      });
      servicesTitle.value = "";
      servicesDescription.value = "";
      servicesPanel.hidden = true;
      notifySuccess();
      await loadServices();
      text(servicesMessage, "Новая услуга создана.");
    } catch (error) {
      text(servicesMessage, error && error.message === "services_write_denied"
        ? "Для Вашей роли создание услуг недоступно."
        : "Не удалось создать услугу. Проверьте название и описание.");
    } finally { setBusy(servicesView, servicesRefresh, false); }
  });

  const addOption = (target, value, label) => {
    const option = document.createElement("option");
    option.value = value;
    text(option, label);
    target.appendChild(option);
  };

  const renderMoney = (payload) => {
    moneyPayload = payload;
    moneyTotals.replaceChildren();
    moneyList.replaceChildren();
    moneyCustomer.replaceChildren();
    moneyOffering.replaceChildren();
    addOption(moneyCustomer, "", "Без привязки");
    addOption(moneyOffering, "", "Без привязки");
    text(moneyMeta, `${payload.business_name} · подтверждённые финансовые факты`);
    for (const total of payload.totals || []) {
      const node = document.createElement("div");
      const caption = document.createElement("span");
      const value = document.createElement("strong");
      node.className = "money-card";
      text(caption, `${total.paid_payments} успешных оплат`);
      text(value, total.display);
      node.append(caption, value);
      moneyTotals.appendChild(node);
    }
    text(moneySummary, `Успешных оплат: ${payload.paid_payments || 0}. Платящих клиентов: ${payload.paid_customers || 0}.`);
    moneyAdd.hidden = !payload.can_write;
    if (!payload.can_write) moneyPanel.hidden = true;
    for (const item of payload.customer_choices || []) addOption(moneyCustomer, item.id, item.title);
    for (const item of payload.offering_choices || []) addOption(moneyOffering, item.id, item.title);
    for (const item of payload.recent || []) {
      const node = card(item.display, item.note || item.provider || "Оплата");
      const state = document.createElement("small");
      text(state, item.status === "refunded" ? "Возврат оформлен" : "Оплата подтверждена");
      node.appendChild(state);
      if (item.refundable) {
        const actions = document.createElement("div");
        actions.className = "workspace-actions";
        actions.appendChild(actionButton("Полный возврат", () => {
          confirmAction(`Оформить полный возврат ${item.display}? Будет создан отдельный подтверждённый факт возврата.`, () => { void refundPayment(item.id); });
        }));
        node.appendChild(actions);
      }
      moneyList.appendChild(node);
    }
    text(moneyEmpty, (payload.recent || []).length ? "" : "Оплат пока нет.");
  };

  const loadMoney = async () => {
    showView("money");
    setBusy(moneyView, moneyRefresh, true);
    text(moneyMeta, "Обновляем подтверждённые оплаты…");
    try { renderMoney(await post("/clientplatform/cockpit/money", {limit: 20})); }
    catch (error) {
      moneyTotals.replaceChildren(); moneyList.replaceChildren();
      text(moneyMeta, "Не удалось обновить деньги");
      text(moneyEmpty, error && error.message === "money_access_denied"
        ? "Для Вашей роли финансовые данные недоступны."
        : "Финансовые данные временно недоступны. Ничего не изменялось.");
    } finally { setBusy(moneyView, moneyRefresh, false); }
  };

  moneyForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const requestId = window.crypto && typeof window.crypto.randomUUID === "function"
      ? window.crypto.randomUUID()
      : "";
    if (!requestId) { text(moneyMessage, "Браузер не может безопасно создать идентификатор операции. Откройте кабинет заново."); return; }
    setBusy(moneyView, moneyRefresh, true);
    try {
      await post("/clientplatform/cockpit/money/record", {
        amount: moneyAmount.value,
        currency: moneyCurrency.value,
        request_id: requestId,
        customer_id: moneyCustomer.value || null,
        offering_id: moneyOffering.value || null,
        note: moneyNote.value,
      });
      moneyAmount.value = "";
      moneyNote.value = "";
      moneyPanel.hidden = true;
      notifySuccess();
      await loadMoney();
      text(moneyMessage, "Оплата сохранена как подтверждённый денежный факт.");
    } catch (error) {
      text(moneyMessage, error && error.message === "money_write_denied"
        ? "Для Вашей роли запись оплаты недоступна."
        : "Не удалось сохранить оплату. Проверьте сумму, валюту и выбранные связи.");
    } finally { setBusy(moneyView, moneyRefresh, false); }
  });

  const refundPayment = async (paymentId) => {
    setBusy(moneyView, moneyRefresh, true);
    try {
      await post("/clientplatform/cockpit/money/refund", {payment_id: paymentId});
      notifySuccess();
      await loadMoney();
      text(moneyMessage, "Полный возврат сохранён отдельным подтверждённым фактом.");
    } catch (_error) {
      text(moneyMessage, "Возврат не выполнен. Оплата могла уже измениться; обновите экран.");
    } finally { setBusy(moneyView, moneyRefresh, false); }
  };

  const metricLabels = {
    leads: "Обращения",
    qualified_leads: "С интересом",
    bookings: "Записи",
    paid_customers: "Оплатили",
  };

  const metricNode = (label, value, note) => {
    const node = document.createElement("div");
    const caption = document.createElement("span");
    const number = document.createElement("strong");
    const detail = document.createElement("span");
    node.className = "metric";
    text(caption, label); text(number, value); text(detail, note || "");
    node.append(caption, number, detail);
    return node;
  };

  const setPeriodActive = (selector, value) => {
    for (const button of document.querySelectorAll(selector)) {
      button.classList.toggle("active", Number(button.dataset.growthPeriod || button.dataset.analyticsPeriod) === value);
    }
  };

  const renderGrowth = (payload) => {
    growthMetrics.replaceChildren(); growthSources.replaceChildren(); growthAdvertising.replaceChildren(); growthActions.replaceChildren();
    text(growthMeta, `${payload.business_name} · последние ${payload.period_days} дней`);
    setPeriodActive("[data-growth-period]", payload.period_days);
    for (const item of payload.metrics || []) growthMetrics.appendChild(metricNode(metricLabels[item.key] || item.key, item.value, item.meaning));
    for (const item of payload.sources || []) growthSources.appendChild(card(item.label, `Подтверждённых результатов: ${item.outcomes}`));
    if (payload.advertising) {
      const ad = payload.advertising;
      const node = card("Яндекс Директ", `Показы: ${ad.impressions} · клики: ${ad.clicks} · CTR: ${ad.ctr_percent}%`);
      const detail = document.createElement("small");
      text(detail, `Лиды: ${ad.leads} · записи: ${ad.bookings} · оплаты: ${ad.won}. Денежная стоимость не объединяется с выручкой без подтверждённой валюты провайдера.`);
      node.appendChild(detail);
      growthAdvertising.appendChild(node);
    } else {
      growthAdvertising.appendChild(card("Реклама", "Подтверждённых рекламных данных за этот период сейчас нет."));
    }
    for (const item of payload.actions || []) {
      const node = card(item.title, item.reason);
      const actions = document.createElement("div");
      actions.className = "workspace-actions";
      const target = item.action_key === "economic_open_slots" ? "calendar"
        : item.action_key === "economic_reactivation" ? "customers"
        : item.action_key === "attribution_review" ? "analytics"
        : item.action_key.startsWith("sales_") ? "sales"
        : item.action_key.startsWith("sales_plan:") || item.action_key.startsWith("sales_lead:") ? "sales"
        : "growth";
      const button = actionButton("Открыть нужный раздел", () => openCanonical(target, button));
      actions.appendChild(button);
      node.appendChild(actions);
      growthActions.appendChild(node);
    }
    text(growthLimitations, (payload.limitations || []).length
      ? "Часть показателей имеет ограничения источников. ClientPlatform не подставляет догадки вместо недоступных данных."
      : "");
  };

  const loadGrowth = async () => {
    showView("growth"); setBusy(growthView, growthRefresh, true); text(growthMeta, "Обновляем показатели роста…");
    try { renderGrowth(await post("/clientplatform/cockpit/growth", {period_days: growthPeriod})); }
    catch (error) {
      growthMetrics.replaceChildren(); growthSources.replaceChildren(); growthAdvertising.replaceChildren(); growthActions.replaceChildren();
      text(growthMeta, error && error.message === "growth_access_denied" ? "Для Вашей роли раздел недоступен" : "Показатели роста временно недоступны");
    } finally { setBusy(growthView, growthRefresh, false); }
  };

  const renderAnalytics = (payload) => {
    analyticsFunnel.replaceChildren(); analyticsMoney.replaceChildren(); analyticsSources.replaceChildren();
    text(analyticsMeta, `${payload.business_name} · последние ${payload.period_days} дней`);
    setPeriodActive("[data-analytics-period]", payload.period_days);
    const journey = payload.journey || {};
    analyticsFunnel.appendChild(metricNode("Обращения", journey.leads || 0, "Вошли в подтверждённый путь клиента"));
    analyticsFunnel.appendChild(metricNode("Записи", journey.bookings || 0, "Записались"));
    analyticsFunnel.appendChild(metricNode("Пришли", journey.completed_bookings === -1 ? "—" : (journey.completed_bookings || 0), journey.completed_bookings === -1 ? "Источник завершения записи сейчас недоступен" : "Завершённые записи"));
    analyticsFunnel.appendChild(metricNode("Оплатили", journey.paid_customers || 0, "Клиенты с подтверждённой оплатой"));
    analyticsFunnel.appendChild(metricNode("Вернулись", journey.reactivated_customers || 0, "Повторная подтверждённая выручка"));
    for (const item of journey.verified_revenue || []) {
      const node = document.createElement("div"); const caption = document.createElement("span"); const value = document.createElement("strong");
      node.className = "money-card"; text(caption, "Подтверждённая выручка"); text(value, item.display); node.append(caption, value); analyticsMoney.appendChild(node);
    }
    for (const item of payload.sources || []) analyticsSources.appendChild(card(item.label, `Подтверждённых результатов: ${item.outcomes}`));
    text(analyticsLimitations, (payload.limitations || []).length
      ? "Есть неполные источники или неатрибутированные результаты. Такие данные показаны отдельно и не приписываются каналу автоматически."
      : "");
  };

  const loadAnalytics = async () => {
    showView("analytics"); setBusy(analyticsView, analyticsRefresh, true); text(analyticsMeta, "Обновляем подтверждённый путь клиента…");
    try { renderAnalytics(await post("/clientplatform/cockpit/analytics", {period_days: analyticsPeriod})); }
    catch (error) {
      analyticsFunnel.replaceChildren(); analyticsMoney.replaceChildren(); analyticsSources.replaceChildren();
      text(analyticsMeta, error && error.message === "analytics_access_denied" ? "Для Вашей роли раздел недоступен" : "Аналитика временно недоступна");
    } finally { setBusy(analyticsView, analyticsRefresh, false); }
  };

  servicesRefresh.addEventListener("click", () => { void loadServices(); });
  servicesMore.addEventListener("click", () => { const api = controller(); if (api) api.showNavigation(); });
  servicesAdd.addEventListener("click", () => { servicesPanel.hidden = !servicesPanel.hidden; });
  servicesAdvanced.addEventListener("click", () => openCanonical("services", servicesAdvanced));
  moneyRefresh.addEventListener("click", () => { void loadMoney(); });
  moneyMore.addEventListener("click", () => { const api = controller(); if (api) api.showNavigation(); });
  moneyAdd.addEventListener("click", () => { moneyPanel.hidden = !moneyPanel.hidden; });
  moneyAdvanced.addEventListener("click", () => openCanonical("money", moneyAdvanced));
  growthRefresh.addEventListener("click", () => { void loadGrowth(); });
  growthMore.addEventListener("click", () => { const api = controller(); if (api) api.showNavigation(); });
  growthAdvanced.addEventListener("click", () => openCanonical("growth", growthAdvanced));
  analyticsRefresh.addEventListener("click", () => { void loadAnalytics(); });
  analyticsMore.addEventListener("click", () => { const api = controller(); if (api) api.showNavigation(); });
  analyticsAdvanced.addEventListener("click", () => openCanonical("analytics", analyticsAdvanced));

  for (const button of document.querySelectorAll("[data-growth-period]")) {
    button.addEventListener("click", () => { growthPeriod = Number(button.dataset.growthPeriod); void loadGrowth(); });
  }
  for (const button of document.querySelectorAll("[data-analytics-period]")) {
    button.addEventListener("click", () => { analyticsPeriod = Number(button.dataset.analyticsPeriod); void loadAnalytics(); });
  }

  window.ClientPlatformBusinessWorkspace = Object.freeze({
    openServices: () => { void loadServices(); },
    openMoney: () => { void loadMoney(); },
    openGrowth: () => { void loadGrowth(); },
    openAnalytics: () => { void loadAnalytics(); },
    back: () => { const api = controller(); if (api && typeof api.showHome === "function") api.showHome(); },
  });
})();
