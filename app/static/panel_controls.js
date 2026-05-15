(() => {
    const mobileQuery = window.matchMedia('(max-width: 1120px)');
    const overlay = document.getElementById('panel-overlay');
    const panelButtons = Array.from(document.querySelectorAll('.panel-toggle'));
    const desktopToggleButtons = panelButtons.filter((button) => Boolean(button.getAttribute('data-desktop-toggle-class')));
    const sortStorageKey = 'vstreamware.saved-channels-sort';
    const savedPanelPollMs = 3000;
    const transcodeStatusPollMs = 1500;
    let savedPanelRefreshInFlight = false;
    let savedPanelPollTimer = null;
    let transcodeStatusInFlight = false;
    let transcodeStatusPollTimer = null;
    let mainBrowserCardResizeObserver = null;

    const transcodeStatusUrl = String(document.body.getAttribute('data-transcode-status-url') || '').trim();
    const headerTranscodeIndicator = document.getElementById('header-transcode-indicator');
    const headerTranscodeLabel = document.getElementById('header-transcode-label');

    const setHeaderTranscodeIndicator = (visible, label, progressPercent = 0, indeterminate = false) => {
        if (!headerTranscodeIndicator) {
            return;
        }

        headerTranscodeIndicator.hidden = !visible;
        if (!visible) {
            headerTranscodeIndicator.classList.remove('is-indeterminate');
            headerTranscodeIndicator.style.setProperty('--header-transcode-progress', '0%');
        } else {
            const normalizedProgress = Number.isFinite(Number(progressPercent))
                ? Math.max(0, Math.min(100, Math.round(Number(progressPercent))))
                : 0;
            headerTranscodeIndicator.style.setProperty('--header-transcode-progress', `${normalizedProgress}%`);
            headerTranscodeIndicator.classList.toggle('is-indeterminate', Boolean(indeterminate));
        }

        if (headerTranscodeLabel && typeof label === 'string' && label.trim()) {
            headerTranscodeLabel.textContent = label;
        }
    };

    // Always reset hidden state first to avoid stale visible UI from cached state/style changes.
    setHeaderTranscodeIndicator(false, 'Transcoding');

    const syncTranscodeIndicator = async () => {
        if (!headerTranscodeIndicator || !transcodeStatusUrl || transcodeStatusInFlight) {
            return;
        }

        transcodeStatusInFlight = true;
        try {
            const response = await fetch(transcodeStatusUrl, {
                cache: 'no-store',
                headers: {
                    'X-Requested-With': 'vstreamware-transcode-queue-status',
                },
            });
            if (!response.ok) {
                throw new Error(`status ${response.status}`);
            }

            const payload = await response.json();
            const active = payload && typeof payload.active === 'object' ? payload.active : null;
            const indicatorText = payload && typeof payload.indicator_text === 'string'
                ? payload.indicator_text.trim()
                : '';
            const activePercentRaw = active ? Number(active.progress_percent) : Number.NaN;
            const hasActiveProgress = Number.isFinite(activePercentRaw);
            const activePercent = hasActiveProgress
                ? Math.max(0, Math.min(100, Math.round(activePercentRaw)))
                : 0;

            if (!active) {
                setHeaderTranscodeIndicator(false, 'Transcoding');
                return;
            }

            if (indicatorText) {
                setHeaderTranscodeIndicator(
                    true,
                    indicatorText,
                    hasActiveProgress ? activePercent : 8,
                    !hasActiveProgress,
                );
                return;
            }

            if (active && (typeof active.file_name === 'string' || typeof active.relative_path === 'string')) {
                const activeName = String(active.file_name || active.relative_path || '').trim() || 'recording';
                const fallbackLabel = `${activeName} (${activePercent}%)`;
                setHeaderTranscodeIndicator(true, fallbackLabel, activePercent, false);
                return;
            }

            setHeaderTranscodeIndicator(true, 'Transcoding...', 8, true);
        } catch (_error) {
            setHeaderTranscodeIndicator(false, 'Transcoding');
        } finally {
            transcodeStatusInFlight = false;
        }
    };

    const stopTranscodeStatusPolling = () => {
        if (transcodeStatusPollTimer === null) {
            return;
        }

        window.clearInterval(transcodeStatusPollTimer);
        transcodeStatusPollTimer = null;
    };

    const startTranscodeStatusPolling = () => {
        if (!headerTranscodeIndicator || !transcodeStatusUrl || transcodeStatusPollTimer !== null) {
            return;
        }

        transcodeStatusPollTimer = window.setInterval(() => {
            if (document.hidden) {
                return;
            }

            void syncTranscodeIndicator();
        }, transcodeStatusPollMs);
    };

    if (headerTranscodeIndicator && transcodeStatusUrl) {
        void syncTranscodeIndicator();
        startTranscodeStatusPolling();

        document.addEventListener('visibilitychange', () => {
            if (document.hidden) {
                stopTranscodeStatusPolling();
                return;
            }

            void syncTranscodeIndicator();
            startTranscodeStatusPolling();
        });

        window.addEventListener('beforeunload', stopTranscodeStatusPolling);
    }

    const getSidePanels = () => Array.from(document.querySelectorAll('.side-panel'));
    const getSavedPanel = () => document.getElementById('saved-panel');
    const getMainVideoBrowserCard = () => document.querySelector('.browser-page-main .video-browser-card');

    const updateSavedPanelMaxHeight = () => {
        const savedPanel = getSavedPanel();
        if (!savedPanel) {
            return;
        }

        const panelShell = savedPanel.querySelector('.panel-shell');
        if (!(panelShell instanceof HTMLElement)) {
            return;
        }

        if (mobileQuery.matches) {
            panelShell.style.removeProperty('--saved-panel-viewport-max-height');
            return;
        }

        const mainVideoBrowserCard = getMainVideoBrowserCard();
        const browserCardHeight = mainVideoBrowserCard instanceof HTMLElement
            ? Math.floor(mainVideoBrowserCard.getBoundingClientRect().height)
            : 0;

        if (browserCardHeight <= 0) {
            return;
        }

        panelShell.style.setProperty('--saved-panel-viewport-max-height', `${browserCardHeight}px`);
    };

    const observeMainVideoBrowserCard = () => {
        if (typeof ResizeObserver !== 'function') {
            return;
        }

        if (mainBrowserCardResizeObserver) {
            mainBrowserCardResizeObserver.disconnect();
            mainBrowserCardResizeObserver = null;
        }

        const mainVideoBrowserCard = getMainVideoBrowserCard();
        if (!(mainVideoBrowserCard instanceof HTMLElement)) {
            return;
        }

        mainBrowserCardResizeObserver = new ResizeObserver(() => {
            updateSavedPanelMaxHeight();
        });
        mainBrowserCardResizeObserver.observe(mainVideoBrowserCard);
    };

    if (panelButtons.length === 0 && getSidePanels().length === 0) {
        return;
    }

    const closePanels = () => {
        for (const panel of getSidePanels()) {
            panel.classList.remove('is-open');
        }

        if (overlay) {
            overlay.classList.remove('is-visible');
        }

        document.body.classList.remove('panel-open');
    };

    const parseBooleanStorageValue = (rawValue) => {
        const normalized = String(rawValue || '').trim().toLowerCase();
        return normalized === '1' || normalized === 'true' || normalized === 'yes' || normalized === 'on';
    };

    const getDesktopToggleClass = (button) => String(button.getAttribute('data-desktop-toggle-class') || '').trim();

    const getDesktopToggleStoreKey = (button, toggleClass) => {
        const explicitKey = String(button.getAttribute('data-desktop-store-key') || '').trim();
        if (explicitKey) {
            return explicitKey;
        }

        return `vstreamware.${toggleClass}`;
    };

    const updateDesktopButtonState = (button) => {
        const toggleClass = getDesktopToggleClass(button);
        if (!toggleClass) {
            return;
        }

        const isCollapsed = document.body.classList.contains(toggleClass);
        const isExpanded = !isCollapsed;
        button.setAttribute('aria-expanded', isExpanded ? 'true' : 'false');
        button.classList.toggle('is-active', isExpanded);

        const labelElement = button.querySelector('.panel-toggle-text');
        const expandedLabel = labelElement ? String(labelElement.getAttribute('data-expanded-label') || '').trim() : '';
        const collapsedLabel = labelElement ? String(labelElement.getAttribute('data-collapsed-label') || '').trim() : '';
        const nextLabelText = isExpanded ? expandedLabel : collapsedLabel;
        if (labelElement && nextLabelText) {
            labelElement.textContent = nextLabelText;
        }

        const ariaLabel = nextLabelText ? `${nextLabelText} panel` : (isExpanded ? 'Hide side panel' : 'Show side panel');
        button.setAttribute('aria-label', ariaLabel);
    };

    const applyDesktopToggleState = (button, collapsed, persist = false) => {
        const toggleClass = getDesktopToggleClass(button);
        if (!toggleClass) {
            return;
        }

        document.body.classList.toggle(toggleClass, Boolean(collapsed));
        updateDesktopButtonState(button);

        if (!persist) {
            return;
        }

        try {
            window.localStorage.setItem(getDesktopToggleStoreKey(button, toggleClass), collapsed ? '1' : '0');
        } catch (_error) {
            // Ignore storage failures.
        }
    };

    const initializeDesktopToggleState = () => {
        for (const button of desktopToggleButtons) {
            const toggleClass = getDesktopToggleClass(button);
            if (!toggleClass) {
                continue;
            }

            let shouldCollapse = false;
            try {
                const persisted = window.localStorage.getItem(getDesktopToggleStoreKey(button, toggleClass));
                shouldCollapse = parseBooleanStorageValue(persisted);
            } catch (_error) {
                shouldCollapse = false;
            }

            document.body.classList.toggle(toggleClass, shouldCollapse);
            updateDesktopButtonState(button);
        }
    };

    const syncDesktopButtons = () => {
        for (const button of desktopToggleButtons) {
            updateDesktopButtonState(button);
        }
    };

    const openPanel = (panelId) => {
        if (!mobileQuery.matches) {
            return;
        }

        const target = document.getElementById(panelId);
        if (!target) {
            return;
        }

        closePanels();
        target.classList.add('is-open');

        if (overlay) {
            overlay.classList.add('is-visible');
        }

        document.body.classList.add('panel-open');
    };

    const sortSavedChannels = (savedList, mode) => {
        if (!savedList) {
            return;
        }

        const items = Array.from(savedList.querySelectorAll('.saved-item'));
        if (items.length <= 1) {
            return;
        }

        const normalizedMode = mode === 'name' ? 'name' : 'recent';
        items.sort((leftItem, rightItem) => {
            const leftName = String(leftItem.getAttribute('data-channel-name') || '').toLowerCase();
            const rightName = String(rightItem.getAttribute('data-channel-name') || '').toLowerCase();
            if (normalizedMode === 'name') {
                return leftName.localeCompare(rightName);
            }

            const leftIsRecording = Number.parseInt(String(leftItem.getAttribute('data-is-recording') || '0'), 10) || 0;
            const rightIsRecording = Number.parseInt(String(rightItem.getAttribute('data-is-recording') || '0'), 10) || 0;
            if (leftIsRecording !== rightIsRecording) {
                return rightIsRecording - leftIsRecording;
            }

            const leftPriority = Number.parseInt(String(leftItem.getAttribute('data-sort-priority') || '2'), 10) || 2;
            const rightPriority = Number.parseInt(String(rightItem.getAttribute('data-sort-priority') || '2'), 10) || 2;
            if (leftPriority !== rightPriority) {
                return leftPriority - rightPriority;
            }

            const leftRecent = Number.parseInt(String(leftItem.getAttribute('data-last-activity') || '0'), 10) || 0;
            const rightRecent = Number.parseInt(String(rightItem.getAttribute('data-last-activity') || '0'), 10) || 0;
            if (leftRecent !== rightRecent) {
                return rightRecent - leftRecent;
            }

            return leftName.localeCompare(rightName);
        });

        for (const item of items) {
            savedList.appendChild(item);
        }
    };

    const readSavedSortMode = () => {
        let initialSortMode = 'recent';
        try {
            const persistedSortMode = window.localStorage.getItem(sortStorageKey);
            if (persistedSortMode === 'name' || persistedSortMode === 'recent') {
                initialSortMode = persistedSortMode;
            }
        } catch (_error) {
            initialSortMode = 'recent';
        }

        return initialSortMode;
    };

    const initializeSavedSortUi = () => {
        const savedPanel = getSavedPanel();
        if (!savedPanel) {
            return;
        }

        const savedList = savedPanel.querySelector('.saved-list');
        const savedSortSelect = savedPanel.querySelector('#saved-channel-sort');
        if (!savedList || !savedSortSelect) {
            return;
        }

        const initialSortMode = readSavedSortMode();
        savedSortSelect.value = initialSortMode;
        sortSavedChannels(savedList, initialSortMode);

        if (savedSortSelect.getAttribute('data-sort-bound') === 'true') {
            return;
        }

        savedSortSelect.setAttribute('data-sort-bound', 'true');
        savedSortSelect.addEventListener('change', () => {
            const mode = savedSortSelect.value === 'name' ? 'name' : 'recent';
            sortSavedChannels(savedList, mode);
            try {
                window.localStorage.setItem(sortStorageKey, mode);
            } catch (_error) {
                // Ignore storage failures.
            }
        });
    };

    const replaceSavedPanelFromHtml = (html, wasOpen) => {
        const currentPanel = getSavedPanel();
        if (!currentPanel || !currentPanel.parentElement) {
            return;
        }

        const currentPanelBody = currentPanel.querySelector('.panel-body');
        const previousScrollTop = currentPanelBody instanceof HTMLElement ? currentPanelBody.scrollTop : 0;
        const previousScrollLeft = currentPanelBody instanceof HTMLElement ? currentPanelBody.scrollLeft : 0;
        const currentPanelShell = currentPanel.querySelector('.panel-shell');
        const previousMaxHeight = currentPanelShell instanceof HTMLElement
            ? String(currentPanelShell.style.getPropertyValue('--saved-panel-viewport-max-height') || '').trim()
            : '';

        const parser = new DOMParser();
        const parsed = parser.parseFromString(html, 'text/html');
        const nextPanel = parsed.querySelector('#saved-panel');
        if (!nextPanel) {
            return;
        }

        const nextPanelShell = nextPanel.querySelector('.panel-shell');
        if (nextPanelShell instanceof HTMLElement && previousMaxHeight) {
            nextPanelShell.style.setProperty('--saved-panel-viewport-max-height', previousMaxHeight);
        }

        currentPanel.replaceWith(nextPanel);
        if (wasOpen) {
            nextPanel.classList.add('is-open');
        }

        const nextPanelBody = nextPanel.querySelector('.panel-body');
        if (nextPanelBody instanceof HTMLElement && (previousScrollTop > 0 || previousScrollLeft > 0)) {
            window.requestAnimationFrame(() => {
                nextPanelBody.scrollTop = previousScrollTop;
                nextPanelBody.scrollLeft = previousScrollLeft;
            });
        }

        initializeSavedSortUi();
        updateSavedPanelMaxHeight();
    };

    const refreshSavedPanel = async () => {
        const savedPanel = getSavedPanel();
        if (!savedPanel || savedPanelRefreshInFlight) {
            return;
        }

        const refreshUrl = String(savedPanel.getAttribute('data-refresh-url') || '').trim();
        if (!refreshUrl) {
            return;
        }

        const wasOpen = savedPanel.classList.contains('is-open');
        savedPanelRefreshInFlight = true;
        try {
            const response = await fetch(refreshUrl, {
                cache: 'no-store',
                headers: {
                    'X-Requested-With': 'vstreamware-saved-panel-refresh',
                },
            });
            if (!response.ok) {
                throw new Error(`status ${response.status}`);
            }

            const html = await response.text();
            replaceSavedPanelFromHtml(html, wasOpen);
            syncDesktopButtons();
        } catch (_error) {
            // Ignore refresh errors and keep current panel state.
        } finally {
            savedPanelRefreshInFlight = false;
        }
    };

    const submitSavedPanelForm = async (form) => {
        const actionUrl = String(form.getAttribute('action') || '').trim();
        if (!actionUrl) {
            return;
        }

        const formData = new FormData(form);

        const submitter = document.activeElement instanceof HTMLElement ? document.activeElement : null;
        if (submitter && 'disabled' in submitter) {
            submitter.disabled = true;
        }

        try {
            const response = await fetch(actionUrl, {
                method: 'POST',
                body: formData,
                cache: 'no-store',
                headers: {
                    'X-Requested-With': 'vstreamware-saved-panel-action',
                },
                redirect: 'follow',
            });
            if (!response.ok) {
                throw new Error(`status ${response.status}`);
            }

            await refreshSavedPanel();
        } catch (_error) {
            // Ignore action errors and keep current panel state.
        } finally {
            if (submitter && 'disabled' in submitter) {
                submitter.disabled = false;
            }
        }
    };

    const shouldSkipAutomaticSavedPanelRefresh = () => {
        const savedPanel = getSavedPanel();
        if (!savedPanel) {
            return true;
        }

        const activeElement = document.activeElement;
        if (!(activeElement instanceof Element) || !savedPanel.contains(activeElement)) {
            return false;
        }

        return activeElement instanceof HTMLInputElement
            || activeElement instanceof HTMLSelectElement
            || activeElement instanceof HTMLTextAreaElement;
    };

    const stopSavedPanelPolling = () => {
        if (savedPanelPollTimer === null) {
            return;
        }

        window.clearInterval(savedPanelPollTimer);
        savedPanelPollTimer = null;
    };

    const startSavedPanelPolling = () => {
        if (savedPanelPollTimer !== null || !getSavedPanel()) {
            return;
        }

        savedPanelPollTimer = window.setInterval(() => {
            if (document.hidden || shouldSkipAutomaticSavedPanelRefresh()) {
                return;
            }

            void refreshSavedPanel();
        }, savedPanelPollMs);
    };

    for (const button of panelButtons) {
        button.addEventListener('click', () => {
            const desktopToggleClass = getDesktopToggleClass(button);
            if (!mobileQuery.matches && desktopToggleClass) {
                const nextCollapsed = !document.body.classList.contains(desktopToggleClass);
                applyDesktopToggleState(button, nextCollapsed, true);
                syncDesktopButtons();
                return;
            }

            const panelId = button.getAttribute('data-target');
            if (!panelId) {
                return;
            }

            openPanel(panelId);
        });
    }

    if (overlay) {
        overlay.addEventListener('click', closePanels);
    }

    document.addEventListener('click', (event) => {
        const target = event.target;
        if (!(target instanceof Element)) {
            return;
        }

        const refreshButton = target.closest('[data-saved-panel-refresh]');
        if (!refreshButton) {
            return;
        }

        event.preventDefault();
        void refreshSavedPanel();
    });

    document.addEventListener('submit', (event) => {
        const target = event.target;
        if (!(target instanceof HTMLFormElement)) {
            return;
        }

        const savedPanel = getSavedPanel();
        if (!savedPanel || !savedPanel.contains(target)) {
            return;
        }

        const method = String(target.getAttribute('method') || target.method || 'get').trim().toLowerCase();
        if (method !== 'post') {
            return;
        }

        event.preventDefault();
        void submitSavedPanelForm(target);
    });

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
            closePanels();
        }
    });

    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            return;
        }

        void refreshSavedPanel();
    });

    window.addEventListener('beforeunload', stopSavedPanelPolling);
    window.addEventListener('beforeunload', () => {
        if (!mainBrowserCardResizeObserver) {
            return;
        }

        mainBrowserCardResizeObserver.disconnect();
        mainBrowserCardResizeObserver = null;
    });

    const handleViewportChange = () => {
        closePanels();
        syncDesktopButtons();
        observeMainVideoBrowserCard();
        updateSavedPanelMaxHeight();
    };

    window.addEventListener('resize', updateSavedPanelMaxHeight);

    if (typeof mobileQuery.addEventListener === 'function') {
        mobileQuery.addEventListener('change', handleViewportChange);
    } else if (typeof mobileQuery.addListener === 'function') {
        mobileQuery.addListener(handleViewportChange);
    }

    initializeDesktopToggleState();
    initializeSavedSortUi();
    observeMainVideoBrowserCard();
    updateSavedPanelMaxHeight();
    startSavedPanelPolling();
})();
