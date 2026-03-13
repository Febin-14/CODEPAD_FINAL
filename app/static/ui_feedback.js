document.addEventListener("DOMContentLoaded", () => {
    const NOTICE_KEY = "codepad_ui_notice";

    const ensureHosts = () => {
        let noticeRegion = document.getElementById("appNoticeRegion");
        if (!noticeRegion) {
            noticeRegion = document.createElement("div");
            noticeRegion.id = "appNoticeRegion";
            noticeRegion.className = "app-notice-region";
            document.body.appendChild(noticeRegion);
        }

        let confirmRoot = document.getElementById("appConfirmRoot");
        if (!confirmRoot) {
            confirmRoot = document.createElement("div");
            confirmRoot.id = "appConfirmRoot";
            confirmRoot.className = "app-confirm-root";
            confirmRoot.innerHTML = `
                <div class="app-confirm-backdrop" data-confirm-close="backdrop"></div>
                <div class="app-confirm-card" role="dialog" aria-modal="true" aria-labelledby="appConfirmTitle">
                    <div class="app-confirm-header">
                        <div>
                            <h3 id="appConfirmTitle" class="app-confirm-title">Confirm action</h3>
                            <p id="appConfirmMessage" class="app-confirm-message"></p>
                        </div>
                    </div>
                    <div class="app-confirm-actions">
                        <button type="button" id="appConfirmCancel" class="app-confirm-btn app-confirm-cancel">Cancel</button>
                        <button type="button" id="appConfirmApprove" class="app-confirm-btn app-confirm-approve">Confirm</button>
                    </div>
                </div>
            `;
            document.body.appendChild(confirmRoot);
        }

        return { noticeRegion, confirmRoot };
    };

    const { noticeRegion, confirmRoot } = ensureHosts();
    const confirmTitle = confirmRoot.querySelector("#appConfirmTitle");
    const confirmMessage = confirmRoot.querySelector("#appConfirmMessage");
    const confirmCancel = confirmRoot.querySelector("#appConfirmCancel");
    const confirmApprove = confirmRoot.querySelector("#appConfirmApprove");

    let confirmResolver = null;

    const closeConfirm = (result) => {
        confirmRoot.classList.remove("is-visible");
        if (confirmResolver) {
            confirmResolver(result);
            confirmResolver = null;
        }
    };

    confirmCancel.addEventListener("click", () => closeConfirm(false));
    confirmApprove.addEventListener("click", () => closeConfirm(true));
    confirmRoot.addEventListener("click", (event) => {
        if (event.target.dataset.confirmClose === "backdrop") {
            closeConfirm(false);
        }
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && confirmRoot.classList.contains("is-visible")) {
            closeConfirm(false);
        }
    });

    const removeNotice = (node) => {
        if (!node || !node.parentNode) {
            return;
        }

        node.classList.add("is-leaving");
        window.setTimeout(() => {
            node.remove();
        }, 220);
    };

    window.appNotice = (message, options = {}) => {
        if (!message) {
            return;
        }

        const type = options.type || "info";
        const duration = options.duration ?? (type === "error" ? 5200 : 3200);
        const notice = document.createElement("div");
        notice.className = `app-notice app-notice-${type}`;
        notice.innerHTML = `
            <div class="app-notice-body">
                <span class="app-notice-tone"></span>
                <div class="app-notice-copy">${String(message)}</div>
            </div>
            <button type="button" class="app-notice-close" aria-label="Dismiss notice">&times;</button>
        `;

        notice.querySelector(".app-notice-close").addEventListener("click", () => removeNotice(notice));
        noticeRegion.appendChild(notice);

        if (duration > 0) {
            window.setTimeout(() => removeNotice(notice), duration);
        }
    };

    window.queueAppNotice = (message, options = {}) => {
        sessionStorage.setItem(
            NOTICE_KEY,
            JSON.stringify({
                message,
                type: options.type || "info",
                duration: options.duration ?? 3200,
            })
        );
    };

    window.appConfirm = (options = {}) => {
        confirmTitle.textContent = options.title || "Confirm action";
        confirmMessage.textContent = options.message || "Are you sure you want to continue?";
        confirmCancel.textContent = options.cancelText || "Cancel";
        confirmApprove.textContent = options.confirmText || "Confirm";

        confirmApprove.classList.remove("is-danger", "is-success");
        if (options.tone === "danger") {
            confirmApprove.classList.add("is-danger");
        } else if (options.tone === "success") {
            confirmApprove.classList.add("is-success");
        }

        confirmRoot.classList.add("is-visible");
        return new Promise((resolve) => {
            confirmResolver = resolve;
        });
    };

    window.alert = (message) => {
        window.appNotice(message, { type: "info" });
    };

    const queuedNotice = sessionStorage.getItem(NOTICE_KEY);
    if (queuedNotice) {
        sessionStorage.removeItem(NOTICE_KEY);
        try {
            const parsed = JSON.parse(queuedNotice);
            window.appNotice(parsed.message, { type: parsed.type, duration: parsed.duration });
        } catch {
            window.appNotice(queuedNotice, { type: "info" });
        }
    }
});
