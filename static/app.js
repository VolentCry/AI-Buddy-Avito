(() => {
  const root = document.getElementById('avito-onboarding');
  const messages = document.getElementById('messages');
  const composer = root.querySelector('.composer');
  const input = composer.querySelector('input');
  const draft = root.querySelector('.request-form');
  const requestCard = root.querySelector('.request');
  const prepare = document.getElementById('prepare');
  const suggestion = document.getElementById('suggestion');
  const refresh = document.getElementById('refresh');
  const profile = document.getElementById('profile');
  const errorPanel = document.getElementById('error-panel');
  const token = document.querySelector('meta[name="csrf-token"]').content;
  let currentCard = null;
  let resourceKnown = false;
  let busy = false;
  let needsStatusCheck = false;

  async function api(path, body) {
    const response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': token},
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(25000),
    });
    const data = await response.json();
    if (!response.ok) {
      const error = new Error(data.error?.message || 'Не удалось выполнить запрос.');
      error.code = data.error?.code;
      throw error;
    }
    return data;
  }

  function setBusy(value) {
    busy = value;
    root.querySelectorAll('button, input, select').forEach(control => { control.disabled = value; });
    composer.setAttribute('aria-busy', String(value));
  }

  function showError(message) {
    document.getElementById('error-text').textContent = message;
    errorPanel.hidden = false;
  }

  function appendMessage(role, content, details = {}) {
    const bubble = document.createElement('div');
    bubble.className = role === 'user' ? 'outgoing' : 'incoming';
    const text = document.createElement('p');
    text.textContent = content;
    text.className = 'message-text';
    bubble.append(text);
    for (const source of details.sources || []) {
      const disclosure = document.createElement('details');
      disclosure.className = 'source';
      const summary = document.createElement('summary');
      summary.textContent = source.title;
      const excerpt = document.createElement('p');
      excerpt.className = 'source-excerpt';
      excerpt.textContent = source.excerpt;
      disclosure.append(summary, excerpt);
      bubble.append(disclosure);
    }
    if (details.generation?.notice) {
      const notice = document.createElement('p');
      notice.className = 'generation-notice';
      notice.textContent = details.generation.notice;
      bubble.append(notice);
    }
    if (details.action?.type === 'draft_message') {
      const handoff = document.createElement('details');
      handoff.className = 'source handoff';
      handoff.open = true;
      const summary = document.createElement('summary');
      summary.textContent = 'Черновик для владельца стенда';
      const draftText = document.createElement('textarea');
      draftText.value = details.action.payload.text;
      draftText.readOnly = true;
      draftText.rows = 5;
      draftText.setAttribute('aria-label', 'Текст обращения');
      const copy = document.createElement('button');
      copy.type = 'button';
      copy.className = 'secondary';
      copy.textContent = 'Скопировать обращение';
      const feedback = document.createElement('p');
      feedback.setAttribute('role', 'status');
      copy.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(draftText.value);
          feedback.textContent = 'Обращение скопировано';
        } catch {
          draftText.focus();
          draftText.select();
          feedback.textContent = 'Текст выделен. Скопируй его вручную.';
        }
      });
      handoff.append(summary, draftText, copy, feedback);
      bubble.append(handoff);
    }
    messages.append(bubble);
    return bubble;
  }

  function renderCard(card, visible = true) {
    currentCard = card;
    requestCard.hidden = !visible || card.status === 'restricted';
    draft.hidden = true;
    document.getElementById('request-info').textContent = card.request ? `Чтение · Заявка ${card.request.number}` : 'Чтение';
    document.getElementById('request-status').textContent = card.title;
    document.getElementById('next-step').textContent = card.next_step;
    root.querySelector('.receipt').dataset.status = card.status === 'granted' ? 'approved' : card.status;
    prepare.hidden = card.status !== 'missing' || !resourceKnown || needsStatusCheck;
    refresh.textContent = needsStatusCheck ? 'Проверить заявку' : 'Проверить статус';
    root.querySelectorAll('.steps li').forEach((step, index) => {
      step.classList.toggle('done', card.step !== null && index < card.step);
      if (index === card.step) step.setAttribute('aria-current', 'step');
      else step.removeAttribute('aria-current');
    });
  }

  async function loadContext(initial = false) {
    if (busy) return;
    setBusy(true);
    try {
      const data = await api('/api/context');
      resourceKnown = data.resource_known;
      needsStatusCheck = data.card.status === 'unknown';
      root.querySelector('.team').textContent = `Команда «${data.profile.team_name}»`;
      document.getElementById('model-status').textContent = data.model.configured ? `Модель · ${data.model.name}` : 'Ответы по инструкциям · модель не подключена';
      profile.replaceChildren(...data.profiles.map(item => new Option(item.name, item.id)));
      profile.value = data.profile.id;
      if (initial) {
        messages.replaceChildren();
        for (const message of data.history) appendMessage(message.role, message.content, message.details);
        if (!data.history.length) {
          appendMessage('assistant', 'Привет! Помогу с доступом к стенду для первой задачи. Расскажи, что не получается.');
        }
        suggestion.hidden = data.history.length > 0 || data.profile.team !== 'catalog';
      }
      renderCard(data.card, data.history.length > 0 || data.card.request !== null || data.card.status === 'granted' || needsStatusCheck);
      errorPanel.hidden = true;
    } catch (error) {
      showError(error.message || 'Не удалось загрузить данные. Повтори проверку.');
      needsStatusCheck = true;
      prepare.hidden = true;
      draft.hidden = true;
      if (currentCard) renderUnknown('Не удалось проверить статус');
    } finally {
      setBusy(false);
    }
  }

  function renderUnknown(title) {
    renderCard({status: 'unknown', request: null, step: null, title,
      next_step: 'Проверь заявку, чтобы узнать, сохранилась ли она. Повторная отправка станет доступна после проверки.'});
  }

  async function sendMessage(question) {
    if (busy || !question.trim()) return;
    setBusy(true);
    errorPanel.hidden = true;
    suggestion.hidden = true;
    const outgoing = appendMessage('user', question);
    document.getElementById('chat-loading').hidden = false;
    try {
      const result = await api('/api/chat', {message: question});
      appendMessage('assistant', result.answer, result);
      resourceKnown = result.action.type !== 'clarify';
      needsStatusCheck = result.card.status === 'unknown';
      renderCard(result.card, result.action.type !== 'none' && result.action.type !== 'clarify');
      input.value = '';
    } catch (error) {
      outgoing.remove();
      input.value = question;
      showError(error.message || 'Ответ не получен. Сообщение можно отправить ещё раз.');
    } finally {
      document.getElementById('chat-loading').hidden = true;
      setBusy(false);
      input.focus();
    }
  }

  composer.addEventListener('submit', event => {
    event.preventDefault();
    sendMessage(input.value.trim());
  });
  suggestion.addEventListener('click', () => sendMessage(suggestion.textContent));
  prepare.addEventListener('click', () => {
    draft.hidden = false;
    prepare.hidden = true;
    draft.querySelector('[type="submit"]').focus();
  });
  document.getElementById('cancel').addEventListener('click', () => {
    draft.hidden = true;
    prepare.hidden = false;
    prepare.focus();
  });
  draft.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || needsStatusCheck) return;
    setBusy(true);
    const submit = draft.querySelector('[type="submit"]');
    submit.textContent = 'Отправляем…';
    errorPanel.hidden = true;
    try {
      const result = await api('/api/requests', {resource_id: 'catalog-dev', access_level: 'read'});
      needsStatusCheck = false;
      renderCard(result.card);
    } catch (error) {
      if (['forbidden', 'resource_unknown', 'invalid_csrf', 'invalid_origin', 'session_expired'].includes(error.code)) {
        draft.hidden = true;
        prepare.hidden = true;
        showError(error.message);
      } else {
        needsStatusCheck = true;
        renderUnknown('Результат отправки пока неизвестен');
      }
    } finally {
      submit.textContent = 'Отправить заявку';
      setBusy(false);
    }
  });
  refresh.addEventListener('click', () => loadContext());
  document.getElementById('retry').addEventListener('click', () => loadContext(true));
  profile.addEventListener('change', async () => {
    setBusy(true);
    try {
      await api('/api/session', {profile_id: profile.value});
      window.location.reload();
    } catch (error) {
      showError(error.message);
      setBusy(false);
    }
  });
  loadContext(true);
})();
