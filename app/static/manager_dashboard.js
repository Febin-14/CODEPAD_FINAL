document.getElementById('taskForm').addEventListener('submit', async function(e) {
    e.preventDefault();
    const title = document.getElementById('title').value;
    const type = document.getElementById('type').value;
    const user = new URLSearchParams(window.location.search).get('user');
    const res = await fetch('/api/tasks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, type, created_by: user })
    });
    const data = await res.json();
    if (res.ok) {
        document.getElementById('taskMsg').innerText = 'Task added!';
        document.getElementById('taskForm').reset();
    } else {
        document.getElementById('taskMsg').innerText = data.detail || 'Error adding task';
    }
});
