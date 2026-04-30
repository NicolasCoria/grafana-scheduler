function showFeedback(elementId, type, message) {
    const node = document.getElementById(elementId);
    if (!node) {
        return;
    }
    node.className = `feedback ${type}`;
    node.textContent = message;
    node.classList.remove("hidden");
}

function escapeHtml(value) {
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

function getCsrfToken() {
    return document.querySelector('meta[name="csrf-token"]')?.content || "";
}

const nativeFetch = window.fetch.bind(window);
window.fetch = (input, init = {}) => {
    const method = String(init.method || input?.method || "GET").toUpperCase();
    const url = typeof input === "string" ? input : String(input?.url || "");
    const isPublicApi = url.includes("/api/v1/");
    const isAbsolute = /^https?:\/\//i.test(url);
    const isSameOrigin = !isAbsolute || url.startsWith(window.location.origin);

    if (isSameOrigin && !isPublicApi && !["GET", "HEAD", "OPTIONS"].includes(method)) {
        const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined) || {});
        const csrfToken = getCsrfToken();
        if (csrfToken) {
            headers.set("X-CSRF-Token", csrfToken);
        }
        init.headers = headers;
    }

    return nativeFetch(input, init);
};

function applyTheme(theme) {
    const normalizedTheme = "dark";
    document.body.dataset.theme = normalizedTheme;
    try {
        localStorage.setItem("grafanaSchedulerTheme", normalizedTheme);
    } catch (error) {
        console.debug("No se pudo persistir el tema", error);
    }
}

function hideFeedback(elementId) {
    const node = document.getElementById(elementId);
    if (!node) {
        return;
    }
    node.classList.add("hidden");
    node.textContent = "";
}

function toggleWizardPeriodFields(value) {
    const ids = ["wizard-periodo-diario", "wizard-periodo-semanal", "wizard-periodo-mensal"];
    ids.forEach((id) => {
        const node = document.getElementById(id);
        if (node) {
            node.classList.add("hidden");
        }
    });

    const requiredInputs = [
        document.querySelector("input[name='horario_diario']"),
        document.querySelector("input[name='horario_semanal']"),
        document.querySelector("input[name='horario_mensal']"),
    ];
    requiredInputs.forEach((input) => {
        if (input) {
            input.required = false;
        }
    });

    const targetId = {
        diario: "wizard-periodo-diario",
        semanal: "wizard-periodo-semanal",
        mensal: "wizard-periodo-mensal",
    }[value];

    if (targetId) {
        const node = document.getElementById(targetId);
        if (node) {
            node.classList.remove("hidden");
        }
    }

    const requiredField = {
        diario: document.querySelector("input[name='horario_diario']"),
        semanal: document.querySelector("input[name='horario_semanal']"),
        mensal: document.querySelector("input[name='horario_mensal']"),
    }[value];
    if (requiredField) {
        requiredField.required = true;
    }
}

function renderTelegramChats(chats, selectedIds = []) {
    const container = document.getElementById("telegram-chat-list");
    const emptyState = document.getElementById("telegram-empty-state");
    if (!container) {
        return;
    }

    container.innerHTML = "";
    if (!chats.length) {
        if (emptyState) {
            emptyState.classList.remove("hidden");
        }
        container.innerHTML = '<p class="empty-state">Ningún chat encontrado para este token.</p>';
        return;
    }
    if (emptyState) {
        emptyState.classList.add("hidden");
    }

    chats.forEach((chat) => {
        const checked = selectedIds.includes(String(chat.chat_id)) ? "checked" : "";
        container.insertAdjacentHTML(
            "beforeend",
            `
            <label class="chat-item">
                <input type="checkbox" ${checked} data-chat-id="${escapeHtml(chat.chat_id)}" data-chat-name="${escapeHtml(chat.name)}">
                <span>${escapeHtml(chat.name)}</span>
                <small>${escapeHtml(chat.chat_id)}</small>
            </label>
            `
        );
    });
}

function renderSavedTelegramBots(bots) {
    const container = document.getElementById("telegram-bot-list");
    if (!container) {
        return;
    }
    container.dataset.savedBots = JSON.stringify(bots);
    container.innerHTML = "";
    if (!bots.length) {
        container.innerHTML = '<p class="empty-state">Nenhum bot Telegram salvo.</p>';
        return;
    }

    bots.forEach((bot) => {
        container.insertAdjacentHTML(
            "beforeend",
            `
            <div class="chat-item bot-saved-item">
                <div class="bot-saved-copy">
                    <strong>${escapeHtml(bot.nome)}</strong>
                    <small>${bot.selected_chats.length} chat(s)</small>
                </div>
                <div class="inline-actions">
                    <button type="button" class="secondary-button" data-load-telegram-bot="${bot.id}">Editar</button>
                    <button type="button" class="danger-button" data-delete-telegram-bot="${bot.id}">Eliminar</button>
                </div>
            </div>
            `
        );
    });
}

function bindTelegramBotActions() {
    const container = document.getElementById("telegram-bot-list");
    if (!container) {
        return;
    }
    const savedBots = JSON.parse(container.dataset.savedBots || "[]");

    container.querySelectorAll("[data-load-telegram-bot]").forEach((button) => {
        button.addEventListener("click", () => {
            const bot = savedBots.find((item) => String(item.id) === String(button.dataset.loadTelegramBot));
            if (!bot) {
                return;
            }
            const nameInput = document.getElementById("telegram-bot-name");
            const tokenInput = document.getElementById("telegram-bot-token");
            if (nameInput) {
                nameInput.value = bot.nome;
                nameInput.dataset.botId = bot.id;
            }
            if (tokenInput) {
                tokenInput.value = "";
            }
            renderTelegramChats(bot.selected_chats, bot.selected_chats.map((chat) => String(chat.chat_id)));
            showFeedback("telegram-config-feedback", "success", `Editando configuración del bot ${bot.nome}.`);
        });
    });

    container.querySelectorAll("[data-delete-telegram-bot]").forEach((button) => {
        button.addEventListener("click", async () => {
            if (!window.confirm("Eliminar este bot Telegram?")) {
                return;
            }
            const response = await fetch(`/api/configuracoes/telegram/${button.dataset.deleteTelegramBot}`, { method: "DELETE" });
            const body = await response.json();
            showFeedback("telegram-config-feedback", response.ok ? "success" : "error", body.message);
            if (response.ok) {
                const nextBots = savedBots.filter((item) => String(item.id) !== String(button.dataset.deleteTelegramBot));
                renderSavedTelegramBots(nextBots);
                bindTelegramBotActions();
                const wizardForm = document.getElementById("scheduleWizardForm");
                if (wizardForm) {
                    const telegramConfig = JSON.parse(wizardForm.dataset.telegram || "{}");
                    const totalChats = nextBots.reduce((sum, bot) => sum + (bot.selected_chats?.length || 0), 0);
                    telegramConfig.bots = nextBots;
                    telegramConfig.bot_count = nextBots.length;
                    telegramConfig.chat_count = totalChats;
                    wizardForm.dataset.telegram = JSON.stringify(telegramConfig);
                    syncDeliverySections();
                }
                const nameInput = document.getElementById("telegram-bot-name");
                if (nameInput && String(nameInput.dataset.botId || "") === String(button.dataset.deleteTelegramBot)) {
                    nameInput.value = "";
                    nameInput.dataset.botId = "";
                    const tokenInput = document.getElementById("telegram-bot-token");
                    if (tokenInput) {
                        tokenInput.value = "";
                    }
                    renderTelegramChats([]);
                }
            }
        });
    });
}

function getSelectedTelegramChats() {
    return Array.from(document.querySelectorAll("#telegram-chat-list input[type='checkbox']:checked")).map((input) => ({
        chat_id: input.dataset.chatId,
        name: input.dataset.chatName,
    }));
}

function setWizardStep(step) {
    const totalSteps = document.querySelectorAll(".wizard-panel").length;
    document.querySelectorAll(".wizard-panel").forEach((panel) => {
        panel.classList.toggle("hidden", panel.dataset.step !== String(step));
    });
    document.querySelectorAll(".wizard-step").forEach((indicator) => {
        indicator.classList.toggle("active", indicator.dataset.stepIndicator === String(step));
    });

    const backButton = document.getElementById("wizard-back");
    const nextButton = document.getElementById("wizard-next");
    const submitButton = document.getElementById("wizard-submit");

    if (backButton) {
        backButton.disabled = step === 1;
    }
    if (nextButton) {
        nextButton.classList.toggle("hidden", step === totalSteps);
    }
    if (submitButton) {
        submitButton.classList.toggle("hidden", step !== totalSteps);
    }

    const wizardModeCopy = document.getElementById("wizard-mode-copy");
    const activePanel = document.querySelector(`.wizard-panel[data-step="${step}"]`);
    if (wizardModeCopy && activePanel?.dataset.stepCopy) {
        wizardModeCopy.textContent = activePanel.dataset.stepCopy;
    }

    const wizardView = document.getElementById("schedule-wizard-view");
    if (wizardView && !wizardView.classList.contains("hidden")) {
        wizardView.scrollIntoView({ behavior: "smooth", block: "start" });
    }
}

function collectSelectedTargets() {
    const selectedInput = document.querySelector(".dashboard-checkbox:checked");
    if (!selectedInput) {
        return [];
    }

    return [
        {
            type: "dashboard",
            uid: selectedInput.dataset.uid,
            title: selectedInput.dataset.title,
            url: selectedInput.dataset.url,
        },
    ];
}

function applySelectedTargetsToCatalog(selectedTargets) {
    const normalizedTarget = (selectedTargets || []).find((item) => item?.type === "dashboard" && item?.uid)
        || (selectedTargets || [])
            .filter((item) => item?.type === "folder")
            .flatMap((item) => item.dashboards || [])
            .find((item) => item?.uid);

    document.querySelectorAll(".dashboard-checkbox").forEach((input) => {
        input.checked = Boolean(normalizedTarget) && String(normalizedTarget.uid) === String(input.dataset.uid);
    });
}

function buildWizardEmailRow(email = "") {
    return `
        <div class="email-input-group">
            <label>
                <span>E-mail</span>
                <input type="email" name="emails[]" value="${escapeHtml(email)}" required>
            </label>
            <button type="button" class="danger-button remove-email-button">Eliminar</button>
        </div>
    `;
}

function rebuildWizardEmailList(emails) {
    const list = document.getElementById("wizard-email-list");
    if (!list) {
        return;
    }
    const safeEmails = emails.length ? emails : [""];
    list.innerHTML = safeEmails.map((email) => buildWizardEmailRow(email)).join("");
}

function getWizardEmailInputs() {
    return Array.from(document.querySelectorAll("#wizard-email-list input[name='emails[]']"));
}

function ensureWizardEmailInputs() {
    const list = document.getElementById("wizard-email-list");
    if (!list) {
        return [];
    }

    let inputs = getWizardEmailInputs();
    if (!inputs.length) {
        list.innerHTML = buildWizardEmailRow("");
        inputs = getWizardEmailInputs();
    }
    return inputs;
}

function validateWizardEmailRecipients() {
    const methods = collectDeliveryMethods();
    if (!methods.includes("email")) {
        return true;
    }

    const inputs = ensureWizardEmailInputs();
    const invalidInput = inputs.find((input) => !input.value.trim());
    if (invalidInput) {
        invalidInput.required = true;
        invalidInput.reportValidity();
        invalidInput.focus();
        showFeedback("schedule-feedback", "error", "Informe por lo menos un destinatario de e-mail.");
        return false;
    }

    const malformedInput = inputs.find((input) => !input.reportValidity());
    if (malformedInput) {
        malformedInput.focus();
        showFeedback("schedule-feedback", "error", "Revise los destinatarios de e-mail informados.");
        return false;
    }

    return true;
}

function renderGrafanaCatalog(catalog) {
    const container = document.getElementById("grafana-catalog");
    if (!container) {
        return;
    }

    container.innerHTML = "";
    const { folders, dashboards_without_folder: dashboardsWithoutFolder } = catalog;

    if (!folders.length && !dashboardsWithoutFolder.length) {
        container.innerHTML = '<p class="empty-state catalog-empty-state">Ningún dashboard encontrado.</p>';
        return;
    }

    folders.forEach((folder) => {
        const dashboardsHtml = folder.dashboards.length
            ? folder.dashboards
                .map(
                    (dashboard) => `
                        <label class="catalog-item dashboard-item">
                            <input
                                type="radio"
                                class="dashboard-checkbox"
                                name="selected_dashboard_uid"
                                data-uid="${escapeHtml(dashboard.uid)}"
                                data-title="${escapeHtml(dashboard.title)}"
                                data-url="${escapeHtml(dashboard.url)}"
                            >
                            <div class="catalog-item-copy">
                                <strong>${escapeHtml(dashboard.title)}</strong>
                            </div>
                        </label>
                    `
                )
                .join("")
            : '<p class="empty-state catalog-empty-state">Ningún dashboard en esta carpeta.</p>';

        container.insertAdjacentHTML(
            "beforeend",
            `
            <section class="catalog-folder">
                <div class="catalog-item folder-item folder-heading">
                    <strong>${escapeHtml(folder.title)}</strong>
                </div>
                <div class="catalog-dashboards">${dashboardsHtml}</div>
            </section>
            `
        );
    });

    if (dashboardsWithoutFolder.length) {
        container.insertAdjacentHTML(
            "beforeend",
            `
            <section class="catalog-folder">
                <div class="catalog-item folder-item folder-heading"><strong>Dashboards sin carpeta</strong></div>
                <div class="catalog-dashboards">
                    ${dashboardsWithoutFolder
                        .map(
                            (dashboard) => `
                            <label class="catalog-item dashboard-item">
                                <input
                                    type="radio"
                                    class="dashboard-checkbox"
                                    name="selected_dashboard_uid"
                                    data-uid="${escapeHtml(dashboard.uid)}"
                                    data-title="${escapeHtml(dashboard.title)}"
                                    data-url="${escapeHtml(dashboard.url)}"
                                >
                                <div class="catalog-item-copy">
                                    <strong>${escapeHtml(dashboard.title)}</strong>
                                </div>
                            </label>
                        `
                        )
                        .join("")}
                </div>
            </section>
            `
        );
    }
}

function collectDeliveryMethods() {
    return Array.from(document.querySelectorAll("input[name='delivery_method']:checked")).map((input) => input.value);
}

function syncDeliverySections() {
    const methods = collectDeliveryMethods();
    const emailSection = document.getElementById("wizard-email-section");
    const telegramSection = document.getElementById("wizard-telegram-summary");
    const telegramEstado = document.getElementById("wizard-telegram-config-status");
    const emailInputs = methods.includes("email") ? ensureWizardEmailInputs() : getWizardEmailInputs();
    const wizardForm = document.getElementById("scheduleWizardForm");
    const telegramConfig = wizardForm ? JSON.parse(wizardForm.dataset.telegram || "{}") : {};
    const botCount = Number(telegramConfig.bot_count || 0);
    const chatCount = Number(telegramConfig.chat_count || 0);

    if (emailSection) {
        emailSection.classList.toggle("hidden", !methods.includes("email"));
    }
    if (telegramSection) {
        telegramSection.classList.toggle("hidden", !methods.includes("telegram"));
    }
    if (telegramEstado) {
        telegramEstado.textContent = botCount && chatCount
            ? `${botCount} bot(s) e ${chatCount} chat(s) serán usados en esta programación.`
            : "Ningún bot o chat Telegram está configurado.";
    }
    emailInputs.forEach((input) => {
        input.required = methods.includes("email");
    });
}

function syncAiWizard() {
    const useAiInput = document.getElementById("wizard-use-ai");
    const providerWrapper = document.getElementById("wizard-ai-provider-wrapper");
    const providerSelect = document.getElementById("wizard-ai-provider");
    const promptWrapper = document.getElementById("wizard-ai-prompt-wrapper");
    const promptSelect = document.getElementById("wizard-ai-prompt");
    const instructionWrapper = document.getElementById("wizard-ai-instruction-wrapper");
    if (!useAiInput || !providerWrapper) {
        return;
    }
    providerWrapper.classList.toggle("hidden", !useAiInput.checked);
    if (promptWrapper) {
        promptWrapper.classList.toggle("hidden", !useAiInput.checked);
    }
    if (instructionWrapper) {
        instructionWrapper.classList.toggle("hidden", !useAiInput.checked);
    }
    if (providerSelect) {
        providerSelect.required = useAiInput.checked;
    }
    if (promptSelect) {
        promptSelect.required = useAiInput.checked;
    }
}

function readFileAsDataUrl(file) {
    return new Promise((resolve, reject) => {
        if (!file) {
            resolve("");
            return;
        }
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ""));
        reader.onerror = () => reject(new Errorr("Fallo al leer archivo."));
        reader.readAsDataURL(file);
    });
}

function setTemplateImagePreview(prefix, value) {
    const image = document.getElementById(`report-template-${prefix}-preview`);
    const emptyState = document.getElementById(`report-template-${prefix}-empty`);
    if (!image || !emptyState) {
        return;
    }
    if (value) {
        image.src = value;
        image.classList.remove("hidden");
        emptyState.classList.add("hidden");
        return;
    }
    image.removeAttribute("src");
    image.classList.add("hidden");
    emptyState.classList.remove("hidden");
}

function renderReportTemplateCards(templates) {
    const container = document.getElementById("report-template-list");
    if (!container) {
        return;
    }
    container.dataset.templates = JSON.stringify(templates);
    if (!templates.length) {
        container.innerHTML = '<p class="empty-state">Ningún template registrado.</p>';
        return;
    }
    container.innerHTML = templates.map((template) => `
        <article class="template-card template-library-card">
            <div class="template-library-head">
                <div class="template-library-copy">
                    <p class="card-kicker">Template</p>
                    <strong>${escapeHtml(template.nome)}</strong>
                    <p>${escapeHtml(template.header_text || "Sin encabezado definido")}</p>
                </div>
                <div class="template-library-actions">
                    <button type="button" class="table-action-button secondary-button" data-edit-report-template="${template.id}">Editar</button>
                    <button type="button" class="table-action-button danger-button" data-delete-report-template="${template.id}">Eliminar</button>
                </div>
            </div>
            <div class="template-meta-grid">
                <div class="template-meta-item">
                    <span class="template-meta-label">Fuente</span>
                    <strong>${escapeHtml(template.font_family)}</strong>
                </div>
                <div class="template-meta-item">
                    <span class="template-meta-label">Titulo</span>
                    <strong>${escapeHtml(template.title_font_size)}px</strong>
                </div>
                <div class="template-meta-item">
                    <span class="template-meta-label">Texto</span>
                    <strong>${escapeHtml(template.body_font_size)}px</strong>
                </div>
                <div class="template-meta-item">
                    <span class="template-meta-label">Estrutura</span>
                    <strong>${template.show_summary ? "Con sumario" : "Sin sumario"}</strong>
                </div>
            </div>
        </article>
    `).join("");
}

function syncReportTemplateSelects(templates) {
    ["wizard-report-template", "report-editor-template"].forEach((id) => {
        const select = document.getElementById(id);
        if (!select) {
            return;
        }
        const currentValue = select.value;
        select.innerHTML = [
            '<option value="">Template estándar del sistema</option>',
            ...templates.map((template) => `<option value="${template.id}">${escapeHtml(template.nome)}</option>`),
        ].join("");
        if (currentValue && templates.some((template) => String(template.id) === String(currentValue))) {
            select.value = String(currentValue);
        }
    });
}

document.addEventListener("DOMContentLoaded", () => {
    applyTheme("dark");

    const listView = document.getElementById("schedule-list-view");
    const wizardView = document.getElementById("schedule-wizard-view");
    const showListButton = document.getElementById("show-schedule-list");
    const showWizardButton = document.getElementById("show-schedule-wizard");
    const wizardModeTitle = document.getElementById("wizard-mode-title");
    const wizardModeCopy = document.getElementById("wizard-mode-copy");
    const wizardCancelEdit = document.getElementById("wizard-cancel-edit");
    const wizardSubmit = document.getElementById("wizard-submit");

    const setScheduleViewMode = (mode) => {
        if (!listView || !wizardView || !showListButton || !showWizardButton) {
            return;
        }

        const showList = mode === "list";
        listView.classList.toggle("hidden", !showList);
        wizardView.classList.toggle("hidden", showList);
        showListButton.classList.toggle("is-active", showList);
        showWizardButton.classList.toggle("is-active", !showList);
        showListButton.setAttribute("aria-pressed", showList ? "true" : "false");
        showWizardButton.setAttribute("aria-pressed", showList ? "false" : "true");
    };

    const resetWizardMode = () => {
        const wizardForm = document.getElementById("scheduleWizardForm");
        if (!wizardForm) {
            return;
        }
        wizardForm.reset();
        document.getElementById("schedule-id").value = "";
        document.getElementById("grafana-catalog").innerHTML = "";
        rebuildWizardEmailList([""]);
        if (wizardModeTitle) {
            wizardModeTitle.textContent = "Nueva Programación";
        }
        if (wizardModeCopy) {
            wizardModeCopy.textContent = "Llene el flujo completo y guarde cuando esté listo.";
        }
        if (wizardSubmit) {
            wizardSubmit.textContent = "Guardar Programación";
        }
        if (wizardCancelEdit) {
            wizardCancelEdit.classList.add("hidden");
        }
        toggleWizardPeriodFields("diario");
        syncDeliverySections();
        syncAiWizard();
    };

    if (showListButton && showWizardButton && listView && wizardView) {
        showListButton.addEventListener("click", () => {
            setScheduleViewMode("list");
        });
        showWizardButton.addEventListener("click", () => {
            resetWizardMode();
            setScheduleViewMode("wizard");
            setWizardStep(1);
        });
        setScheduleViewMode(wizardView.classList.contains("hidden") ? "list" : "wizard");
    }

    document.querySelectorAll("[data-delete-schedule]").forEach((button) => {
        button.addEventListener("click", async () => {
            if (!window.confirm("Eliminar esta programación?")) {
                return;
            }
            const response = await fetch(`/api/agendamentos/${button.dataset.deleteSchedule}`, { method: "DELETE" });
            if (response.ok) {
                window.location.reload();
            }
        });
    });

    document.querySelectorAll("[data-run-schedule]").forEach((button) => {
        button.addEventListener("click", async () => {
            if (!window.confirm("Ejecutar esta programación inmediatamente?")) {
                return;
            }
            const response = await fetch(`/api/agendamentos/${button.dataset.runSchedule}/executar`, { method: "POST" });
            const body = await response.json();
            showFeedback("schedule-feedback", response.ok ? "success" : "error", body.message);
        });
    });

    const grafanaServerForm = document.getElementById("grafanaServerForm");
    if (grafanaServerForm) {
        const grafanaServerIdInput = document.getElementById("grafana-server-id");
        const grafanaServerFormTitle = document.getElementById("grafana-server-form-title");
        const grafanaServerFormCopy = document.getElementById("grafana-server-form-copy");
        const grafanaServerSubmit = document.getElementById("grafana-server-submit");
        const grafanaServerCancel = document.getElementById("grafana-server-cancel");
        const passwordInput = grafanaServerForm.querySelector("input[name='password']");
        const tokenInput = grafanaServerForm.querySelector("input[name='service_account_token']");

        const resetGrafanaServerForm = () => {
            grafanaServerForm.reset();
            if (grafanaServerIdInput) {
                grafanaServerIdInput.value = "";
            }
            if (grafanaServerFormTitle) {
                grafanaServerFormTitle.textContent = "Nuevo origen Grafana";
            }
            if (grafanaServerFormCopy) {
                grafanaServerFormCopy.textContent = "Informe los datos del origen que será usado en el wizard y en el scheduler.";
            }
            if (grafanaServerSubmit) {
                grafanaServerSubmit.textContent = "Salvar servidor";
            }
            if (grafanaServerCancel) {
                grafanaServerCancel.classList.add("hidden");
            }
            if (passwordInput) {
                passwordInput.required = true;
            }
            if (tokenInput) {
                tokenInput.required = true;
            }
        };

        const enterGrafanaServerEditMode = (server) => {
            grafanaServerForm.reset();
            hideFeedback("grafana-server-feedback");
            if (grafanaServerIdInput) {
                grafanaServerIdInput.value = String(server.id || "");
            }
            grafanaServerForm.querySelector("input[name='nome']").value = server.nome || "";
            grafanaServerForm.querySelector("input[name='base_url']").value = server.base_url || "";
            grafanaServerForm.querySelector("input[name='username']").value = server.username || "";
            if (passwordInput) {
                passwordInput.required = false;
            }
            if (tokenInput) {
                tokenInput.required = false;
            }
            if (grafanaServerFormTitle) {
                grafanaServerFormTitle.textContent = "Editar origen Grafana";
            }
            if (grafanaServerFormCopy) {
                grafanaServerFormCopy.textContent = "Actualice nombre, URL y credenciales solo si necesita cambiar los valores actuales.";
            }
            if (grafanaServerSubmit) {
                grafanaServerSubmit.textContent = "Guardar alteraciones";
            }
            if (grafanaServerCancel) {
                grafanaServerCancel.classList.remove("hidden");
            }
            grafanaServerForm.scrollIntoView({ behavior: "smooth", block: "start" });
        };

        document.querySelectorAll("[data-edit-server]").forEach((button) => {
            button.addEventListener("click", () => {
                const payload = JSON.parse(button.dataset.editServer || "{}");
                enterGrafanaServerEditMode(payload);
            });
        });

        if (grafanaServerCancel) {
            grafanaServerCancel.addEventListener("click", () => {
                resetGrafanaServerForm();
                hideFeedback("grafana-server-feedback");
            });
        }

        grafanaServerForm.addEventListener("submit", async (event) => {
            event.preventDefault();
            hideFeedback("grafana-server-feedback");
            const formData = new FormData(grafanaServerForm);
            const payload = Object.fromEntries(formData.entries());
            const response = await fetch("/api/servidores-grafana", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            const body = await response.json();
            showFeedback("grafana-server-feedback", response.ok ? "success" : "error", body.message);
            if (response.ok) {
                resetGrafanaServerForm();
                setTimeout(() => window.location.reload(), 600);
            }
        });

        resetGrafanaServerForm();
    }

    document.querySelectorAll("[data-delete-server]").forEach((button) => {
        button.addEventListener("click", async () => {
            if (!window.confirm("Eliminar este servidor Grafana?")) {
                return;
            }
            const response = await fetch(`/api/servidores-grafana/${button.dataset.deleteServer}`, { method: "DELETE" });
            const body = await response.json().catch(() => ({ message: "Fallo ao excluir servidor Grafana." }));
            showFeedback("grafana-server-feedback", response.ok ? "success" : "error", body.message || "Fallo ao excluir servidor Grafana.");
            if (response.ok) {
                window.location.reload();
            }
        });
    });

    const emailConfigForm = document.getElementById("emailConfigForm");
    if (emailConfigForm) {
        emailConfigForm.addEventListener("submit", async (event) => {
            event.preventDefault();
            const formData = new FormData(emailConfigForm);
            const payload = {
                smtp_server: formData.get("smtp_server"),
                smtp_port: formData.get("smtp_port"),
                smtp_username: formData.get("smtp_username"),
                smtp_password: formData.get("smtp_password"),
                smtp_from_email: formData.get("smtp_from_email"),
                smtp_use_tls: formData.get("smtp_use_tls") === "on",
            };

            const response = await fetch("/api/configuracoes/email", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            const body = await response.json();
            showFeedback("email-config-feedback", response.ok ? "success" : "error", body.message);
        });
    }

    const fetchTelegramChatsButton = document.getElementById("fetch-telegram-chats");
    if (fetchTelegramChatsButton) {
        fetchTelegramChatsButton.addEventListener("click", async () => {
            const tokenInput = document.getElementById("telegram-bot-token");
            const botToken = tokenInput.value.trim();
            if (!botToken) {
                showFeedback("telegram-config-feedback", "error", "Informe el Bot Token para buscar chats.");
                return;
            }

            const response = await fetch("/api/configuracoes/telegram/chats", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ bot_token: botToken }),
            });
            const body = await response.json();
            if (!response.ok) {
                showFeedback("telegram-config-feedback", "error", body.message);
                return;
            }

            renderTelegramChats(body.chats);
            showFeedback("telegram-config-feedback", "success", "Chats cargados con éxito.");
        });
    }

    const clearTelegramFormButton = document.getElementById("clear-telegram-form");
    if (clearTelegramFormButton) {
        clearTelegramFormButton.addEventListener("click", () => {
            const nameInput = document.getElementById("telegram-bot-name");
            const tokenInput = document.getElementById("telegram-bot-token");
            if (nameInput) {
                nameInput.value = "";
                nameInput.dataset.botId = "";
            }
            if (tokenInput) {
                tokenInput.value = "";
            }
            renderTelegramChats([]);
            hideFeedback("telegram-config-feedback");
        });
    }

    const saveTelegramConfigButton = document.getElementById("save-telegram-config");
    if (saveTelegramConfigButton) {
        saveTelegramConfigButton.addEventListener("click", async () => {
            const nameInput = document.getElementById("telegram-bot-name");
            const tokenInput = document.getElementById("telegram-bot-token");
            const payload = {
                bot_id: nameInput?.dataset.botId ? Number(nameInput.dataset.botId) : null,
                nome: nameInput?.value.trim() || "",
                bot_token: tokenInput.value.trim(),
                selected_chats: getSelectedTelegramChats(),
            };

            const response = await fetch("/api/configuracoes/telegram", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            const body = await response.json();
            showFeedback("telegram-config-feedback", response.ok ? "success" : "error", body.message);
            if (response.ok) {
                const container = document.getElementById("telegram-bot-list");
                const savedBots = JSON.parse(container.dataset.savedBots || "[]");
                const existingIndex = savedBots.findIndex((item) => item.id === body.bot_id);
                const nextBot = {
                    id: body.bot_id,
                    nome: payload.nome,
                    token_configured: true,
                    selected_chats: payload.selected_chats,
                };
                if (existingIndex >= 0) {
                    savedBots[existingIndex] = nextBot;
                } else {
                    savedBots.push(nextBot);
                }
                renderSavedTelegramBots(savedBots);
                bindTelegramBotActions();
                const wizardForm = document.getElementById("scheduleWizardForm");
                if (wizardForm) {
                    const telegramConfig = JSON.parse(wizardForm.dataset.telegram || "{}");
                    const totalChats = savedBots.reduce((sum, bot) => sum + (bot.selected_chats?.length || 0), 0);
                    telegramConfig.bots = savedBots;
                    telegramConfig.bot_count = savedBots.length;
                    telegramConfig.chat_count = totalChats;
                    wizardForm.dataset.telegram = JSON.stringify(telegramConfig);
                    syncDeliverySections();
                }
                if (nameInput) {
                    nameInput.dataset.botId = String(body.bot_id);
                }
                if (tokenInput) {
                    tokenInput.value = "";
                }
            }
        });
    }

    bindTelegramBotActions();

    const aiConfigForm = document.getElementById("aiConfigForm");
    if (aiConfigForm) {
        const aiProviderSelect = document.getElementById("ai-provider-select");
        const endpointWrapper = document.getElementById("ai-endpoint-wrapper");
        const aiModelInput = aiConfigForm.querySelector("input[name='model']");
        const aiTestButton = document.getElementById("ai-test-button");
        const defaultAiModels = {
            openai: "gpt-4o-mini",
            azure: "gpt-4o-mini",
            claude: "claude-sonnet-4-6",
        };

        const shouldRefreshAiModel = (provider, currentValue) => {
            const normalizedValue = String(currentValue || "").trim().toLowerCase();
            if (!normalizedValue) {
                return true;
            }
            if (Object.values(defaultAiModels).includes(normalizedValue)) {
                return true;
            }
            if (provider === "claude" && !normalizedValue.startsWith("claude-")) {
                return true;
            }
            if (provider !== "claude" && normalizedValue.startsWith("claude-")) {
                return true;
            }
            return false;
        };

        const syncProviderFields = ({ forceModel = false } = {}) => {
            const provider = aiProviderSelect?.value || "openai";
            if (endpointWrapper) {
                endpointWrapper.classList.toggle("hidden", provider !== "azure");
            }
            if (aiModelInput) {
                const suggestedModel = defaultAiModels[provider] || defaultAiModels.openai;
                if (forceModel || shouldRefreshAiModel(provider, aiModelInput.value)) {
                    aiModelInput.value = suggestedModel;
                }
                aiModelInput.placeholder = `Ex.: ${suggestedModel}`;
            }
        };

        syncProviderFields();
        if (aiProviderSelect) {
            aiProviderSelect.addEventListener("change", () => syncProviderFields({ forceModel: true }));
        }

        const buildAiConfigPayload = () => {
            const formData = new FormData(aiConfigForm);
            return {
                provider: String(formData.get("provider") || "").trim(),
                api_key: String(formData.get("api_key") || "").trim(),
                endpoint: String(formData.get("endpoint") || "").trim(),
                model: String(formData.get("model") || "").trim(),
            };
        };

        if (aiTestButton) {
            aiTestButton.addEventListener("click", async () => {
                aiTestButton.disabled = true;
                aiTestButton.textContent = "Probando...";
                hideFeedback("ai-config-feedback");

                try {
                    const response = await fetch("/api/configuracoes/ia/teste", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(buildAiConfigPayload()),
                    });
                    const body = await response.json();
                    const preview = body.response_preview ? `\nResposta: ${body.response_preview}` : "";
                    showFeedback("ai-config-feedback", response.ok ? "success" : "error", `${body.message || "Fallo ao testar a conexao."}${preview}`);
                } catch (error) {
                    showFeedback("ai-config-feedback", "error", `Fallo ao testar a conexao com a IA: ${error.message}`);
                } finally {
                    aiTestButton.disabled = false;
                    aiTestButton.textContent = "Probar conexión";
                }
            });
        }

        aiConfigForm.addEventListener("submit", async (event) => {
            event.preventDefault();
            const payload = buildAiConfigPayload();

            const response = await fetch("/api/configuracoes/ia", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            const body = await response.json();
            showFeedback("ai-config-feedback", response.ok ? "success" : "error", body.message);
        });
    }

    const executionConfigForm = document.getElementById("executionConfigForm");
    if (executionConfigForm) {
        const executionPurgeButton = document.getElementById("execution-purge-button");

        executionConfigForm.addEventListener("submit", async (event) => {
            event.preventDefault();
            hideFeedback("execution-config-feedback");

            const formData = new FormData(executionConfigForm);
            const payload = {
                max_concurrent_reports: Number(formData.get("max_concurrent_reports") || 0),
            };

            try {
                const response = await fetch("/api/configuracoes/execution", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                });
                const body = await response.json();
                showFeedback(
                    "execution-config-feedback",
                    response.ok ? "success" : "error",
                    body.message || "Fallo ao salvar a configuracao de execucao.",
                );
            } catch (error) {
                showFeedback("execution-config-feedback", "error", `Fallo ao salvar a configuracao de execucao: ${error.message}`);
            }
        });

        if (executionPurgeButton) {
            executionPurgeButton.addEventListener("click", async () => {
                if (!window.confirm("Limpiar toda a fila de workers e cancelar os envios em andamento?")) {
                    return;
                }

                hideFeedback("execution-config-feedback");
                executionPurgeButton.disabled = true;
                const originalLabel = executionPurgeButton.textContent;
                executionPurgeButton.textContent = "Limpiando fila...";

                try {
                    const response = await fetch("/api/configuracoes/execution/purge", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                    });
                    const body = await response.json();
                    showFeedback(
                        "execution-config-feedback",
                        response.ok ? "success" : "error",
                        body.message || "Fallo ao limpar a fila de workers.",
                    );
                } catch (error) {
                    showFeedback("execution-config-feedback", "error", `Fallo ao limpar a fila de workers: ${error.message}`);
                } finally {
                    executionPurgeButton.disabled = false;
                    executionPurgeButton.textContent = originalLabel;
                }
            });
        }
    }

    const aiPromptForm = document.getElementById("aiPromptForm");
    if (aiPromptForm) {
        const promptIdInput = document.getElementById("ai-prompt-id");
        const promptTitleInput = document.getElementById("ai-prompt-title");
        const promptTextInput = document.getElementById("ai-prompt-text");
        const promptClearButton = document.getElementById("ai-prompt-clear");
        const promptList = document.getElementById("ai-prompt-list");
        const promptCount = document.getElementById("ai-prompt-count");

        const getStoredPrompts = () => JSON.parse(promptList?.dataset.prompts || aiPromptForm.dataset.prompts || "[]");

        const renderPromptCards = (prompts) => {
            if (!promptList) {
                return;
            }
            promptList.dataset.prompts = JSON.stringify(prompts);
            if (promptCount) {
                promptCount.textContent = `${prompts.length} prompt(s)`;
            }
            if (!prompts.length) {
                promptList.innerHTML = '<p class="empty-state">Nenhum prompt cadastrado.</p>';
                return;
            }
            promptList.innerHTML = prompts.map((prompt) => `
                <article class="prompt-library-item" data-prompt-id="${escapeHtml(prompt.id)}">
                    <div class="prompt-library-copy">
                        <strong>${escapeHtml(prompt.titulo)}</strong>
                        <p>${escapeHtml((prompt.prompt_text || "").slice(0, 160))}${(prompt.prompt_text || "").length > 160 ? "..." : ""}</p>
                    </div>
                    <div class="compact-actions">
                        <button type="button" class="secondary-button" data-edit-ai-prompt="${prompt.id}">Editar</button>
                        <button type="button" class="danger-button" data-delete-ai-prompt="${prompt.id}">Eliminar</button>
                    </div>
                </article>
            `).join("");
        };

        const bindPromptActions = () => {
            document.querySelectorAll("[data-edit-ai-prompt]").forEach((button) => {
                button.addEventListener("click", () => {
                    const prompt = getStoredPrompts().find((item) => String(item.id) === String(button.dataset.editAiPrompt));
                    if (!prompt) {
                        return;
                    }
                    promptIdInput.value = prompt.id || "";
                    promptTitleInput.value = prompt.titulo || "";
                    promptTextInput.value = prompt.prompt_text || "";
                    hideFeedback("ai-prompt-feedback");
                    window.scrollTo({ top: 0, behavior: "smooth" });
                });
            });

            document.querySelectorAll("[data-delete-ai-prompt]").forEach((button) => {
                button.addEventListener("click", async () => {
                    if (!window.confirm("Eliminar este prompt?")) {
                        return;
                    }
                    const response = await fetch(`/api/prompts/${button.dataset.deleteAiPrompt}`, { method: "DELETE" });
                    const body = await response.json();
                    showFeedback("ai-prompt-feedback", response.ok ? "success" : "error", body.message);
                    if (!response.ok) {
                        return;
                    }
                    const nextPrompts = getStoredPrompts().filter((item) => String(item.id) !== String(button.dataset.deleteAiPrompt));
                    renderPromptCards(nextPrompts);
                    bindPromptActions();
                    if (String(promptIdInput.value || "") === String(button.dataset.deleteAiPrompt)) {
                        promptIdInput.value = "";
                        promptTitleInput.value = "";
                        promptTextInput.value = "";
                    }
                });
            });
        };

        if (promptClearButton) {
            promptClearButton.addEventListener("click", () => {
                promptIdInput.value = "";
                promptTitleInput.value = "";
                promptTextInput.value = "";
                hideFeedback("ai-prompt-feedback");
            });
        }

        aiPromptForm.addEventListener("submit", async (event) => {
            event.preventDefault();
            const payload = {
                prompt_id: promptIdInput.value || null,
                titulo: promptTitleInput.value.trim(),
                prompt_text: promptTextInput.value.trim(),
            };
            const response = await fetch("/api/prompts", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            const body = await response.json();
            showFeedback("ai-prompt-feedback", response.ok ? "success" : "error", body.message);
            if (!response.ok) {
                return;
            }
            const savedId = String(body.prompt_id);
            const currentPrompts = getStoredPrompts();
            const nextPrompt = { id: Number(savedId), titulo: payload.titulo, prompt_text: payload.prompt_text };
            const existingIndex = currentPrompts.findIndex((item) => String(item.id) === savedId);
            if (existingIndex >= 0) {
                currentPrompts[existingIndex] = nextPrompt;
            } else {
                currentPrompts.push(nextPrompt);
            }
            currentPrompts.sort((left, right) => left.titulo.localeCompare(right.titulo));
            renderPromptCards(currentPrompts);
            bindPromptActions();
            promptIdInput.value = savedId;
        });

        renderPromptCards(getStoredPrompts());
        bindPromptActions();
    }

    const apiTokenForm = document.getElementById("apiTokenForm");
    if (apiTokenForm) {
        const tokenList = document.getElementById("api-token-list");
        const createdTokenBox = document.getElementById("api-token-created");

        const renderApiTokens = (tokens) => {
            if (!tokenList) {
                return;
            }
            tokenList.dataset.tokens = JSON.stringify(tokens);
            if (!tokens.length) {
                tokenList.innerHTML = '<p class="empty-state">Nenhum token de API cadastrado.</p>';
                return;
            }
            tokenList.innerHTML = tokens
                .map(
                    (token) => `
                    <article class="token-item" data-api-token-id="${escapeHtml(token.id)}">
                        <div>
                            <strong>${escapeHtml(token.nome)}</strong>
                            <span>Creado en ${escapeHtml(token.created_at || "")}</span>
                        </div>
                        <button type="button" class="danger-button" data-delete-api-token="${escapeHtml(token.id)}">Eliminar</button>
                    </article>
                    `
                )
                .join("");
            bindApiTokenActions();
        };

        const bindApiTokenActions = () => {
            if (!tokenList) {
                return;
            }
            tokenList.querySelectorAll("[data-delete-api-token]").forEach((button) => {
                button.addEventListener("click", async () => {
                    const tokenId = button.dataset.deleteApiToken;
                    const response = await fetch(`/api/configuracoes/api-token/${tokenId}`, { method: "DELETE" });
                    const body = await response.json();
                    showFeedback("api-token-feedback", response.ok ? "success" : "error", body.message);
                    if (response.ok) {
                        const tokens = JSON.parse(tokenList.dataset.tokens || "[]").filter(
                            (item) => String(item.id) !== String(tokenId)
                        );
                        renderApiTokens(tokens);
                    }
                });
            });
        };

        apiTokenForm.addEventListener("submit", async (event) => {
            event.preventDefault();
            const formData = new FormData(apiTokenForm);
            const payload = {
                nome: formData.get("nome"),
            };
            const response = await fetch("/api/configuracoes/api-token", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            const body = await response.json();
            showFeedback("api-token-feedback", response.ok ? "success" : "error", body.message);
            if (response.ok && body.token) {
                apiTokenForm.reset();
                if (createdTokenBox) {
                    createdTokenBox.classList.remove("hidden");
                    createdTokenBox.innerHTML = `
                        <strong>Salve este token agora.</strong>
                        <span>Token gerado para ${escapeHtml(body.token.nome)}. Ele nao sera exibido novamente.</span>
                        <code>${escapeHtml(body.token.value)}</code>
                    `;
                }
                const tokens = JSON.parse(tokenList?.dataset.tokens || "[]");
                tokens.unshift({
                    id: body.token.id,
                    nome: body.token.nome,
                    created_at: "agora",
                });
                renderApiTokens(tokens);
            }
        });

        renderApiTokens(JSON.parse(tokenList?.dataset.tokens || "[]"));
    }

    const reportTemplateForm = document.getElementById("reportTemplateForm");
    if (reportTemplateForm) {
        let logoBase64 = "";

        const templateIdInput = document.getElementById("report-template-id");
        const templateNameInput = document.getElementById("report-template-name");
        const templateHeaderInput = document.getElementById("report-template-header");
        const templateFontInput = document.getElementById("report-template-font");
        const templateSummaryInput = document.getElementById("report-template-summary");
        const templatePrimaryInput = document.getElementById("report-template-primary");
        const templateSecondaryInput = document.getElementById("report-template-secondary");
        const templateTitleSizeInput = document.getElementById("report-template-title-size");
        const templateBodySizeInput = document.getElementById("report-template-body-size");
        const templateLogoInput = document.getElementById("report-template-logo");
        const clearTemplateButton = document.getElementById("report-template-clear");
        const templateList = document.getElementById("report-template-list");

        const getStoredTemplates = () => JSON.parse(templateList?.dataset.templates || reportTemplateForm.dataset.templates || "[]");

        const resetTemplateForm = () => {
            reportTemplateForm.reset();
            if (templateIdInput) {
                templateIdInput.value = "";
            }
            logoBase64 = "";
            setTemplateImagePreview("logo", "");
            if (templatePrimaryInput) {
                templatePrimaryInput.value = "#f97316";
            }
            if (templateSecondaryInput) {
                templateSecondaryInput.value = "#0f172a";
            }
            if (templateTitleSizeInput) {
                templateTitleSizeInput.value = "20";
            }
            if (templateBodySizeInput) {
                templateBodySizeInput.value = "11";
            }
        };

        const fillTemplateForm = (template) => {
            if (!template) {
                resetTemplateForm();
                return;
            }
            templateIdInput.value = template.id || "";
            templateNameInput.value = template.nome || "";
            templateHeaderInput.value = template.header_text || "";
            templateFontInput.value = template.font_family || "Helvetica";
            templateSummaryInput.checked = Boolean(template.show_summary);
            templatePrimaryInput.value = template.primary_color || "#f97316";
            templateSecondaryInput.value = template.secondary_color || "#0f172a";
            templateTitleSizeInput.value = String(template.title_font_size || 20);
            templateBodySizeInput.value = String(template.body_font_size || 11);
            logoBase64 = template.logo_base64 || "";
            setTemplateImagePreview("logo", logoBase64);
            window.scrollTo({ top: 0, behavior: "smooth" });
        };

        const bindTemplateListActions = () => {
            document.querySelectorAll("[data-edit-report-template]").forEach((button) => {
                button.addEventListener("click", () => {
                    const template = getStoredTemplates().find((item) => String(item.id) === String(button.dataset.editReportTemplate));
                    fillTemplateForm(template);
                    hideFeedback("report-template-feedback");
                });
            });

            document.querySelectorAll("[data-delete-report-template]").forEach((button) => {
                button.addEventListener("click", async () => {
                    if (!window.confirm("Eliminar este template de relatorio?")) {
                        return;
                    }
                    const response = await fetch(`/api/relatorios/templates/${button.dataset.deleteReportTemplate}`, { method: "DELETE" });
                    const body = await response.json();
                    showFeedback("report-template-feedback", response.ok ? "success" : "error", body.message);
                    if (!response.ok) {
                        return;
                    }
                    const nextTemplates = getStoredTemplates().filter((item) => String(item.id) !== String(button.dataset.deleteReportTemplate));
                    renderReportTemplateCards(nextTemplates);
                    syncReportTemplateSelects(nextTemplates);
                    bindTemplateListActions();
                    if (String(templateIdInput.value || "") === String(button.dataset.deleteReportTemplate)) {
                        resetTemplateForm();
                    }
                });
            });
        };

        [["logo", templateLogoInput, (value) => { logoBase64 = value; }]].forEach(([prefix, input, setter]) => {
            if (!input) {
                return;
            }
            input.addEventListener("change", async () => {
                try {
                    const value = await readFileAsDataUrl(input.files?.[0]);
                    setter(value);
                    setTemplateImagePreview(prefix, value);
                } catch (error) {
                    showFeedback("report-template-feedback", "error", error.message);
                }
            });
        });

        if (clearTemplateButton) {
            clearTemplateButton.addEventListener("click", () => {
                resetTemplateForm();
                hideFeedback("report-template-feedback");
            });
        }

        reportTemplateForm.addEventListener("submit", async (event) => {
            event.preventDefault();
            const payload = {
                template_id: templateIdInput.value || null,
                nome: templateNameInput.value.trim(),
                header_text: templateHeaderInput.value.trim(),
                font_family: templateFontInput.value,
                show_summary: templateSummaryInput.checked,
                primary_color: templatePrimaryInput.value,
                secondary_color: templateSecondaryInput.value,
                title_font_size: templateTitleSizeInput.value,
                body_font_size: templateBodySizeInput.value,
                logo_base64: logoBase64,
            };

            const response = await fetch("/api/relatorios/templates", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            const body = await response.json();
            showFeedback("report-template-feedback", response.ok ? "success" : "error", body.message);
            if (!response.ok) {
                return;
            }

            const savedId = String(body.template_id);
            const currentTemplates = getStoredTemplates();
            const nextTemplate = {
                id: Number(savedId),
                nome: payload.nome,
                header_text: payload.header_text,
                font_family: payload.font_family,
                show_summary: payload.show_summary,
                primary_color: payload.primary_color,
                secondary_color: payload.secondary_color,
                title_font_size: Number(payload.title_font_size),
                body_font_size: Number(payload.body_font_size),
                logo_base64: logoBase64,
            };
            const existingIndex = currentTemplates.findIndex((item) => String(item.id) === savedId);
            if (existingIndex >= 0) {
                currentTemplates[existingIndex] = nextTemplate;
            } else {
                currentTemplates.push(nextTemplate);
            }
            currentTemplates.sort((left, right) => left.nome.localeCompare(right.nome));
            renderReportTemplateCards(currentTemplates);
            syncReportTemplateSelects(currentTemplates);
            bindTemplateListActions();
            templateIdInput.value = savedId;
        });

        renderReportTemplateCards(getStoredTemplates());
        syncReportTemplateSelects(getStoredTemplates());
        bindTemplateListActions();
        resetTemplateForm();
    }

    const wizardForm = document.getElementById("scheduleWizardForm");
    if (wizardForm) {
        let currentStep = 1;
        let loadedCatalog = null;
        const totalSteps = document.querySelectorAll(".wizard-panel").length;
        const aiConfig = JSON.parse(wizardForm.dataset.ai || "{}");
        const wizardAiProvider = document.getElementById("wizard-ai-provider");
        const wizardAiPrompt = document.getElementById("wizard-ai-prompt");
        const scheduleIdInput = document.getElementById("schedule-id");
        const useAiInput = document.getElementById("wizard-use-ai");

        const goToStep = (step) => {
            currentStep = Math.max(1, Math.min(totalSteps, step));
            setWizardStep(currentStep);
            hideFeedback("schedule-feedback");
        };

        const validateVisibleFields = (step) => {
            const panel = document.querySelector(`.wizard-panel[data-step="${step}"]`);
            if (!panel) {
                return true;
            }

            const fields = Array.from(panel.querySelectorAll("input, select, textarea")).filter((field) => {
                if (field.disabled || !field.required) {
                    return false;
                }
                return field.offsetParent !== null;
            });

            return fields.every((field) => field.reportValidity());
        };

        const validateStepTransition = (step) => {
            if (!validateVisibleFields(step)) {
                return false;
            }

            const currentTelegramConfig = JSON.parse(wizardForm.dataset.telegram || "{}");
            if (step === 2) {
                const selectedTargets = collectSelectedTargets();
                if (!loadedCatalog) {
                    showFeedback("schedule-feedback", "error", "Cargue el catálogo del servidor Grafana.");
                    return false;
                }
                if (!selectedTargets.length) {
                    showFeedback("schedule-feedback", "error", "Seleccione un dashboard.");
                    return false;
                }
            }

            if (step === 4) {
                const deliveryMethods = collectDeliveryMethods();
                if (!deliveryMethods.length) {
                    showFeedback("schedule-feedback", "error", "Seleccione al menos un método de envío.");
                    return false;
                }
                if (!validateWizardEmailRecipients()) {
                    return false;
                }
                if (deliveryMethods.includes("telegram") && !Number(currentTelegramConfig.chat_count || 0)) {
                    showFeedback("schedule-feedback", "error", "Telegram fue seleccionado, pero no hay chats configurados.");
                    return false;
                }
            }

            return true;
        };

        const loadCatalog = async (serverId, selectedTargets = []) => {
            const response = await fetch(`/api/servidores-grafana/${serverId}/catalogo`);
            const body = await response.json();
            if (!response.ok) {
                showFeedback("grafana-catalog-feedback", "error", body.message);
                return false;
            }

            loadedCatalog = body.catalog;
            renderGrafanaCatalog(body.catalog);
            applySelectedTargetsToCatalog(selectedTargets);
                showFeedback("grafana-catalog-feedback", "success", "Catálogo de Grafana cargado.");
            hideFeedback("schedule-feedback");
            return true;
        };

        const enterEditMode = async (schedule) => {
            resetWizardMode();
            setScheduleViewMode("wizard");
            scheduleIdInput.value = String(schedule.id);
            if (wizardModeTitle) {
                wizardModeTitle.textContent = `Editando #${schedule.id}`;
            }
            if (wizardModeCopy) {
                wizardModeCopy.textContent = "Ajuste la programación existente y guarde para sustituir la configuración actual.";
            }
            if (wizardSubmit) {
                wizardSubmit.textContent = "Atualizar Programación";
            }
            if (wizardCancelEdit) {
                wizardCancelEdit.classList.remove("hidden");
            }

            wizardForm.querySelector("input[name='titulo']").value = schedule.titulo || "";
            wizardForm.querySelector("input[name='report_subject']").value = schedule.report_subject || "";
            wizardForm.querySelector("#grafana-server-select").value = schedule.grafana_server_id || "";
            wizardForm.querySelector("#wizard-periodo").value = schedule.periodo || "diario";
            toggleWizardPeriodFields(schedule.periodo || "diario");
            if (schedule.periodo === "semanal") {
                wizardForm.querySelector("select[name='dia_semana']").value = schedule.detalhe_periodo || "segunda";
                wizardForm.querySelector("input[name='horario_semanal']").value = schedule.horario || "";
            } else if (schedule.periodo === "mensal") {
                wizardForm.querySelector("select[name='dia_mes']").value = schedule.detalhe_periodo || "1";
                wizardForm.querySelector("input[name='horario_mensal']").value = schedule.horario || "";
            } else {
                wizardForm.querySelector("input[name='horario_diario']").value = schedule.horario || "";
            }

            const deliveryMethods = schedule.delivery_methods || [];
            document.querySelectorAll("input[name='delivery_method']").forEach((input) => {
                input.checked = deliveryMethods.includes(input.value);
            });
            rebuildWizardEmailList(
                (schedule.destinatarios || [])
                    .filter((item) => item.tipo === "email")
                    .map((item) => item.valor)
            );

            wizardForm.querySelector("select[name='report_type']").value = schedule.report_type || "resumido";
            wizardForm.querySelector("select[name='report_template_id']").value = schedule.report_template_id || "";
            wizardForm.querySelector("textarea[name='report_intro']").value = schedule.report_intro || "";
            wizardForm.querySelector("textarea[name='report_footer']").value = schedule.report_footer || "";
            wizardForm.querySelector("textarea[name='report_ai_instruction']").value = schedule.report_ai_instruction || "";
            const useAiInput = document.getElementById("wizard-use-ai");
            if (useAiInput) {
                useAiInput.checked = Boolean(schedule.use_ai);
            }
            if (wizardAiProvider) {
                wizardAiProvider.value = schedule.ai_provider || aiConfig.provider || "";
            }
            if (wizardAiPrompt) {
                wizardAiPrompt.value = schedule.ai_prompt_id || "";
            }
            syncDeliverySections();
            syncAiWizard();
            goToStep(1);

            if (schedule.grafana_server_id) {
                await loadCatalog(schedule.grafana_server_id, schedule.selected_targets || []);
            }
        };

        goToStep(currentStep);
        toggleWizardPeriodFields("diario");
        rebuildWizardEmailList([""]);
        syncDeliverySections();
        syncAiWizard();

        if (wizardAiProvider && aiConfig.provider) {
            wizardAiProvider.value = aiConfig.provider;
        }

        const wizardPeriodo = document.getElementById("wizard-periodo");
        if (wizardPeriodo) {
            wizardPeriodo.addEventListener("change", (event) => toggleWizardPeriodFields(event.target.value));
        }

        document.querySelectorAll("input[name='delivery_method']").forEach((input) => {
            input.addEventListener("change", syncDeliverySections);
        });

        if (useAiInput) {
            useAiInput.addEventListener("change", syncAiWizard);
        }

        showWizardButton?.addEventListener("click", () => {
            loadedCatalog = null;
            goToStep(1);
        });

        showListButton?.addEventListener("click", () => {
            goToStep(1);
        });

        const wizardEmailList = document.getElementById("wizard-email-list");
        if (wizardEmailList) {
            wizardEmailList.addEventListener("click", (event) => {
                if (event.target.classList.contains("remove-email-button")) {
                    const group = event.target.closest(".email-input-group");
                    if (group) {
                        group.remove();
                        syncDeliverySections();
                    }
                }
            });
        }

        const addWizardEmailButton = document.getElementById("wizard-add-email");
        if (addWizardEmailButton) {
            addWizardEmailButton.addEventListener("click", () => {
                const list = document.getElementById("wizard-email-list");
                list.insertAdjacentHTML("beforeend", buildWizardEmailRow());
                syncDeliverySections();
            });
        }

        const loadCatalogButton = document.getElementById("load-grafana-catalog");
        if (loadCatalogButton) {
            loadCatalogButton.addEventListener("click", async () => {
                const select = document.getElementById("grafana-server-select");
                const serverId = select.value;
                if (!serverId) {
                    showFeedback("grafana-catalog-feedback", "error", "Seleccione un servidor Grafana.");
                    return;
                }
                await loadCatalog(serverId);
            });
        }

        if (wizardCancelEdit) {
            wizardCancelEdit.addEventListener("click", () => {
                resetWizardMode();
                setScheduleViewMode("list");
                loadedCatalog = null;
                goToStep(1);
            });
        }

        document.querySelectorAll("[data-edit-schedule]").forEach((button) => {
            button.addEventListener("click", async () => {
                const payload = JSON.parse(button.dataset.editSchedule || "{}");
                await enterEditMode(payload);
            });
        });

        const backButton = document.getElementById("wizard-back");
        const nextButton = document.getElementById("wizard-next");
        if (backButton) {
            backButton.addEventListener("click", () => {
                if (currentStep > 1) {
                    goToStep(currentStep - 1);
                }
            });
        }

        if (nextButton) {
            nextButton.addEventListener("click", () => {
                if (!validateStepTransition(currentStep)) {
                    return;
                }
                if (currentStep < totalSteps) {
                    goToStep(currentStep + 1);
                }
            });
        }

        wizardForm.addEventListener("submit", async (event) => {
            event.preventDefault();
            hideFeedback("schedule-feedback");

            const selectedTargets = collectSelectedTargets();
            const deliveryMethods = collectDeliveryMethods();
            if (!selectedTargets.length) {
                showFeedback("schedule-feedback", "error", "Seleccione un dashboard.");
                return;
            }
            if (!deliveryMethods.length) {
                showFeedback("schedule-feedback", "error", "Seleccione al menos un método de envío.");
                return;
            }
            if (!validateWizardEmailRecipients()) {
                return;
            }
            const currentTelegramConfig = JSON.parse(wizardForm.dataset.telegram || "{}");
            if (deliveryMethods.includes("telegram") && !Number(currentTelegramConfig.chat_count || 0)) {
                showFeedback("schedule-feedback", "error", "Telegram fue seleccionado, pero no hay chats configurados.");
                return;
            }
            if (useAiInput?.checked && !aiConfig.api_key_configured) {
                showFeedback("schedule-feedback", "error", "Ative e configure a IA antes de usar insights.");
                return;
            }
            if (useAiInput?.checked && aiConfig.provider && wizardAiProvider && wizardAiProvider.value !== aiConfig.provider) {
                showFeedback("schedule-feedback", "error", `A IA configurada atualmente e ${aiConfig.provider}.`);
                return;
            }

            document.getElementById("selected-targets-json").value = JSON.stringify(selectedTargets);
            document.getElementById("delivery-methods-json").value = JSON.stringify(deliveryMethods);

            const formData = new FormData(wizardForm);
            formData.set("use_ai", useAiInput?.checked ? "true" : "false");

            const scheduleId = scheduleIdInput?.value.trim();
            const response = await fetch(scheduleId ? `/api/agendamentos/${scheduleId}` : "/api/agendamentos", {
                method: "POST",
                body: formData,
            });
            const body = await response.json();
            showFeedback("schedule-feedback", response.ok ? "success" : "error", body.message);
            if (response.ok) {
                resetWizardMode();
                loadedCatalog = null;
                goToStep(1);
                setTimeout(() => window.location.reload(), 700);
            }
        });
    }
});
