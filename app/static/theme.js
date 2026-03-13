document.addEventListener("DOMContentLoaded", () => {
    const themeToggle = document.getElementById("theme-toggle");
    const currentTheme = localStorage.getItem("theme") || "dark";

    document.documentElement.setAttribute("data-theme", currentTheme);

    if (!themeToggle) {
        return;
    }

    const setToggleState = (theme) => {
        themeToggle.innerHTML = theme === "dark" ? "&#9728;" : "&#9790;";
        themeToggle.title = theme === "dark" ? "Switch to Light Mode" : "Switch to Dark Mode";
    };

    setToggleState(currentTheme);

    themeToggle.addEventListener("click", () => {
        const theme = document.documentElement.getAttribute("data-theme");
        const newTheme = theme === "dark" ? "light" : "dark";

        document.documentElement.setAttribute("data-theme", newTheme);
        localStorage.setItem("theme", newTheme);
        setToggleState(newTheme);
    });
});
