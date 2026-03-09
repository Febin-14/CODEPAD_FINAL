document.getElementById('loginForm').addEventListener('submit', async function(e) {
    e.preventDefault();
    const username = document.getElementById('username').value;
    const password = document.getElementById('password').value;
    const role = document.getElementById('role').value;
    const res = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password, role })
    });
    const data = await res.json();
    if (res.ok) {
        if (role === 'manager') {
            window.location.href = '/manager_dashboard.html?user=' + encodeURIComponent(username);
        } else {
            window.location.href = '/developer_dashboard.html?user=' + encodeURIComponent(username);
        }
    } else {
        document.getElementById('loginError').innerText = data.detail || 'Login failed';
    }
});
