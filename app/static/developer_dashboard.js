window.onload = async function() {
    const user = new URLSearchParams(window.location.search).get('user');
    const res = await fetch('/api/tasks?assigned_to=' + encodeURIComponent(user));
    const data = await res.json();
    const taskList = document.getElementById('taskList');
    if (Array.isArray(data)) {
        data.forEach(task => {
            const li = document.createElement('li');
            li.innerText = `${task.title} (${task.type})`;
            taskList.appendChild(li);
        });
    } else {
        taskList.innerHTML = '<li>No tasks found.</li>';
    }
};
