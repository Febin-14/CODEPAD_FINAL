document.addEventListener('DOMContentLoaded', () => {
    const themeToggle = document.getElementById('theme-toggle');
    const currentTheme = localStorage.getItem('theme') || 'dark';

    // Apply the theme immediately to avoid flash of wrong theme, but also do it inline in head if possible.
    document.documentElement.setAttribute('data-theme', currentTheme);

    if (themeToggle) {
        // Set initial icon
        themeToggle.innerHTML = currentTheme === 'dark' ? '☀️' : '🌙';
        themeToggle.title = currentTheme === 'dark' ? 'Switch to Light Mode' : 'Switch to Dark Mode';

        themeToggle.addEventListener('click', () => {
            let theme = document.documentElement.getAttribute('data-theme');
            let newTheme = theme === 'dark' ? 'light' : 'dark';

            document.documentElement.setAttribute('data-theme', newTheme);
            localStorage.setItem('theme', newTheme);

            themeToggle.innerHTML = newTheme === 'dark' ? '☀️' : '🌙';
            themeToggle.title = newTheme === 'dark' ? 'Switch to Light Mode' : 'Switch to Dark Mode';
        });
    }
});
