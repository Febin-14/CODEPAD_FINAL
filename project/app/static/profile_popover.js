document.addEventListener("DOMContentLoaded", () => {
    const popovers = Array.from(document.querySelectorAll("[data-profile-popover]"));
    if (!popovers.length) {
        return;
    }

    const notify = (message, type = "info") => {
        if (typeof window.appNotice === "function") {
            window.appNotice(message, { type });
            return;
        }
        console.log(`${type}: ${message}`);
    };

    const setPhotoState = (popover, photoUrl) => {
        popover.querySelectorAll("[data-profile-photo-image]").forEach((image) => {
            image.src = photoUrl || "";
            image.classList.toggle("is-hidden", !photoUrl);
        });
        popover.querySelectorAll("[data-profile-photo-fallback]").forEach((fallback) => {
            fallback.classList.toggle("is-hidden", Boolean(photoUrl));
        });
    };

    const closePopover = (popover) => {
        const trigger = popover.querySelector("[data-profile-trigger]");
        const card = popover.querySelector("[data-profile-card]");
        if (!trigger || !card) {
            return;
        }
        popover.classList.remove("is-open");
        trigger.setAttribute("aria-expanded", "false");
        card.hidden = true;
    };

    const openPopover = (popover) => {
        popovers.forEach((item) => {
            if (item !== popover) {
                closePopover(item);
            }
        });

        const trigger = popover.querySelector("[data-profile-trigger]");
        const card = popover.querySelector("[data-profile-card]");
        if (!trigger || !card) {
            return;
        }
        popover.classList.add("is-open");
        trigger.setAttribute("aria-expanded", "true");
        card.hidden = false;
    };

    popovers.forEach((popover) => {
        const trigger = popover.querySelector("[data-profile-trigger]");
        const card = popover.querySelector("[data-profile-card]");
        const uploadButton = popover.querySelector("[data-profile-photo-button]");
        const photoInput = popover.querySelector("[data-profile-photo-input]");
        const uploadUrl = popover.dataset.uploadUrl || "/api/profile/photo";
        if (!trigger || !card) {
            return;
        }

        trigger.addEventListener("click", (event) => {
            event.stopPropagation();
            if (card.hidden) {
                openPopover(popover);
                return;
            }
            closePopover(popover);
        });

        card.addEventListener("click", (event) => {
            event.stopPropagation();
        });

        if (uploadButton && photoInput) {
            uploadButton.addEventListener("click", () => {
                photoInput.click();
            });

            photoInput.addEventListener("change", async () => {
                const file = photoInput.files && photoInput.files[0];
                if (!file) {
                    return;
                }

                const originalText = uploadButton.textContent;
                uploadButton.disabled = true;
                uploadButton.textContent = "Uploading...";

                try {
                    const formData = new FormData();
                    formData.append("photo", file);
                    const response = await fetch(uploadUrl, {
                        method: "POST",
                        body: formData,
                    });
                    const data = await response.json().catch(() => ({}));
                    if (!response.ok) {
                        notify(data.detail || "Unable to upload profile photo", "error");
                        return;
                    }
                    setPhotoState(popover, data.photo_url || "");
                    notify("Profile photo updated.", "success");
                } catch (error) {
                    console.error(error);
                    notify("Unable to upload profile photo", "error");
                } finally {
                    uploadButton.disabled = false;
                    uploadButton.textContent = originalText;
                    photoInput.value = "";
                }
            });
        }
    });

    document.addEventListener("click", (event) => {
        popovers.forEach((popover) => {
            if (!popover.contains(event.target)) {
                closePopover(popover);
            }
        });
    });

    document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") {
            return;
        }
        popovers.forEach(closePopover);
    });
});
