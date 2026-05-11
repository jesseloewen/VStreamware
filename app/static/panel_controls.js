(() => {
    const mobileQuery = window.matchMedia('(max-width: 980px)');
    const overlay = document.getElementById('panel-overlay');
    const panelButtons = Array.from(document.querySelectorAll('.panel-toggle'));
    const sidePanels = Array.from(document.querySelectorAll('.side-panel'));
    const desktopToggleButtons = panelButtons.filter((button) => Boolean(button.getAttribute('data-desktop-toggle-class')));
    const savedList = document.querySelector('.saved-list');
    const savedSortSelect = document.getElementById('saved-channel-sort');

    if (panelButtons.length === 0 || sidePanels.length === 0) {
        return;
    }

    const closePanels = () => {
        for (const panel of sidePanels) {
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

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
            closePanels();
        }
    });

    const handleViewportChange = () => {
        closePanels();
        syncDesktopButtons();
    };

    if (typeof mobileQuery.addEventListener === 'function') {
        mobileQuery.addEventListener('change', handleViewportChange);
    } else if (typeof mobileQuery.addListener === 'function') {
        mobileQuery.addListener(handleViewportChange);
    }

    initializeDesktopToggleState();

    const sortSavedChannels = (mode) => {
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

    if (savedList && savedSortSelect) {
        const sortStorageKey = 'vstreamware.saved-channels-sort';
        let initialSortMode = 'recent';
        try {
            const persistedSortMode = window.localStorage.getItem(sortStorageKey);
            if (persistedSortMode === 'name' || persistedSortMode === 'recent') {
                initialSortMode = persistedSortMode;
            }
        } catch (_error) {
            initialSortMode = 'recent';
        }

        savedSortSelect.value = initialSortMode;
        sortSavedChannels(initialSortMode);

        savedSortSelect.addEventListener('change', () => {
            const mode = savedSortSelect.value === 'name' ? 'name' : 'recent';
            sortSavedChannels(mode);
            try {
                window.localStorage.setItem(sortStorageKey, mode);
            } catch (_error) {
                // Ignore storage failures.
            }
        });
    }
})();
