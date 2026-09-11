(() => {
  'use strict';

  const tg = window.Telegram && window.Telegram.WebApp;
  const initData = tg && typeof tg.initData === 'string' ? tg.initData : '';
  const view = document.getElementById('customers-view');
  const nav = document.getElementById('navigation');
  const home = document.getElementById('home-view');
  const explanation = document.getElementById('explanation');
  const calendar = document.getElementById('calendar-view');
  const sales = document.getElementById('sales-view');
  const select = document.getElementById('business-select');
  const back = document.getElementById('customers-back');
  const refresh = document.getElementById('customers-refresh');
  const listPanel = document.getElementById('customer-list-panel');
  const searchForm = document.getElementById('customer-search-form');
  const search = document.getElementById('customer-search');
  const listMeta = document.getElementById('customer-list-meta');
  const list = document.getElementById('customer-list');
  const prev = document.getElementById('customer-prev');
  const next = document.getElementById('customer-next');
  const detail = document.getElementById('customer-detail');
  const detailBack = document.getElementById('customer-detail-back');
  const detailName = document.getElementById('customer-detail-name');
  const detailMeta = document.getElementById('customer-detail-meta');
  const contacts = document.getElementById('customer-contacts');
  const action = document.getElementById('customer-action');
  const timeline = document.getElementById('customer-timeline');
  const limitations = document.getElementById('customer-limitations');
  let page = null;
  let detailReturn = 'customers';

  const text = (node, value) => {
    node.textContent = value == null ? '' : String(value);
  };
  const controller = () => window.ClientPlatformCockpitNavigation;
  const captureContext = () => controller().captureBusinessContext();
  const assertCurrent = (snapshot, payload = null) => {
    controller().assertBusinessContextCurrent(snapshot);
    const payloadBusiness = String(payload && payload.business_id || '').trim();
    if (payloadBusiness && payloadBusiness !== snapshot.businessId) throw new Error('workspace_context_changed');
  };
  const contextChanged = (error) => controller().isContextChangedError(error);
  const focusView = () => controller().focusRegion(view);

  const setBusy = (busy) => {
    view.classList.toggle('busy', Boolean(busy));
    refresh.disabled = Boolean(busy);
    search.disabled = Boolean(busy);
    view.setAttribute('aria-busy', busy ? 'true' : 'false');
  };

  const post = async (path, extra, businessId) => {
    const body = {init_data: initData, ...(extra || {})};
    if (businessId) body.business_id = businessId;
    const response = await fetch(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      credentials: 'same-origin',
      cache: 'no-store',
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({error: 'invalid_response'}));
    if (!response.ok) throw new Error(payload.error || 'customer_unavailable');
    return payload;
  };

  const dateText = (value) => {
    const raw = String(value || '');
    const day = raw.slice(0, 10).split('-');
    return day.length === 3 ? `${day[2]}.${day[1]}.${day[0]}` : raw;
  };

  const showNavigation = () => {
    const api = controller();
    if (api && typeof api.showNavigation === 'function') { api.showNavigation(); return; }
    view.hidden = true; explanation.hidden = true; home.hidden = true; calendar.hidden = true; sales.hidden = true; nav.hidden = false;
  };

  const enterCustomers = () => {
    const api = controller();
    if (api && typeof api.enterCustomers === 'function') api.enterCustomers();
  };

  const showList = () => {
    enterCustomers();
    view.hidden = false;
    nav.hidden = true;
    home.hidden = true;
    explanation.hidden = true;
    calendar.hidden = true;
    sales.hidden = true;
    listPanel.hidden = false;
    detail.hidden = true;
  };

  const showFailure = (message) => {
    list.replaceChildren();
    text(listMeta, message || 'Не удалось загрузить клиентов. Нажмите «Обновить».');
    prev.disabled = true;
    next.disabled = true;
    showList();
  };

  const closeAfterDelivery = () => {
    if (tg && typeof tg.close === 'function') { tg.close(); return true; }
    return false;
  };

  const openAction = async (customerId, expectedActionKey, button) => {
    const snapshot = captureContext();
    setBusy(true);
    if (button) button.disabled = true;
    try {
      await post('/clientplatform/cockpit/customers/action-open', {
        customer_id: customerId,
        expected_action_key: expectedActionKey,
      }, snapshot.businessId);
      assertCurrent(snapshot);
      if (tg && tg.HapticFeedback && typeof tg.HapticFeedback.notificationOccurred === 'function') {
        tg.HapticFeedback.notificationOccurred('success');
      }
      if (!closeAfterDelivery()) {
        text(limitations, 'Следующий шаг открыт в чате с ботом. Вернитесь в Telegram.');
      }
    } catch (error) {
      if (contextChanged(error)) return;
      text(
        limitations,
        error && error.message === 'customer_action_changed'
          ? 'Следующий шаг уже изменился. Обновите карточку клиента.'
          : 'Не удалось открыть следующий шаг. Обновите карточку и попробуйте ещё раз.',
      );
    } finally {
      if (button) button.disabled = false;
      if (controller().isBusinessContextCurrent(snapshot)) setBusy(false);
    }
  };

  const renderDetail = (payload) => {
    contacts.replaceChildren();
    timeline.replaceChildren();
    action.replaceChildren();
    text(detailName, payload.display_name || 'Клиент');
    text(detailMeta, `Статус: ${payload.status === 'active' ? 'активный' : payload.status} · обновлено ${dateText(payload.updated_at)}`);

    for (const item of payload.contacts || []) {
      const card = document.createElement('div');
      const label = document.createElement('strong');
      const display = document.createElement('small');
      card.className = 'contact-card';
      text(label, item.label);
      text(display, item.display);
      card.append(label, display);
      contacts.appendChild(card);
    }
    if (!(payload.contacts || []).length) {
      const empty = document.createElement('p');
      empty.className = 'muted';
      text(empty, 'Подтверждённых контактных данных пока нет.');
      contacts.appendChild(empty);
    }

    if (payload.next_action) {
      const button = document.createElement('button');
      const label = document.createElement('span');
      const reason = document.createElement('small');
      button.type = 'button';
      button.className = 'action-card';
      text(label, payload.next_action.title);
      text(reason, payload.next_action.reason);
      button.append(label, reason);
      button.addEventListener('click', () => {
        void openAction(payload.customer_id, payload.next_action.action_key, button);
      });
      action.appendChild(button);
    } else {
      const empty = document.createElement('p');
      empty.className = 'muted';
      text(empty, 'Сохранённого следующего шага сейчас нет.');
      action.appendChild(empty);
    }

    for (const item of payload.timeline || []) {
      const card = document.createElement('div');
      const label = document.createElement('strong');
      const meta = document.createElement('small');
      card.className = 'timeline-card';
      text(label, item.title);
      const parts = [dateText(item.occurred_at), item.detail, item.money].filter(Boolean);
      text(meta, parts.join(' · '));
      card.append(label, meta);
      timeline.appendChild(card);
    }
    if (!(payload.timeline || []).length) {
      const empty = document.createElement('p');
      empty.className = 'muted';
      text(empty, 'История пока пуста или временно недоступна.');
      timeline.appendChild(empty);
    }

    text(
      limitations,
      (payload.limitations || []).length
        ? 'Часть дополнительных данных временно недоступна. Показаны только подтверждённые сведения.'
        : '',
    );
    enterCustomers();
    listPanel.hidden = true;
    detail.hidden = false;
    view.hidden = false;
    nav.hidden = true;
    home.hidden = true;
    explanation.hidden = true;
    calendar.hidden = true;
    sales.hidden = true;
  };

  const loadDetail = async (customerId) => {
    const snapshot = captureContext();
    setBusy(true);
    try {
      const payload = await post('/clientplatform/cockpit/customers/detail', {
        customer_id: customerId,
        timeline_limit: 20,
      }, snapshot.businessId);
      assertCurrent(snapshot, payload);
      renderDetail(payload); focusView();
    } catch (error) {
      if (contextChanged(error)) return;
      text(listMeta, error && error.message === 'customer_not_found'
        ? 'Клиент больше недоступен в этом бизнесе.'
        : 'Не удалось открыть карточку клиента.');
      showList();
    } finally {
      if (controller().isBusinessContextCurrent(snapshot)) setBusy(false);
    }
  };

  const renderPage = (payload) => {
    page = payload;
    list.replaceChildren();
    const items = payload.items || [];
    text(
      listMeta,
      items.length
        ? `Показано клиентов: ${items.length}${payload.query ? ` · поиск «${payload.query}»` : ''}`
        : payload.query ? 'По этому запросу клиентов не найдено.' : 'Клиентов пока нет.',
    );
    for (const item of items) {
      const button = document.createElement('button');
      const label = document.createElement('strong');
      const meta = document.createElement('small');
      button.type = 'button';
      button.className = 'customer-row';
      text(label, item.display_name || 'Клиент');
      text(meta, `Обновлено: ${dateText(item.updated_at)}`);
      button.append(label, meta);
      button.addEventListener('click', () => { detailReturn = 'customers'; loadDetail(item.customer_id); });
      list.appendChild(button);
    }
    prev.disabled = payload.previous_offset == null;
    next.disabled = payload.next_offset == null;
    showList();
  };

  const loadPage = async (offset) => {
    const snapshot = captureContext();
    setBusy(true);
    list.replaceChildren(); text(listMeta, 'Обновляем клиентов…'); prev.disabled = true; next.disabled = true;
    try {
      const payload = await post('/clientplatform/cockpit/customers', {
        query: search.value.trim(),
        limit: 20,
        offset: Number.isInteger(offset) ? offset : 0,
      }, snapshot.businessId);
      assertCurrent(snapshot, payload);
      renderPage(payload); focusView();
    } catch (error) {
      if (contextChanged(error)) return;
      if (error && error.message === 'customer_access_denied') {
        showFailure('Для Вашей роли список клиентов недоступен.');
      } else {
        showFailure('Не удалось обновить список клиентов. Нажмите «Обновить».');
      }
    } finally {
      if (controller().isBusinessContextCurrent(snapshot)) setBusy(false);
    }
  };

  const open = () => {
    detailReturn = 'customers';
    showList();
    loadPage(0);
  };

  const openCustomer = (customerId, returnView = 'customers') => {
    detailReturn = returnView === 'sales' ? 'sales' : 'customers';
    showList();
    void loadDetail(customerId);
  };

  const returnFromDetail = () => {
    if (detailReturn === 'sales') {
      const api = controller();
      if (api && typeof api.enterSales === 'function') { api.enterSales(); return; }
    }
    showList();
  };

  const handleBack = () => {
    if (!detail.hidden) { returnFromDetail(); return; }
    showNavigation();
  };

  back.addEventListener('click', showNavigation);
  detailBack.addEventListener('click', returnFromDetail);
  refresh.addEventListener('click', () => loadPage(page ? page.offset : 0));
  searchForm.addEventListener('submit', (event) => {
    event.preventDefault();
    loadPage(0);
  });
  prev.addEventListener('click', () => {
    if (page && page.previous_offset != null) loadPage(page.previous_offset);
  });
  next.addEventListener('click', () => {
    if (page && page.next_offset != null) loadPage(page.next_offset);
  });

  window.addEventListener('clientplatform:business-context-changing', () => {
    page = null; detailReturn = 'customers'; list.replaceChildren(); contacts.replaceChildren(); action.replaceChildren(); timeline.replaceChildren();
    listPanel.hidden = false; detail.hidden = true; text(listMeta, ''); text(limitations, '');
  });

  window.ClientPlatformCustomers = Object.freeze({open, openCustomer, back:handleBack});
})();
