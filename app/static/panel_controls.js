(() => {
    const mobileQuery = window.matchMedia('(max-width: 980px)');
    const overlay = document.getElementById('panel-overlay');
    const panelButtons = Array.from(document.querySelectorAll('.panel-toggle'));
    const sidePanels = Array.from(document.querySelectorAll('.side-panel'));
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

    if (typeof mobileQuery.addEventListener === 'function') {
        mobileQuery.addEventListener('change', closePanels);
    } else if (typeof mobileQuery.addListener === 'function') {
        mobileQuery.addListener(closePanels);
    }

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
